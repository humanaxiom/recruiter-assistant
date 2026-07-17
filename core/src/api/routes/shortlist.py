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

from src.api.deps import get_arq, require_api_key
from src.models.pool import Db
from src.schemas.matching import ShortlistEntry
from src.services import shortlist_service
from src.services.pii import set_pii_key

router = APIRouter(dependencies=[Depends(require_api_key)])

ExportFormat = Literal["csv", "evidence-csv", "json"]


@router.post("/jobs/{job_id}/shortlist", status_code=status.HTTP_202_ACCEPTED)
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
    format: Annotated[ExportFormat, Query()] = "csv",
    reveal: Annotated[bool, Query()] = False,
) -> Response:
    """``pii_service.set_pii_key`` runs inside an open ``db.transaction()``
    BEFORE ``shortlist_service.export_rows`` — its own raw ``pgp_sym_decrypt``
    fails loud without it."""
    async with db.transaction():
        await set_pii_key(db)
        rows = await shortlist_service.export_rows(db, job_id=job_id, reveal=reveal)

    anon_suffix = "" if reveal else "-anon"
    if format == "csv":
        content = shortlist_service.shortlist_csv(rows)
        filename = f"shortlist-{job_id}{anon_suffix}.csv"
        media_type = "text/csv"
    elif format == "evidence-csv":
        content = shortlist_service.shortlist_evidence_csv(rows)
        filename = f"shortlist-{job_id}-evidence{anon_suffix}.csv"
        media_type = "text/csv"
    else:
        content = shortlist_service.shortlist_json(rows)
        filename = f"shortlist-{job_id}{anon_suffix}.json"
        media_type = "application/json"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/jobs/{job_id}/shortlist")
async def list_shortlist(job_id: UUID, db: Db) -> list[ShortlistEntry]:
    return await shortlist_service.list_for_job(db, job_id=job_id)


@router.get("/shortlist/{entry_id}")
async def get_shortlist_entry(entry_id: UUID, db: Db) -> ShortlistEntry:
    return await shortlist_service.get_one(db, entry_id)


__all__ = ["router"]
