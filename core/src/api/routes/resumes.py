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
from src.errors import NotFoundError
from src.models.pool import Db
from src.schemas.matching import JobMatchResultOut
from src.schemas.resumes import ResumeListItem, ResumeOut, ResumeUploadResult
from src.services import resume_service, shortlist_service
from src.services.zip_upload import ZipRejected, expand_zip_entries
from src.storage.blob_store import BlobStore, get_blob_store

router = APIRouter(dependencies=[Depends(require_api_key)])


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
) -> list[ResumeUploadResult]:
    """Multipart upload: one or more résumé files, OR one entry ending
    ``.zip`` which is expanded and merged into the same accepted/rejected
    accounting. A zip containing a path-traversal or over-cap entry rejects
    the WHOLE request (4xx) — nothing at all is enqueued, not even the valid
    entries alongside it. ``parse_resume`` is enqueued ONCE PER ACCEPTED
    résumé, after the upload's own transaction has committed.
    """
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

    consent = consent_acknowledged.strip().lower() == "true"
    try:
        results = await resume_service.upload_resumes(
            db,
            blob_store,
            job_id=job_id,
            files=expanded,
            consent_acknowledged=consent,
            uploaded_by=actor,
            cover_letter_text=cover_letter_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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


# ── reverse-match subresource ────────────────────────────────────────────────

_EXISTS_SQL = "SELECT id FROM resumes WHERE id = $1"


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
