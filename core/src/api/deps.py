"""Cross-cutting API dependencies — RBAC, actor resolution, arq dep.

**Keyed roles (FU-4).** Phase 6's single ``require_api_key`` switch is retired:
the route→role table needs DIFFERENT allowed-role sets on different routes of
the same router (``PATCH /jobs/{id}`` is admin/recruiter-only while ``GET
/jobs/{id}`` is open to all four roles), which one boolean pass/fail dependency
cannot express. Two primitives replace it:

* :func:`resolve_role` — the KEY→ROLE step. Auth DISABLED (all four
  ``settings.api_key_*`` empty) always resolves :attr:`Role.ADMIN`, whatever
  the header carries — today's fail-open-by-EXPLICIT-configuration local-dev
  mode, preserved verbatim. Auth ENABLED: the ``X-API-Key`` header must match
  exactly one configured role key (constant-time, over UTF-8 bytes) or the
  request is rejected with 401.
* :func:`require_role` — a dependency FACTORY producing a per-route check that
  403s a resolved role outside its allowed set. It composes ``resolve_role`` as
  a sub-dependency rather than re-parsing the header, so ``resolve_role`` stays
  the ONE shared function object an ``app.dependency_overrides`` entry can
  target to bypass auth uniformly across every route in a test.

``log_auth_mode`` logs a loud WARNING at API startup when the switch is off, so
a misconfigured deploy is impossible to miss in the logs. No role key value is
ever logged, here or anywhere else.

**Session identity (FU-5 slice 6, ADR-019 §10/§10b).** :func:`resolve_user`
is a SEPARATE, additive dependency — the cookie -> session -> user chain used
by ``src.api.routes.auth`` and (as of slice 7) the audit-identity call sites
in ``src.api.routes.jobs``/``resumes``. It does not replace or modify
:func:`resolve_role`/:func:`require_role`.

**Actor identity (FU-5 slice 7, ADR-019 §8.3/§9.2).** The formerly optional,
unverified actor-name header (and the function that read it) is REMOVED
ENTIRELY — a spoofable identity-shaped header next to a cryptographically
verified session invited confusion and misconfiguration.
``created_by``/``uploaded_by``/the reveal actor are now sourced exclusively
from :func:`resolve_user`'s resolved ``cas_username`` (``None`` when no
identity resolves, e.g. a bare service-key caller with CAS enabled).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import Cookie, Depends, Header, HTTPException, Request

from src.models.pool import Db
from src.schemas.auth import User
from src.services import session_service, user_service
from src.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# ADR-019 §10b — the dev-anonymous synthetic admin's fixed sentinel id.
# Deliberately the all-zero UUID so it is instantly recognisable in logs/DB
# dumps as "not a real user." This id is NEVER written to `users` and is
# NEVER a valid `audit_log` FK target — auth-disabled actions are attributed
# to a fixed "service" actor label at the audit layer (slice 8), not to this
# id. A real DB-backed row (hris's `_ensure_dev_anonymous_user` upsert
# pattern) was deliberately NOT ported: it would pollute `users` with an
# account that never actually logged in.
_DEV_ADMIN_SENTINEL_ID = UUID("00000000-0000-0000-0000-000000000000")


class Role(StrEnum):
    """The four keyed roles (``StrEnum``, so each member IS its wire string —
    ``Role.ADMIN == "admin"``).

    Role-level, NOT row-level (FU-4/D6): there is no
    per-job owner column, so a hiring-manager or auditor key grants its read
    access across every job company-wide."""

    ADMIN = "admin"
    RECRUITER = "recruiter"
    HIRING_MANAGER = "hiring_manager"
    AUDITOR = "auditor"


def _configured_role_keys(settings: Settings) -> tuple[tuple[Role, str], ...]:
    return (
        (Role.ADMIN, settings.api_key_admin),
        (Role.RECRUITER, settings.api_key_recruiter),
        (Role.HIRING_MANAGER, settings.api_key_hiring_manager),
        (Role.AUDITOR, settings.api_key_auditor),
    )


async def resolve_role(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Role:
    """Resolve the presented ``X-API-Key`` to exactly one :class:`Role`.

    Auth disabled (all four role keys empty) ALWAYS resolves ``Role.ADMIN``,
    regardless of what ``x_api_key`` carries. Enabled: a missing key, or one
    matching no configured role, raises 401.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return Role.ADMIN

    # Compare on UTF-8 BYTES, not ``str``: ``secrets.compare_digest`` requires
    # ASCII-only strings and raises ``TypeError`` otherwise — Starlette
    # latin-1-decodes header bytes, so a non-ASCII ``X-API-Key`` header is a
    # valid (non-ASCII) ``str`` that would otherwise crash this dependency into
    # an unhandled 500 instead of failing closed with 401 (SEC-1).
    presented = (x_api_key or "").encode("utf-8")
    matched: Role | None = None
    for role, configured in _configured_role_keys(settings):
        if not configured:
            continue
        # Constant-time, and deliberately NOT short-circuited on the first
        # match: every configured key is compared on every request, so neither
        # response timing nor the number of comparisons leaks which role a
        # near-miss guess was closest to.
        if secrets.compare_digest(presented, configured.encode("utf-8")):
            matched = role

    if matched is None:
        # Never log the presented value or any configured key.
        logger.warning("auth.rejected — X-API-Key matched no configured role")
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return matched


