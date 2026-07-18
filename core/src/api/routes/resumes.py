"""Résumé upload/list/get + reverse-match subresource routes (Phase 6).

Reverse-match (``POST /resumes/{id}/match-jobs`` / ``GET
/resumes/{id}/match-results``) lives here as a subresource of this module
(the plan-of-record default), not a standalone ``routes/matching.py``.
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

from src.api.deps import get_arq, require_api_key, resolve_actor
from src.errors import FileRejectedError, NotFoundError
from src.models.pool import Db
from src.schemas.matching import JobMatchResultOut
from src.schemas.resumes import ResumeListItem, ResumeOut, ResumeUploadResult
from src.services import (
    bulk_ingest_service,
    resume_service,
    reveal_service,
    shortlist_service,
)
from src.services.zip_upload import _MAX_ZIP_ENTRIES, ZipRejected, expand_zip_entries
from src.storage.blob_store import BlobStore, get_blob_store

router = APIRouter(dependencies=[Depends(require_api_key)])

# Cap the number of files in a single multipart batch (memory-exhaustion
# guard): each accepted file is read fully into memory, so an unbounded batch
# is a DoS vector. Kept consistent with the zip-expansion entry cap so a raw
# multi-file upload and a zipped batch are bounded identically.
_MAX_UPLOAD_FILES = _MAX_ZIP_ENTRIES


@router.post("/jobs/{job_id}/resumes", status_code=status.HTTP_202_ACCEPTED)
async def upload_resumes(
    job_id: UUID,
    db: Db,
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    arq: Annotated[ArqRedis, Depends(get_arq)],
    actor: Annotated[str, Depends(resolve_actor)],
    files: Annotated[list[UploadFile], File()],
    consent_acknowledged: Annotated[str, Form()],
    cover_letter_text: Annotated[str | None, Form()] = None,
    cover_letter_file: Annotated[UploadFile | None, File()] = None,
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
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            expanded.append((filename, data))

    cover_file: tuple[str, bytes] | None = None
    if cover_letter_file is not None:
        cover_file = (
            cover_letter_file.filename or "cover_letter",
            await cover_letter_file.read(),
        )

    # FU-3 Slice 2: pair each résumé with ITS OWN cover letter by filename
    # convention (manifest is Slice 3 — plumbed but not passed here). A plain
    # no-suffix upload pairs no cover → empty maps → today's behaviour.
    pairing = bulk_ingest_service.pair_applicants(expanded)
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
    try:
        results = await resume_service.upload_resumes(
            db,
            blob_store,
            job_id=job_id,
            files=resume_files,
            consent_acknowledged=consent,
            uploaded_by=actor,
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


@router.get("/jobs/{job_id}/resumes")
async def list_resumes(
    job_id: UUID,
    db: Db,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[ResumeListItem]:
    return await resume_service.list_for_job(
        db, job_id=job_id, limit=limit, offset=offset
    )


@router.get("/resumes/{resume_id}")
async def get_resume(
    resume_id: UUID, db: Db, reveal: bool = Query(default=False)
) -> ResumeOut:
    """Redaction happens INSIDE ``resume_service.get_one`` (ADR-006 §4) — the
    route never re-queries raw, so the blind-review boundary always applies."""
    return await resume_service.get_one(db, resume_id, reveal=reveal)


_EXISTS_SQL = "SELECT id FROM resumes WHERE id = $1"


@router.post("/resumes/{resume_id}/reveal")
async def reveal_resume(
    resume_id: UUID,
    db: Db,
    actor: Annotated[str, Depends(resolve_actor)],
    context: Annotated[str | None, Query(max_length=64)] = None,
) -> ResumeOut:
    """AUDITED de-anonymization. Records exactly one ``reveal_audit`` row, then
    returns the UN-blinded résumé. Existence is probed FIRST so a missing id
    404s WITHOUT writing an audit row. This is the audited reveal path the UI
    uses; ``GET /resumes/{id}?reveal=true`` stays for direct API callers but
    writes no audit — so the browser reveal button routes here, not there."""
    exists = await db.fetchval(_EXISTS_SQL, resume_id)
    if exists is None:
        raise NotFoundError(f"resume {resume_id} not found", resume_id=str(resume_id))
    await reveal_service.record_reveal(
        db, resume_id=resume_id, actor=actor, context=context
    )
    return await resume_service.get_one(db, resume_id, reveal=True)


# ── reverse-match subresource ────────────────────────────────────────────────


@router.post("/resumes/{resume_id}/match-jobs", status_code=status.HTTP_202_ACCEPTED)
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


@router.get("/resumes/{resume_id}/match-results")
async def get_match_results(resume_id: UUID, db: Db) -> JobMatchResultOut:
    """No redaction on this path — the caller already owns the résumé, so
    there is no blind-review boundary to enforce here (unlike ``GET
    /resumes/{id}`` itself)."""
    return await shortlist_service.get_reverse_match_result(db, resume_id)


__all__ = ["router"]
