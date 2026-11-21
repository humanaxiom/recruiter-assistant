"""The résumé export as a background job.

``POST /jobs/{id}/resumes/exports`` queues a ``resume_export_requests`` row and
enqueues ``build_resume_export`` BY NAME; the worker builds the as-is +
anonymized ZIP into the ``BlobStore`` and the requester polls
``GET /resume-exports/{id}`` (or ``/jobs/{id}/resumes/exports/latest``).
``POST /resume-exports/{id}/download`` is the one place the file leaves, and the
one place the ``resume_export`` audit row is written -- at DISCLOSURE, never at
build, and with counts only.

Every route is admin/recruiter on BOTH the API-key role and the CAS session role
(the same gate as the résumé upload and split routes); both run before any DB or
blob access. The export is the requester's own: status is visible to them (and to
a session admin), the file to them ALONE -- it discloses every candidate at once.

The download is a POST (a GET would be prefetchable by a browser, link scanner or
mail client, and would write an audit row for somebody who never clicked), and is
``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse

from src.api.deps import (
    Role,
    actor_fields_from_user,
    get_arq,
    require_role,
    require_session_role,
    resolve_user,
)
from src.models.pool import Db
from src.schemas.auth import User
from src.services import audit_service
from src.services import resume_export_request_service as requests
from src.settings import get_settings
from src.storage.blob_store import BlobNotFound, BlobStore, get_blob_store

router = APIRouter()

_WRITERS: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER)
_GATES = [
    Depends(require_role(*_WRITERS)),
    Depends(require_session_role(*_WRITERS)),
]

_NOT_READY = "the export is not ready, has expired, or its file is missing"


def _view(row: dict[str, Any]) -> dict[str, Any]:
    """The public shape of a row: state and numbers. Never the blob key."""
    created, expires = row["created_at"], row["expires_at"]
    return {
        "id": str(row["id"]),
        "job_id": str(row["job_id"]),
        "status": row["status"],
        "counts": requests.counts_of(row["counts"]),
        "created_at": created.isoformat() if created is not None else None,
        "expires_at": expires.isoformat() if expires is not None else None,
    }


def _is_admin(user: User | None) -> bool:
    return user is not None and str(user.role) == str(Role.ADMIN)


@router.post("/jobs/{job_id}/resumes/exports", dependencies=_GATES, status_code=202)
async def start_export(
    job_id: UUID,
    db: Db,
    arq: Annotated[ArqRedis, Depends(get_arq)],
    user: Annotated[User | None, Depends(resolve_user)],
) -> JSONResponse:
    if await requests.job_blind_review(db, job_id) is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if user is None:
        raise HTTPException(
            status_code=403, detail="exports are per signed-in user; sign in first"
        )
    cap = get_settings().resume_export_max_applicants
    if await requests.count_live_applicants(db, job_id) > cap:
        raise HTTPException(
            status_code=422,
            detail=f"this job has more than {cap} applicants; too many to export",
        )
    export_id = await requests.create(db, job_id, user.cas_username)
    try:
        await arq.enqueue_job("build_resume_export", str(export_id))
    except Exception:
        await requests.mark_failed(db, export_id)
        raise HTTPException(
            status_code=503, detail="the export queue is unavailable"
        ) from None
    return JSONResponse(
        status_code=202, content={"id": str(export_id), "status": "queued"}
    )


@router.get("/jobs/{job_id}/resumes/exports/latest", dependencies=_GATES)
async def latest_export(
    job_id: UUID,
    db: Db,
    user: Annotated[User | None, Depends(resolve_user)],
) -> dict[str, Any]:
    row = (
        await requests.latest_for(db, job_id, user.cas_username)
        if user is not None
        else None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no export requested yet")
    return _view(row)


@router.get("/resume-exports/{export_id}", dependencies=_GATES)
async def export_status(
    export_id: UUID,
    db: Db,
    user: Annotated[User | None, Depends(resolve_user)],
) -> dict[str, Any]:
    row = await requests.get(db, export_id)
    if row is None or user is None:
        raise HTTPException(status_code=404, detail="export not found")
    if row["requested_by"] != user.cas_username and not _is_admin(user):
        raise HTTPException(status_code=404, detail="export not found")
    return _view(row)


@router.post(
    "/resume-exports/{export_id}/download",
    dependencies=_GATES,
    response_class=Response,
)
async def download_export(
    export_id: UUID,
    db: Db,
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    user: Annotated[User | None, Depends(resolve_user)],
) -> Response:
    row = (
        await requests.get_for_download(db, export_id, user.cas_username)
        if user is not None
        else None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="export not found")
    if not row["downloadable"]:
        raise HTTPException(status_code=409, detail=_NOT_READY)
    try:
        data = await blob_store.get(row["blob_key"])
    except BlobNotFound:
        raise HTTPException(status_code=409, detail=_NOT_READY) from None

    actor_kind, actor_user_id, actor_service = actor_fields_from_user(user)
    counts = requests.counts_of(row["counts"])
    await audit_service.record_audit(
        db,
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        actor_service=actor_service,
        action="resume_export",
        subject_type="job",
        subject_id=row["job_id"],
        job_id=row["job_id"],
        details={
            "applicants": counts.get("applicants"),
            "anonymized": counts.get("anonymized"),
            "not_anonymized": counts.get("not_anonymized"),
            "blind_review": bool(row["blind_review"]),
        },
    )
    disposition = f'attachment; filename="job-{row["job_id"]}-resumes.zip"'
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["router"]