def require_role(*allowed: Role) -> Callable[..., Awaitable[Role]]:
    """Build the per-route authorization dependency for ``allowed``.

    Each call returns a DISTINCT closure (so ``dependency_overrides`` cannot
    target "every ``require_role(...)`` call" as one entry — override the
    shared :func:`resolve_role` instead). A role outside ``allowed`` raises
    403, which is distinct from ``resolve_role``'s 401: authenticated but not
    authorized.
    """
    allowed_roles = frozenset(allowed)

    async def _check(role: Annotated[Role, Depends(resolve_role)]) -> Role:
        if role not in allowed_roles:
            raise HTTPException(
                status_code=403, detail="role not permitted for this route"
            )
        return role

    return _check


def get_arq(request: Request) -> ArqRedis:
    """FastAPI dependency — hand the lifespan-built arq pool to routes.

    Mirrors ``get_db``/``get_blob_store``: raises ``RuntimeError`` when the
    lifespan has not parked a pool on ``app.state.arq``.
    """
    arq: ArqRedis | None = getattr(request.app.state, "arq", None)
    if arq is None:
        raise RuntimeError("arq pool not initialised (lifespan not running)")
    return arq


def log_auth_mode(settings: Settings) -> None:
    """Called ONCE at API startup. Loud WARNING when auth is disabled, an
    informational line otherwise. Never logs a key value."""
    if not settings.auth_enabled:
        logger.warning(
            "AUTH DISABLED — all four role keys are empty; every route is "
            "unauthenticated and resolves to the admin role. Set "
            "API_KEY_ADMIN / API_KEY_RECRUITER / API_KEY_HIRING_MANAGER / "
            "API_KEY_AUDITOR to enable auth."
        )
    else:
        logger.info("auth.enabled")


async def resolve_user(
    request: Request,
    db: Db,
    ra_session: str | None = Cookie(default=None),
) -> User | None:
    """Resolve the CAS session cookie to a :class:`~src.schemas.auth.User`.

    ADR-019 §10b — CAS DISABLED: every request resolves a synthetic
    in-memory dev-admin identity (``role="admin"``), regardless of
    ``ra_session`` (a presented cookie is ignored, not even looked up). See
    the module-level ``_DEV_ADMIN_SENTINEL_ID`` comment for why this is a
    plain object, not a DB-backed row.

    CAS ENABLED: no cookie, or a cookie that does not resolve to a live
    (unrevoked, unexpired) session, or a session whose user no longer
    exists, all return ``None`` — the caller cannot distinguish those cases
    from this return value alone (no timing/existence oracle for a stolen
    cookie, matching ``session_service.get_active_session``'s own
    discipline).

    ``request`` is accepted (unused) to match FastAPI's dependency-injection
    shape and the port source's signature; a later slice may read
    ``request.state``/headers here.
    """
    settings = get_settings()
    if not settings.cas_enabled:
        now = datetime.now(UTC)
        return User(
            id=_DEV_ADMIN_SENTINEL_ID,
            cas_username="dev-anonymous",
            display_name="Dev Anonymous (auth disabled)",
            email=None,
            role="admin",
            active=True,
            created_at=now,
            last_seen_at=now,
        )

    if not ra_session:
        return None

    session = await session_service.get_active_session(db, ra_session)
    if session is None:
        return None

    return await user_service.get_by_id(db, session.user_id)


def actor_fields_from_user(
    user: User | None,
) -> tuple[str, UUID | None, str | None]:
    """Derive the ``(actor_kind, actor_user_id, actor_service)`` triple
    ``audit_service.record_audit`` expects, from a :func:`resolve_user`
    resolution.

    FU-5 slice 9 (ADR-019 §1.4/§6) factors this OUT of
    ``src.api.routes.resumes.reveal_resume``'s inline if/else (which handles
    only two of these three cases, because reveal 403s outright on ``user is
    None``) so ``src.api.routes.jobs``'s ``blind_review``-flip audit — which
    is NOT human-only and must succeed for a bare service-key caller too —
    can share the derivation without duplicating it. Three cases:

    1. ``user is None`` (no identity resolves at all, e.g. CAS enabled with
       no live session) -> ``("service", None, "api")``.
    2. ``user.id == _DEV_ADMIN_SENTINEL_ID`` (the synthetic dev-anonymous
       identity, CAS disabled) -> ``("service", None, user.cas_username)`` —
       never ``actor_kind="user"`` against the sentinel id: it is not a real
       ``users`` row and would violate ``audit_log.actor_user_id``'s FK.
    3. Any other resolved user (a real human session) ->
       ``("user", user.id, None)``.

    Deliberately NOT wired into ``reveal_resume`` in this slice: reveal's
    403-on-``None`` gate runs BEFORE any actor derivation, so reusing this
    helper there would require restructuring an already-green, security-
    sensitive code path for no behavioural gain. Left alone.
    """
    if user is None:
        return ("service", None, "api")
    if user.id == _DEV_ADMIN_SENTINEL_ID:
        return ("service", None, user.cas_username)
    return ("user", user.id, None)
