"""Shortlist generate/list/get + export routes (Phase 6).

The review-workflow decision/stage routes (``/shortlist/{id}/decision`` /
``/shortlist/{id}/stage``) from hris are CUT and must never exist — there is
deliberately no route defined for them here, so hitting them is FastAPI's own
unmatched-route 404 (proving the route table itself has no entry), never a
401/403.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Query, Response, status

from src.api.deps import (
    Role,
    get_arq,
    require_role,
    resolve_user,
    scoped_user_id_or_403,
)
from src.models.pool import Db
from src.schemas.auth import User
from src.schemas.matching import ShortlistEntry
from src.services import shortlist_service
from src.services.pii import set_pii_key

router = APIRouter()

# FU-4 (RBAC), per route: generating a shortlist enqueues real ranking work, so
# it is a write; listing/reading/exporting are blind reads open to all four
# roles (the export can no longer un-blind at all — see below).
_SHORTLIST_WRITERS: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER)
_SHORTLIST_READERS: tuple[Role, ...] = tuple(Role)

ExportFormat = Literal["csv", "evidence-csv", "json"]


@router.post(
    "/jobs/{job_id}/shortlist",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_role(*_SHORTLIST_WRITERS))],
)
async def generate_shortlist(
    job_id: UUID,
    arq: Annotated[ArqRedis, Depends(get_arq)],
) -> dict[str, str]:
    await arq.enqueue_job("shortlist_job", str(job_id))
    return {"job_id": str(job_id), "status": "enqueued"}


# Declared BEFORE /jobs/{job_id}/shortlist's own path shape is unambiguous
# (export is a THIRD path segment) so no ordering hazard exists, but kept
# adjacent for readability.
@router.get("/jobs/{job_id}/shortlist/export")
async def export_shortlist(
    job_id: UUID,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_SHORTLIST_READERS))],
    user: Annotated[User | None, Depends(resolve_user)],
    format: Annotated[ExportFormat, Query()] = "csv",
) -> Response:
    """``pii_service.set_pii_key`` runs inside an open ``db.transaction()``
    BEFORE ``shortlist_service.export_rows`` — its own raw ``pgp_sym_decrypt``
    fails loud without it.

    FU-4/D3: exports are BLIND-ONLY. The former ``reveal`` query parameter was
    an UNAUDITED BULK de-anonymization across a whole shortlist (never recorded
    in ADR-016), so it is gone — ``reveal=False`` is hardcoded below and the
    ``-anon`` filename suffix is now unconditional. If a bulk reveal export
    ever becomes a real need it gets its own audited POST route.

    FU-6 slice 7 (ADR-020 §3/§4) — row-scoped for a hiring_manager SESSION
    (not the key role — see ``scoped_user_id_or_403``'s docstring for why),
    unscoped (``user_id=None``) for admin/recruiter/auditor. An unassigned
    or nonexistent job both surface as a 200 + empty export (never 404) —
    this is a by-``job_id`` LIST subresource, matching ``list_shortlist``'s
    own ADR-020 §5 behaviour.
    """
    user_id = await scoped_user_id_or_403(user, role)
    async with db.transaction():
        await set_pii_key(db)
        rows = await shortlist_service.export_rows(
            db, job_id=job_id, reveal=False, user_id=user_id
        )

    if format == "csv":
        content = shortlist_service.shortlist_csv(rows)
        filename = f"shortlist-{job_id}-anon.csv"
        media_type = "text/csv"
    elif format == "evidence-csv":
        content = shortlist_service.shortlist_evidence_csv(rows)
        filename = f"shortlist-{job_id}-evidence-anon.csv"
        media_type = "text/csv"
    else:
        content = shortlist_service.shortlist_json(rows)
        filename = f"shortlist-{job_id}-anon.json"
        media_type = "application/json"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/jobs/{job_id}/shortlist")
async def list_shortlist(
    job_id: UUID,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_SHORTLIST_READERS))],
    user: Annotated[User | None, Depends(resolve_user)],
) -> list[ShortlistEntry]:
    """FU-6 slice 7 (ADR-020 §3/§4) — row-scoped for a hiring_manager SESSION
    (not the key role — see ``scoped_user_id_or_403``'s docstring for why),
    unscoped (``user_id=None``) for admin/recruiter/auditor. An unassigned
    or nonexistent job both surface as 200 + an empty list (never 404) —
    this is a LIST subresource, matching its pre-existing behaviour for a
    genuinely nonexistent ``job_id``."""
    user_id = await scoped_user_id_or_403(user, role)
    return await shortlist_service.list_for_job(db, job_id=job_id, user_id=user_id)


@router.get("/shortlist/{entry_id}")
async def get_shortlist_entry(
    entry_id: UUID,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_SHORTLIST_READERS))],
    user: Annotated[User | None, Depends(resolve_user)],
) -> ShortlistEntry:
    """FU-6 slice 7 (ADR-020 §3/§5) — row-scoped for a hiring_manager
    SESSION; an unassigned or nonexistent entry both surface as 404 (never
    403) via ``shortlist_service.get_one``'s ``NotFoundError``."""
    user_id = await scoped_user_id_or_403(user, role)
    return await shortlist_service.get_one(db, entry_id, user_id=user_id)


__all__ = ["router"]
