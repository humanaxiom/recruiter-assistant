"""Job CRUD + status transition routes (Phase 6).

``router`` is mounted with absolute paths (no router-level ``prefix``) so it
can be composed with the other Phase-6 routers without a path clash. Every
route requires ``require_api_key`` (the configurable auth switch — see
``src.api.deps``); it is applied at the router level so it can't be
accidentally omitted from a new route added later.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from src.api.deps import get_arq, require_api_key, resolve_actor
from src.models.pool import Db
from src.schemas.jobs import (
    JDExtractText,
    JobCreate,
    JobListItem,
    JobOut,
    JobStatus,
    JobTransition,
    JobUpdate,
)
from src.services import jd_import_service, job_service

router = APIRouter(dependencies=[Depends(require_api_key)])


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_job(
    payload: JobCreate,
    db: Db,
    arq: Annotated[ArqRedis, Depends(get_arq)],
    actor: Annotated[str, Depends(resolve_actor)],
) -> JobOut:
    """Insert a draft job and enqueue its ``parse_job`` task with the newly
    created (server-minted) id — never a client-supplied one."""
    job = await job_service.create_job(db, payload, created_by=actor)
    await arq.enqueue_job("parse_job", str(job.id))
    return job


# Declared BEFORE /jobs/{job_id} so "jd-extract" never matches as a job id.
@router.post("/jobs/jd-extract")
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


@router.get("/jobs")
async def list_jobs(
    db: Db,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
) -> list[JobListItem]:
    return await job_service.list_jobs(
        db, limit=limit, offset=offset, status=status_filter
    )


@router.get("/jobs/{job_id}")
async def get_job(job_id: UUID, db: Db) -> JobOut:
    return await job_service.get_job(db, job_id)


@router.patch("/jobs/{job_id}")
async def update_job(job_id: UUID, payload: JobUpdate, db: Db) -> JobOut:
    """General partial update. ``status`` is NOT settable here — ``JobUpdate``
    has no ``status`` field (``extra="forbid"`` 422s a client that tries), and
    status changes must go through ``PATCH /jobs/{job_id}/status`` so the
    forward-only state-machine guard always applies. Fields the client omits
    are left unchanged (see ``job_service.update_job`` for the merge
    semantics); 404 (via the global ``AppError`` handler) when the id does
    not resolve."""
    return await job_service.update_job(db, job_id, payload)


@router.patch("/jobs/{job_id}/status")
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
