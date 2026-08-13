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

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from src.api.deps import Role, require_role, require_session_role
from src.models.pool import Db
from src.schemas.audit import AuditLogItem, RevealAuditItem
from src.services import audit_service, reveal_service

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


@router.get(
    "/audit/log",
    dependencies=[Depends(require_session_role(*_AUDIT_READERS))],
)
async def list_audit_log(
    db: Db,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    # `Annotated` rather than a bare `Query(...)` default: bugbear's B008 flags
    # the call-in-default form for this annotation, and jobs.py's
    # `status_filter` already established this shape for the same reason.
    job_id: Annotated[UUID | None, Query()] = None,
) -> list[AuditLogItem]:
    """Phase 1.4 / ADR-036 — the auditor's read of the LIVE ``audit_log``.

    The sibling ``/audit/reveals-legacy`` above reads ``reveal_audit``, FROZEN
    at FU-5 slice 8; every audit event since the cutover lives in ``audit_log``,
    which until this route had **no read path anywhere in the application** —
    producing the access record meant an engineer running SQL by hand.

    **Gated on the CAS SESSION role alone — deliberately, and not by
    oversight.** Two reasons, and they point the same way:

    1. *Attributability.* The audit log names who looked at whom, and it is the
       record an auditor relies on to detect misuse. Reading it should itself be
       attributable to a person; a shared service key is by construction not a
       person. ``require_session_role`` 403s on ``user is None`` (ADR-034 §2),
       so there is no keyless path in. This is a judgement about THIS surface,
       not an answer to ADR-034's carried question about machine readers in
       general (ADR-036 §4).
    2. *Reachability.* A keyed ``require_role(ADMIN, AUDITOR)`` would make this
       route **unreachable from the browser**, which is the only way an auditor
       would ever use it: the Flask BFF presents ONE fixed ``recruiter`` key for
       every browser it serves (FU-4/D6, ``api_client.build_client``), while
       forwarding the real user's session cookie. Session-role gating is exactly
       how the other browser-reachable privileged surface already works —
       ``users.py::_require_admin_session``. Its sibling
       ``/audit/reveals-legacy`` gates on the KEY and is, for this reason,
       browser-unreachable today.

    Like its sibling it never joins ``resumes``, so nothing decrypts and no
    candidate PII can leak here even by accident; ``details`` is additionally
    allowlist-filtered in the service (``redact_audit_details``).
    """
    return await audit_service.list_audit_log(
        db,
        limit=limit,
        offset=offset,
        action=action,
        subject_type=subject_type,
        job_id=job_id,
    )


__all__ = ["router"]
