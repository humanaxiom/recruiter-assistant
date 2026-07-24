"""CAS user provisioning (FU-5 slice 5, ADR-019 §10 step 3, §10a).

Ported from hris ``apps/api/src/api/services/user_service.py``, adapted to
this repo's schema: ``role`` is a plain column on ``users`` (no
``user_roles`` join table), so the default role is written directly on the
INSERT rather than a second statement against a roles table, and the
``ON CONFLICT`` (second-login) path must never touch it.

ADR-019 §10a — the default-admin allowlist: on a user's FIRST login only, a
``cas_username`` that matches the caller-supplied ``default_admin_cas_username``
is provisioned ``role='admin'`` instead of ``'recruiter'``. This is decided in
plain Python BEFORE the INSERT and only takes effect on row creation — the
Postgres ``(xmax = 0)`` trick tells us whether THIS statement created the row
(never re-promoting/re-applying on a later login, including one that raced a
concurrent first login for the same username down to a single row).

``default_admin_cas_username`` is a required keyword argument, never read
from ``settings`` inside this module — CLAUDE.md's "config only via
settings.py, never scattered" rule; the caller (the CAS callback route) is
the one place settings are read.
"""

from __future__ import annotations

import logging
from uuid import UUID

from src.schemas.auth import User
from src.services import DbConn

logger = logging.getLogger(__name__)

_DEFAULT_ROLE = "recruiter"
_ADMIN_ROLE = "admin"

_GET_BY_ID_SQL = """
SELECT id, cas_username, display_name, email, role, active, created_at,
       last_seen_at
FROM users WHERE id = $1
"""

# On conflict, refresh last_seen_at and (only when a non-null value was
# supplied) display_name/email. ``role`` is deliberately absent from the SET
# clause on the conflict path — a role changed out-of-band (e.g. by an
# admin) must survive every subsequent login, including one by the
# default-admin username that was since demoted.
_PROVISION_SQL = """
INSERT INTO users (cas_username, display_name, email, role)
VALUES ($1, $2, $3, $4)
ON CONFLICT (cas_username) DO UPDATE
    SET last_seen_at = now(),
        display_name = COALESCE(EXCLUDED.display_name, users.display_name),
        email = COALESCE(EXCLUDED.email, users.email)
RETURNING id, cas_username, display_name, email, role, active, created_at,
          last_seen_at, (xmax = 0) AS was_created
"""


async def provision_or_get(
    conn: DbConn,
    *,
    cas_username: str,
    display_name: str | None = None,
    email: str | None = None,
    default_admin_cas_username: str,
) -> User:
    """Return the user for ``cas_username``, creating them on first sight.

    Race-safe via ``ON CONFLICT``: concurrent first logins for the same
    unknown username collapse to exactly one row (the ``xmax = 0`` trick
    identifies which statement — if any — actually inserted).
    """
    role = _ADMIN_ROLE if cas_username == default_admin_cas_username else _DEFAULT_ROLE
    row = await conn.fetchrow(
        _PROVISION_SQL,
        cas_username,
        display_name,
        email,
        role,
    )
    assert row is not None
    data = dict(row)
    was_created = data.pop("was_created", None)
    user = User(**data)
    if was_created:
        logger.info(
            "user.provisioned cas_username=%s user_id=%s role=%s",
            cas_username,
            user.id,
            user.role,
        )
    return user


async def get_by_id(conn: DbConn, user_id: UUID) -> User | None:
    """Look up one ``users`` row by id, or ``None`` if it does not exist.

    Used by ``GET /auth/cas/user`` and ``src.api.deps.resolve_user`` to turn
    an already-resolved session's ``user_id`` into the full row.
    """
    row = await conn.fetchrow(_GET_BY_ID_SQL, user_id)
    return User(**dict(row)) if row is not None else None
