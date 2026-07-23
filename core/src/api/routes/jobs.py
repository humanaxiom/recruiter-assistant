"""Job CRUD + status transition routes (Phase 6).

``router`` is mounted with absolute paths (no router-level ``prefix``) so it
can be composed with the other Phase-6 routers without a path clash.

**FU-4 (RBAC).** Authorization is PER ROUTE, not router-wide: reads are open to
all four roles, while every write — and ``POST /jobs/jd-extract``, which is a
recruiter-workflow step even though it touches no database — is admin/recruiter
only. ``PATCH /jobs/{job_id}`` is explicitly in the writer set (D5): it can set
``blind_review: false``, and every redaction key gates off ``jobs.blind_review``,
so this route permanently un-blinds every résumé and shortlist under a job with
NO audit row at all — a wider blast radius than the audited reveal endpoint.
Each route names its own allowed set; none inherits one from a sibling.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from src.api.deps import Role, get_arq, require_role, resolve_user
from src.errors import FileRejectedError
from src.models.pool import Db
from src.schemas.auth import User
from src.schemas.jobs import (
    BulkJobResult,
    JDExtractText,
    JobCreate,
    JobListItem,
    JobOut,
    JobStatus,
    JobTransition,
    JobUpdate,
)
from src.services import bulk_ingest_service, jd_import_service, job_service
from src.services.zip_upload import _MAX_ZIP_ENTRIES, ZipRejected, expand_zip_entries

router = APIRouter()

# Job WRITES (plus the jd-extract transform, a recruiter-workflow step).
_JOB_WRITERS: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER)
# Job READS — every role, including auditor (D2) and hiring-manager. Blind
# redaction, not the role, is what protects candidate identity on these.
_JOB_READERS: tuple[Role, ...] = tuple(Role)

# The bulk-JD extension allowlist for a ``.zip`` of job descriptions — WIDER
# than the résumé allowlist (adds ``json``), so a JD exported as JSON is
# accepted. Passed explicitly to ``expand_zip_entries`` so the résumé call site
# keeps its own default untouched.
_BULK_JD_ZIP_EXTENSIONS: frozenset[str] = frozenset({"pdf", "docx", "txt", "json"})
# Cap the raw multipart file count BEFORE any body is read (memory-exhaustion
# guard) — parity with the résumé route's `_MAX_UPLOAD_FILES`.
_MAX_BULK_JD_FILES = _MAX_ZIP_ENTRIES


@router.post(
    "/jobs",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role(*_JOB_WRITERS))],
)
async def create_job(
    payload: JobCreate,
    db: Db,
    arq: Annotated[ArqRedis, Depends(get_arq)],
    user: Annotated[User | None, Depends(resolve_user)],
) -> JobOut:
    """Insert a draft job and enqueue its ``parse_job`` task with the newly
    created (server-minted) id — never a client-supplied one.

    ``created_by`` is sourced from the resolved session identity (ADR-019
    §8.3/§9.2) — the user's ``cas_username`` when one resolves, ``None`` for
    a bare service-key caller."""
    created_by = user.cas_username if user is not None else None
    job = await job_service.create_job(db, payload, created_by=created_by)
    await arq.enqueue_job("parse_job", str(job.id))
    return job


# Declared BEFORE /jobs/{job_id} so "jd-extract" never matches as a job id.
@router.post("/jobs/jd-extract", dependencies=[Depends(require_role(*_JOB_WRITERS))])
async def jd_extract(file: Annotated[UploadFile, File()]) -> JDExtractText:
    """Pull plain JD text out of an uploaded txt/json/pdf/docx so the
    recruiter can review/edit it before creating the job. Pure transform —
    performs NO database write at all."""
    blob = await file.read()
    filename = file.filename or "upload"
    text = jd_import_service.extract_jd_text(
        blob, filename=filename, content_type=file.content_type
    )
    return JDExtractText(filename=filename, text=text, chars=len(text))


# Declared BEFORE /jobs/{job_id} so "bulk" never matches as a job id (same
# reason jd-extract is declared before it).
@router.post(
    "/jobs/bulk",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_role(*_JOB_WRITERS))],
)
async def bulk_create_jobs(
    db: Db,
    arq: Annotated[ArqRedis, Depends(get_arq)],
    user: Annotated[User | None, Depends(resolve_user)],
    files: Annotated[list[UploadFile], File()],
    manifest: Annotated[UploadFile | None, File()] = None,
) -> list[BulkJobResult]:
    """Create one draft job per uploaded JD file (txt/json/pdf/docx), OR one
    entry ending ``.zip`` which is expanded server-side (never client-side)
    through the hardened ``expand_zip_entries`` and merged into the same batch.
    An optional CSV ``manifest`` pins per-file metadata (title/department/…).

    Returns **202** with one ``BulkJobResult`` per expanded file
    (created/duplicate/failed); ``parse_job`` is enqueued ONCE PER CREATED job,
    mirroring the single-create path. A malformed manifest raises
    ``ManifestError`` (422); a hostile zip raises 400 — nothing is inserted in
    either case.
    """
    # Reject an oversized batch on COUNT FIRST — before any body is read into
    # memory or expanded — so a flood of files cannot exhaust memory (mirrors
    # the résumé upload route's guard).
    if len(files) > _MAX_BULK_JD_FILES:
        raise FileRejectedError(
            f"upload has {len(files)} files; the per-request cap is "
            f"{_MAX_BULK_JD_FILES}",
            count=len(files),
        )

    expanded: list[tuple[str, bytes]] = []
    for f in files:
        filename = f.filename or "upload"
        data = await f.read()
        if filename.lower().endswith(".zip"):
            try:
                expanded.extend(
                    expand_zip_entries(data, allowed_extensions=_BULK_JD_ZIP_EXTENSIONS)
                )
            except ZipRejected as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            expanded.append((filename, data))

    parsed_manifest = None
    if manifest is not None:
        parsed_manifest = bulk_ingest_service.parse_csv_manifest(await manifest.read())

    created_by = user.cas_username if user is not None else None
    results = await job_service.create_jobs_bulk(
        db, files=expanded, manifest=parsed_manifest, created_by=created_by
    )
    for r in results:
        if r.outcome == "created" and r.job_id is not None:
            await arq.enqueue_job("parse_job", str(r.job_id))
    return results


@router.get("/jobs", dependencies=[Depends(require_role(*_JOB_READERS))])
async def list_jobs(
    db: Db,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
) -> list[JobListItem]:
    return await job_service.list_jobs(
        db, limit=limit, offset=offset, status=status_filter
    )


@router.get("/jobs/{job_id}", dependencies=[Depends(require_role(*_JOB_READERS))])
async def get_job(job_id: UUID, db: Db) -> JobOut:
    return await job_service.get_job(db, job_id)


@router.patch("/jobs/{job_id}", dependencies=[Depends(require_role(*_JOB_WRITERS))])
async def update_job(job_id: UUID, payload: JobUpdate, db: Db) -> JobOut:
    """General partial update. ``status`` is NOT settable here — ``JobUpdate``
    has no ``status`` field (``extra="forbid"`` 422s a client that tries), and
    status changes must go through ``PATCH /jobs/{job_id}/status`` so the
    forward-only state-machine guard always applies. Fields the client omits
    are left unchanged (see ``job_service.update_job`` for the merge
    semantics); 404 (via the global ``AppError`` handler) when the id does
    not resolve."""
    return await job_service.update_job(db, job_id, payload)


@router.patch(
    "/jobs/{job_id}/status", dependencies=[Depends(require_role(*_JOB_WRITERS))]
)
async def patch_job_status(job_id: UUID, payload: JobTransition, db: Db) -> JobOut:
    """A valid forward transition (e.g. draft -> open) applies and returns
    200. An invalid transition (skipping a state, a backward move, a same-
    state no-op) is a business-rule 409 — distinct from the 422 a
    syntactically-invalid ``JobStatus`` member would already get."""
    try:
        return await job_service.transition_status(db, job_id, payload.to)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = ["router"]
