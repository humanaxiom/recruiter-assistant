"""Human decision record routes.

* ``POST /shortlist/{entry_id}/human-decisions`` -- record (201; 200 on an
  idempotent repeat). Deciders: admin, recruiter, hiring_manager on an ASSIGNED
  job (an unassigned entry 404s via ``get_one``, ADR-020 s5).
* ``GET  /shortlist/{entry_id}/human-decisions`` -- the history, every role.
* ``GET  /jobs/{job_id}/decisions/latest`` -- latest per candidate (badges).

Deliberately NOT ``/shortlist/{id}/decision`` or ``/stage`` -- those routes of
the cut review workflow stay absent. The decision audit row is written by the
service inside its transaction, never here.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from src.api.deps import (
    Role,
    log_auditor_read,
    require_role,
    require_session_role,
    resolve_user,
    scoped_user_id_or_403,
)
from src.errors import NotFoundError
from src.models.pool import Db
from src.schemas.auth import User
from src.schemas.decisions import (
    DecisionBadge,
    DecisionHistory,
    DecisionIn,
    DecisionOut,
)
from src.schemas.matching import ShortlistEntry
from src.services import DbConn, decision_service, shortlist_service

router = APIRouter()

_DECIDERS: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER, Role.HIRING_MANAGER)
_READERS: tuple[Role, ...] = tuple(Role)


async def _resolve_entry(
    db: DbConn, entry_id: UUID, user_id: UUID | None
) -> ShortlistEntry:
    """``get_one`` with an id-free 404 body, so a missing entry and one the
    hiring manager is not assigned to are byte-identical (ADR-020 s5)."""
    try:
        return await shortlist_service.get_one(db, entry_id, user_id=user_id)
    except NotFoundError:
        raise NotFoundError("shortlist entry not found") from None


@router.post(
    "/shortlist/{entry_id}/human-decisions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_session_role(*_DECIDERS))],
)
async def record_human_decision(
    entry_id: UUID,
    body: DecisionIn,
    response: Response,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_DECIDERS))],
    user: Annotated[User | None, Depends(resolve_user)],
) -> DecisionOut:
    user_id = await scoped_user_id_or_403(user, role)
    entry = await _resolve_entry(db, entry_id, user_id)
    if user is None:  # require_session_role already 403s a missing session
        raise HTTPException(status_code=403, detail="a signed-in session is required")
    out, created = await decision_service.record(db, entry=entry, user=user, body=body)
    if not created:
        response.status_code = status.HTTP_200_OK
    return out


@router.get("/shortlist/{entry_id}/human-decisions")
async def get_human_decisions(
    entry_id: UUID,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_READERS))],
    user: Annotated[User | None, Depends(resolve_user)],
) -> DecisionHistory:
    user_id = await scoped_user_id_or_403(user, role)
    entry = await _resolve_entry(db, entry_id, user_id)
    items = await decision_service.history(db, entry.job_id, entry.resume_id)
    await log_auditor_read(
        db,
        user,
        action="read_decisions",
        subject_type="resume",
        subject_id=entry.resume_id,
        job_id=entry.job_id,
    )
    return DecisionHistory(latest=items[0] if items else None, history=items)


@router.get("/jobs/{job_id}/decisions/latest")
async def latest_decisions(
    job_id: UUID,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_READERS))],
    user: Annotated[User | None, Depends(resolve_user)],
) -> list[DecisionBadge]:
    user_id = await scoped_user_id_or_403(user, role)
    badges = await decision_service.latest_for_job(db, job_id, user_id)
    await log_auditor_read(
        db,
        user,
        action="read_decisions_latest",
        subject_type="job",
        subject_id=job_id,
        job_id=job_id,
    )
    return badges
