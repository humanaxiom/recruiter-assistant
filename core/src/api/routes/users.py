"""user-admin-roles slice 5 — ``GET /users``, an admin-only listing of every
``users`` row (the surface slice 6 will use to pick a target for a role
change).

**The core design decision this module pins (planner decision #3).** The gate
is on the CAS SESSION role (``resolve_user().role == "admin"``), NOT the
API-key role (``resolve_role``/``require_role``). The Flask viewer sends the
ONE shared ``recruiter`` API key for every browser user
(``core/frontend/api_client.py``) — a key-role gate would 403 every real admin
browsing through that shared key, and would ALSO let a bare service/admin KEY
with no verifiable session list every user, which is exactly the "is a real
human" gap ``job_assignees._require_real_assigner`` (ADR-020 §2, FU-6 slice 3)
left open by only checking "a real human session resolves" and never checking
that session's ROLE. ``_require_admin_session`` closes both gaps in one gate:
``user is not None and user.role == "admin"`` — no key is consulted at all.

Deliberately NOT added to the ``require_role_assigned`` guard in
``src.api.main`` — ``_require_admin_session`` is stricter (a no-role session
is already 403'd by it alone), so stacking the two would be redundant.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import resolve_user
from src.models.pool import Db
from src.schemas.auth import User
from src.services import user_service

router = APIRouter()


async def _require_admin_session(
    user: Annotated[User | None, Depends(resolve_user)],
) -> User:
    """403 unless ``user`` is a real session with ``role == "admin"`` — the
    CAS-disabled synthetic dev-anonymous sentinel (``role="admin"``) also
    passes, exactly like ``require_role_assigned``/``scoped_user_id_or_403``
    treat it."""
    if user is None or user.role != "admin":
        raise HTTPException(
            status_code=403, detail="admin session required for this route"
        )
    return user


@router.get("/users")
async def list_users(
    db: Db,
    _admin: Annotated[User, Depends(_require_admin_session)],
) -> list[User]:
    return await user_service.list_users(db)


__all__ = ["router"]
