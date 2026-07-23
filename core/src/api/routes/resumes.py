"""Résumé upload/list/get + reverse-match subresource routes (Phase 6).

Reverse-match (``POST /resumes/{id}/match-jobs`` / ``GET
/resumes/{id}/match-results``) lives here as a subresource of this module
(the plan-of-record default), not a standalone ``routes/matching.py``.

**FU-4 (RBAC).** Authorization is PER ROUTE. Blind reads (list + get one) are
open to all four roles; upload and the reverse-match subresource are
admin/recruiter. ``POST /resumes/{id}/reveal`` is admin/recruiter ONLY and
deliberately NARROWER than ``GET /resumes/{id}``: an auditor may read a résumé
in blind form (D2) but may never un-blind one.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)

from src.api.deps import (
    _DEV_ADMIN_SENTINEL_ID,
    Role,
    get_arq,
    require_role,
    resolve_user,
)
from src.errors import FileRejectedError, NotFoundError
from src.models.pool import Db
from src.schemas.auth import User
from src.schemas.matching import JobMatchResultOut
from src.schemas.resumes import ResumeListItem, ResumeOut, ResumeUploadResult
from src.services import (
    audit_service,
    bulk_ingest_service,
    resume_service,
    # FU-5 slice 8 (ADR-019 §6): no longer CALLED from this module — the
    # reveal route now writes `audit_log` via `audit_service` instead. KEPT
    # (not deleted): `reveal_audit`/`reveal_service` stay read-only per
    # ADR-019 §6's migration posture, and tests assert this module's
    # `reveal_service.record_reveal` is never awaited on the reveal path any
    # more (proving the cutover, not merely assuming it) — see
    # tests/unit/test_route_reveal.py.
    reveal_service,  # noqa: F401
    shortlist_service,
)
from src.services.zip_upload import _MAX_ZIP_ENTRIES, ZipRejected, expand_zip_entries
from src.storage.blob_store import BlobStore, get_blob_store

router = APIRouter()

# Résumé writes + the reverse-match subresource (a recruiter-workflow action
# that fans out LLM work, not a read).
_RESUME_WRITERS: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER)
# Blind reads — every role (D2: an auditor gets the same blind reads as a
# hiring manager).
_RESUME_READERS: tuple[Role, ...] = tuple(Role)
# The audited un-blind. Named separately from ``_RESUME_WRITERS`` even though
# the members currently coincide: this set must never be widened by a
# copy-paste from the read set on the sibling routes around it.
_REVEALERS: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER)

# Cap the number of files in a single multipart batch (memory-exhaustion
# guard): each accepted file is read fully into memory, so an unbounded batch
# is a DoS vector. Kept consistent with the zip-expansion entry cap so a raw
# multi-file upload and a zipped batch are bounded identically.
_MAX_UPLOAD_FILES = _MAX_ZIP_ENTRIES


@router.post(
    "/jobs/{job_id}/resumes",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_role(*_RESUME_WRITERS))],
)
async def upload_resumes(
    job_id: UUID,
    db: Db,
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    arq: Annotated[ArqRedis, Depends(get_arq)],
    user: Annotated[User | None, Depends(resolve_user)],
    files: Annotated[list[UploadFile], File()],
    consent_acknowledged: Annotated[str, Form()],
    cover_letter_text: Annotated[str | None, Form()] = None,
    cover_letter_file: Annotated[UploadFile | None, File()] = None,
    pairing_manifest: Annotated[UploadFile | None, File()] = None,
) -> list[ResumeUploadResult]:
    """Multipart upload: one or more résumé files, OR one entry ending
    ``.zip`` which is expanded and merged into the same accepted/rejected
    accounting. A zip containing a path-traversal or over-cap entry rejects
    the WHOLE request (4xx) — nothing at all is enqueued, not even the valid
    entries alongside it. ``parse_resume`` is enqueued ONCE PER ACCEPTED
    résumé, after the upload's own transaction has committed.
    """
    # Reject an oversized batch on COUNT FIRST — before any body is read into
    # memory or expanded — so a flood of files cannot exhaust memory.
    if len(files) > _MAX_UPLOAD_FILES:
        raise FileRejectedError(
            f"upload has {len(files)} files; the per-request cap is "
            f"{_MAX_UPLOAD_FILES}",
            count=len(files),
        )

    expanded: list[tuple[str, bytes]] = []
    for f in files:
        filename = f.filename or "upload"
        data = await f.read()
        if filename.lower().endswith(".zip"):
            try:
                expanded.extend(expand_zip_entries(data))
            except ZipRejected as exc:
                # A ``manifest.json`` zipped WITH the résumés trips the zip
                # allowlist (json isn't an accepted résumé extension). Surface
                # the fix, not the generic "disallowed extension" message.
                detail = str(exc)
                if ".json" in detail.lower():
                    detail = (
                        "upload the pairing manifest as its own field, not "
                        "inside the résumé zip"
                    )
                raise HTTPException(status_code=400, detail=detail) from exc
        else:
            expanded.append((filename, data))

    # FU-3 Slice 3: an explicit résumé↔cover pairing manifest (its own field).
    # A malformed manifest raises ``ManifestError`` (AppError, 422) → the global
    # handler renders it before any body is persisted.
    manifest: dict[str, str | None] | None = None
    if pairing_manifest is not None:
        manifest = bulk_ingest_service.parse_pairing_manifest(
            await pairing_manifest.read()
        )

    cover_file: tuple[str, bytes] | None = None
    if cover_letter_file is not None:
        cover_file = (
            cover_letter_file.filename or "cover_letter",
            await cover_letter_file.read(),
        )

    # FU-3 Slice 2/3: pair each résumé with ITS OWN cover letter. The manifest
    # (when supplied) takes precedence; everything it doesn't name falls back to
    # the filename convention. A plain no-suffix upload with no manifest pairs
    # no cover → empty maps → today's behaviour.
    pairing = bulk_ingest_service.pair_applicants(expanded, manifest=manifest)
    resume_files: list[tuple[str, bytes]] = []
    cover_letter_map: dict[str, tuple[str, bytes]] = {}
    warnings_map: dict[str, list[str]] = {}
    for pair in pairing.pairs:
        key = bulk_ingest_service.basename_lower(pair.resume[0])
        resume_files.append(pair.resume)
        if pair.cover_letter is not None:
            cover_letter_map[key] = pair.cover_letter
        if pair.note:
            warnings_map.setdefault(key, []).append(pair.note)

    # Ambiguity guard: per-résumé pairing AND a singular batch cover letter
    # together is ambiguous — never silently pick one.
    has_singular_cover = cover_file is not None or bool(
        cover_letter_text and cover_letter_text.strip()
    )
    if cover_letter_map and has_singular_cover:
        raise HTTPException(
            status_code=422,
            detail=(
                "ambiguous: both per-résumé cover pairing and a single batch "
                "cover letter were supplied"
            ),
        )

    consent = consent_acknowledged.strip().lower() == "true"
    uploaded_by = user.cas_username if user is not None else None
    try:
        results = await resume_service.upload_resumes(
            db,
            blob_store,
            job_id=job_id,
            files=resume_files,
            consent_acknowledged=consent,
            uploaded_by=uploaded_by,
            cover_letter_text=cover_letter_text,
            cover_letter_file=cover_file,
            cover_letter_map=cover_letter_map or None,
            warnings_map=warnings_map or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Manifest references to files that weren't uploaded (Slice 3) surface as
    # rejected rows so the operator sees what was dropped — never a silent loss.
    for missing_name, reason in pairing.rejected:
        results.append(
            ResumeUploadResult(
                original_filename=missing_name, outcome="rejected", reason=reason
            )
        )

    for r in results:
        if r.outcome == "accepted" and r.resume_id is not None:
            await arq.enqueue_job("parse_resume", str(r.resume_id))

    return results


@router.get(
    "/jobs/{job_id}/resumes", dependencies=[Depends(require_role(*_RESUME_READERS))]
)
async def list_resumes(
    job_id: UUID,
    db: Db,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ResumeListItem]:
    return await resume_service.list_for_job(
        db, job_id=job_id, limit=limit, offset=offset
    )


@router.get(
    "/resumes/{resume_id}", dependencies=[Depends(require_role(*_RESUME_READERS))]
)
async def get_resume(resume_id: UUID, db: Db) -> ResumeOut:
    """Redaction happens INSIDE ``resume_service.get_one`` (ADR-006 §4) — the
    route never re-queries raw, so the blind-review boundary always applies.

    FU-4/D3: there is NO ``reveal`` query parameter here any more, per ADR-016's
    own decision that "reveal is an explicit, POST-only, audited action". A
    caller appending ``?reveal=true`` gets FastAPI's ordinary ignore-unknown-
    query-params behaviour and a fully blind response — this route has no code
    path left capable of un-blinding. ``POST /resumes/{id}/reveal`` is the only
    un-blinding path.
    """
    return await resume_service.get_one(db, resume_id)


_EXISTS_SQL = "SELECT id FROM resumes WHERE id = $1"


@router.post(
    "/resumes/{resume_id}/reveal", dependencies=[Depends(require_role(*_REVEALERS))]
)
async def reveal_resume(
    resume_id: UUID,
    db: Db,
    user: Annotated[User | None, Depends(resolve_user)],
    context: Annotated[str | None, Query(max_length=64)] = None,
) -> ResumeOut:
    """AUDITED de-anonymization. Records exactly one ``audit_log`` row, then
    returns the UN-blinded résumé.

    **FU-5 slice 8 (ADR-019 §6/§7/§10b) — the reveal -> ``audit_log`` cutover,
    plus the human-attribution gate.** This route no longer writes
    ``reveal_audit`` (via ``reveal_service.record_reveal``) at all — see
    ``src.services.audit_service.record_audit``. Three cases, entirely
    determined by the identity :func:`resolve_user` resolves (no separate
    ``settings.cas_enabled`` read needed here — the dependency already
    encodes it):

    1. No identity resolves at all (``user is None`` — CAS enabled, no live
       session) -> 403, immediately, BEFORE the existence probe. This is
       deliberate: probing existence first would let an unauthenticated
       caller distinguish "resume exists" (403) from "resume missing" (404)
       — a existence oracle on a résumé collection that should require a
       human session to query at all. Checking the human gate first means an
       unauthenticated caller always gets the same 403 regardless of whether
       ``resume_id`` is real.
    2. A real human session resolves (``user.id`` is a real ``users`` row) ->
       ``actor_kind='user'``, keyed on ``user.id``.
    3. The synthetic dev-anonymous identity resolves (``resolve_user``'s
       auth-disabled fallback, ``user.id == _DEV_ADMIN_SENTINEL_ID`` — ADR-019
       §10b) -> ``actor_kind='service'`` / ``actor_service='dev-anonymous'``.
       Never ``actor_kind='user'`` against the sentinel id: it is not a real
       ``users`` row and would violate ``audit_log.actor_user_id``'s FK.

    Since FU-4/D3 removed the ``reveal`` query parameter from ``GET
    /resumes/{id}``, this is the ONLY un-blinding path in the API —
    restricted to admin/recruiter, and the role check runs as a dependency
    so a disallowed caller is rejected BEFORE the human gate, the existence
    probe, the audit write, or any decryption.

    ADR-016's ordering guarantee, restated ADR-019 §7: the audit row is
    written — and, via ``audit_service.record_audit``'s bare (non-
    transaction-wrapped) ``execute``, autocommitted — BEFORE the decrypting
    read (``resume_service.get_one(..., reveal=True)``), so a crash during
    decryption never leaves an un-audited reveal."""
    if user is None:
        raise HTTPException(
            status_code=403,
            detail="reveal requires an attributable human session",
        )

    exists = await db.fetchval(_EXISTS_SQL, resume_id)
    if exists is None:
        raise NotFoundError(f"resume {resume_id} not found", resume_id=str(resume_id))

    if user.id == _DEV_ADMIN_SENTINEL_ID:
        await audit_service.record_audit(
            db,
            actor_kind="service",
            actor_user_id=None,
            actor_service=user.cas_username,
            action="reveal",
            subject_type="resume",
            subject_id=resume_id,
            context=context,
        )
    else:
        await audit_service.record_audit(
            db,
            actor_kind="user",
            actor_user_id=user.id,
            actor_service=None,
            action="reveal",
            subject_type="resume",
            subject_id=resume_id,
            context=context,
        )

    return await resume_service.get_one(db, resume_id, reveal=True)


# ── reverse-match subresource ────────────────────────────────────────────────


@router.post(
    "/resumes/{resume_id}/match-jobs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_role(*_RESUME_WRITERS))],
)
async def trigger_reverse_match(
    resume_id: UUID,
    db: Db,
    arq: Annotated[ArqRedis, Depends(get_arq)],
) -> dict[str, str]:
    """A lightweight existence probe (not a full ``resume_service.get_one``
    PII decrypt) BEFORE enqueueing anything — a nonexistent id 404s and
    nothing is enqueued."""
    exists = await db.fetchval(_EXISTS_SQL, resume_id)
    if exists is None:
        raise NotFoundError(f"resume {resume_id} not found", resume_id=str(resume_id))
    await arq.enqueue_job("reverse_match_job", str(resume_id))
    return {"resume_id": str(resume_id), "status": "enqueued"}


@router.get(
    "/resumes/{resume_id}/match-results",
    dependencies=[Depends(require_role(*_RESUME_WRITERS))],
)
async def get_match_results(resume_id: UUID, db: Db) -> JobMatchResultOut:
    """No redaction on this path — the caller already owns the résumé, so
    there is no blind-review boundary to enforce here (unlike ``GET
    /resumes/{id}`` itself)."""
    return await shortlist_service.get_reverse_match_result(db, resume_id)


__all__ = ["router"]
