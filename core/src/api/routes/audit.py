"""FU-5 slice 10 (ADR-019 §6 / §9.4) — ``GET /audit/reveals-legacy``, a
read-only, paginated view of the FROZEN ``reveal_audit`` table.

``reveal_audit`` was the live reveal-audit sink before slice 8 cut reveal over
to the generalized ``audit_log`` table (see ``src.services.reveal_service``'s
module docstring and ``src.api.routes.resumes.reveal_resume``). This route
exists purely for historical review of that frozen table, per ADR-019 §6 —
it is the auditor role's first real capability anywhere in the codebase
(§9.4's ratified build decision 4), so it is gated admin + auditor, not
admin-only like the other write-adjacent routes.

Reads ``reveal_audit`` ALONE — never joins ``resumes`` — so nothing decrypts
and no candidate PII can leak through this endpoint even by accident. No
router-level ``prefix``; the absolute path lives on the route decorator,
mirroring every other Phase-6/FU-5 route module.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from src.api.deps import Role, require_role
from src.models.pool import Db
from src.schemas.audit import RevealAuditItem
from src.services import reveal_service

router = APIRouter()

_AUDIT_READERS: tuple[Role, ...] = (Role.ADMIN, Role.AUDITOR)


@router.get(
    "/audit/reveals-legacy", dependencies=[Depends(require_role(*_AUDIT_READERS))]
)
async def list_reveals_legacy(
    db: Db,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[RevealAuditItem]:
    return await reveal_service.list_reveal_audit(db, limit=limit, offset=offset)


__all__ = ["router"]
