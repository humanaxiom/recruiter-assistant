"""Unit tests for FU-4's cross-cutting API dependencies — ``src.api.deps``.

**REWRITE (FU-4, keyed roles).** The Phase-6 single-switch ``require_api_key``
dependency is RETIRED entirely — the route→role table needs different
allowed-role sets on different routes of the SAME router (e.g. ``PATCH
/jobs/{id}`` is admin/recruiter-only while ``GET /jobs/{id}`` is open to all
four roles), which a single boolean pass/fail dependency cannot express. It is
replaced by two primitives:

* ``Role(str, Enum)`` — ``ADMIN``/``RECRUITER``/``HIRING_MANAGER``/``AUDITOR``,
  values ``"admin"``/``"recruiter"``/``"hiring_manager"``/``"auditor"``.
* ``resolve_role(x_api_key: str | None = Header(alias="X-API-Key")) -> Role``
  — the KEY→ROLE resolution step. ``settings.auth_enabled is False`` (all
  four ``api_key_*`` fields empty) -> ALWAYS resolves ``Role.ADMIN``,
  regardless of what ``x_api_key`` carries (today's fail-open-by-explicit-
  configuration local-dev mode, preserved verbatim — "nothing that works
  today with auth off may change behavior"). Enabled: the header must match
  exactly one configured role key (constant-time, UTF-8 bytes), else
  ``HTTPException(401)``.
* ``require_role(*allowed: Role)`` — a dependency FACTORY. Returns an async
  callable with signature ``async def _check(role: Annotated[Role,
  Depends(resolve_role)]) -> Role`` (so it composes ``resolve_role`` as its
  own sub-dependency rather than re-parsing the header) that raises
  ``HTTPException(403)`` when the resolved role is not in ``allowed``, else
  returns it. Calling the factory's *return value* directly with a keyword
  ``role=`` argument (bypassing FastAPI's DI) is the locked shape route test
  files rely on for isolated unit coverage.

**Why route tests override ``resolve_role``, never ``require_role(...)``:**
every ``Depends(require_role(Role.ADMIN, Role.RECRUITER))`` call site
produces a DISTINCT closure object, even across two routes with the identical
allowed-role tuple — so ``app.dependency_overrides`` cannot target "every
require_role(...) call" as one entry. ``resolve_role`` is the single shared
function object every one of those closures depends on, so overriding IT
propagates through every route's role check uniformly. This is proved
directly below, by
``test_require_role_composes_with_resolve_role_via_dependency_override``,
and is the exact mechanism every ``test_route_*.py`` file's ``_build_app``
helper now relies on in place of the old
``app.dependency_overrides[require_api_key] = lambda: None`` bypass.

``get_arq`` is UNCHANGED from Phase 6 — kept here for regression coverage.
``log_auth_mode`` is updated: it now reads ``settings.auth_enabled`` (not the
retired ``settings.api_key`` truthiness) so it warns/informs consistently
with the new four-key switch.

**RETIRED (FU-5 slice 7, ADR-019 §8.3).** ``resolve_actor`` and the
``X-Actor-Name`` header it read are DELETED entirely — "the optional,
unverified ``X-Actor-Name`` header ... is removed entirely". The five
``resolve_actor`` regression tests that lived in this file under Phase 6 are
deleted in this same commit (not left red pointing at a function the ADR says
must not exist); ``test_resolve_actor_no_longer_exists`` below is the
positive pin for the deletion. ``created_by``/``uploaded_by`` are now sourced
from ``src.api.deps.resolve_user`` (ADR-019 §9.2/§10) — see
``test_route_jobs.py``, ``test_route_jobs_bulk.py``, ``test_route_resumes.py``
and ``test_route_reveal.py`` for the call-site coverage of that switch.

**FU-5 slice 12 (ADR-019 §10 step 5) — sliding-window refresh wiring.** The
tests near the bottom of this file (``test_resolve_user_calls_refresh_if_
needed_...`` and friends) pin that ``resolve_user`` calls
``session_service.refresh_if_needed`` when a session resolves, with
settings-derived ``ttl_seconds``/``idle_refresh_seconds`` — closing a
reviewer finding that ``refresh_if_needed`` existed and was unit-tested in
isolation (``tests/unit/test_session_service.py``) but nothing on the
request path ever called it, so sessions hard-expired at the fixed TTL
instead of sliding. Nothing here mocks a real database — the actual
``expires_at`` extension is real-Postgres behaviour and is separately pinned
in ``tests/integration/test_auth_routes_pg.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.settings import Settings


def _settings(
    *,
    api_key_admin: str = "",
    api_key_recruiter: str = "",
    api_key_hiring_manager: str = "",
    api_key_auditor: str = "",
) -> Settings:
    return Settings(
        api_key_admin=api_key_admin,
        api_key_recruiter=api_key_recruiter,
        api_key_hiring_manager=api_key_hiring_manager,
        api_key_auditor=api_key_auditor,
        skill_hash_salt="test-salt",
        pii_key="test-key",
    )


# ── Role enum ─────────────────────────────────────────────────────────────


def test_role_enum_has_exactly_the_four_expected_members() -> None:
    from src.api.deps import Role

    assert {r.value for r in Role} == {
        "admin",
        "recruiter",
        "hiring_manager",
        "auditor",
    }


def test_role_enum_members_are_string_valued() -> None:
    from src.api.deps import Role

    assert Role.ADMIN == "admin"
    assert Role.RECRUITER == "recruiter"
    assert Role.HIRING_MANAGER == "hiring_manager"
    assert Role.AUDITOR == "auditor"


# ── resolve_role: enabled, key→role resolution ──────────────────────────


@pytest.mark.asyncio
async def test_resolve_role_maps_the_admin_key_to_admin(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    assert await deps.resolve_role(x_api_key="admin-secret") == deps.Role.ADMIN


@pytest.mark.asyncio
async def test_resolve_role_maps_the_recruiter_key_to_recruiter(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_recruiter="recruiter-secret")
    )
    assert await deps.resolve_role(x_api_key="recruiter-secret") == deps.Role.RECRUITER


@pytest.mark.asyncio
async def test_resolve_role_maps_the_hiring_manager_key_to_hiring_manager(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_hiring_manager="hm-secret")
    )
    assert await deps.resolve_role(x_api_key="hm-secret") == deps.Role.HIRING_MANAGER


@pytest.mark.asyncio
async def test_resolve_role_maps_the_auditor_key_to_auditor(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_auditor="auditor-secret")
    )
    assert await deps.resolve_role(x_api_key="auditor-secret") == deps.Role.AUDITOR


@pytest.mark.asyncio
async def test_resolve_role_401s_an_unknown_key_when_enabled(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    with pytest.raises(HTTPException) as excinfo:
        await deps.resolve_role(x_api_key="totally-wrong")
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_role_401s_a_missing_key_when_enabled(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    with pytest.raises(HTTPException) as excinfo:
        await deps.resolve_role(x_api_key=None)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_role_401s_a_key_that_matches_no_configured_role(
    monkeypatch: Any,
) -> None:
    """A syntactically plausible key that just doesn't match ANY of the four
    configured secrets — distinct scenario from a totally-empty header."""
    from src.api import deps

    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: _settings(
            api_key_admin="admin-secret", api_key_recruiter="recruiter-secret"
        ),
    )
    with pytest.raises(HTTPException) as excinfo:
        await deps.resolve_role(x_api_key="hiring-manager-secret")
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_resolve_role_401s_not_500_on_a_non_ascii_key(monkeypatch: Any) -> None:
    """SEC-1 regression pin, carried forward under the new keyed-role
    resolution: Starlette latin-1-decodes header bytes, so a non-ASCII
    ``X-API-Key`` is a valid (non-ASCII) ``str``. ``secrets.compare_digest``
    on two ``str`` requires both ASCII-only or raises ``TypeError`` — must
    never propagate as an unhandled 500; always fails closed with 401."""
    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    with pytest.raises(HTTPException) as excinfo:
        await deps.resolve_role(x_api_key="clé-secrète-café")
    assert excinfo.value.status_code == 401


# ── resolve_role: disabled (all four empty) ─────────────────────────────


@pytest.mark.asyncio
async def test_resolve_role_disabled_always_resolves_admin_with_no_header(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings())
    assert await deps.resolve_role(x_api_key=None) == deps.Role.ADMIN


@pytest.mark.asyncio
async def test_resolve_role_disabled_always_resolves_admin_with_a_bogus_header(
    monkeypatch: Any,
) -> None:
    """Nothing that works today with auth off may change behavior — a bogus
    header is simply ignored, exactly as the retired ``require_api_key``
    ignored one when disabled."""
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings())
    assert await deps.resolve_role(x_api_key="anything-at-all") == deps.Role.ADMIN


# ── resolve_role: constant-time comparison preserved ────────────────────


@pytest.mark.asyncio
async def test_resolve_role_compares_via_secrets_compare_digest_not_eq(
    monkeypatch: Any,
) -> None:
    import secrets as real_secrets

    from src.api import deps

    monkeypatch.setattr(
        deps, "get_settings", lambda: _settings(api_key_admin="admin-secret")
    )
    spy = MagicMock(wraps=real_secrets.compare_digest)
    monkeypatch.setattr(deps.secrets, "compare_digest", spy)
    await deps.resolve_role(x_api_key="admin-secret")
    assert spy.called, "expected resolve_role to compare via secrets.compare_digest"
    # Every call must compare BYTES (never raw str), so a non-ASCII header
    # can never hit compare_digest's ASCII-only str/str path and raise.
    for call in spy.call_args_list:
        for arg in call.args:
            assert isinstance(arg, bytes)


@pytest.mark.asyncio
async def test_resolve_role_does_not_short_circuit_after_the_first_match(
    monkeypatch: Any,
) -> None:
    """``resolve_role`` must compare EVERY configured role key on every
    request, even after it has already found a match — never stop early.

    All four role keys are configured, and the presented key is
    ``api_key_admin``'s value: the FIRST candidate ``_configured_role_keys``
    yields. A short-circuit (``break``/early ``return`` right after the
    match) would stop the loop after exactly one ``secrets.compare_digest``
    call. Comparing all four regardless of where the match falls is what
    keeps neither response timing nor comparison count from leaking which
    role a near-miss guess was closest to (see ADR-018 §5)."""
    import secrets as real_secrets

    from src.api import deps

    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: _settings(
            api_key_admin="admin-secret",
            api_key_recruiter="recruiter-secret",
            api_key_hiring_manager="hiring-manager-secret",
            api_key_auditor="auditor-secret",
        ),
    )
    spy = MagicMock(wraps=real_secrets.compare_digest)
    monkeypatch.setattr(deps.secrets, "compare_digest", spy)
    result = await deps.resolve_role(x_api_key="admin-secret")
    assert result == deps.Role.ADMIN
    assert spy.call_count == 4, (
        "expected resolve_role to compare all four configured role keys "
        "even after matching the first one (no short-circuit)"
    )


# ── require_role: per-route allowed-role check ──────────────────────────


@pytest.mark.asyncio
async def test_require_role_returns_the_role_when_it_is_allowed() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN, deps.Role.RECRUITER)
    result = await checker(role=deps.Role.RECRUITER)
    assert result == deps.Role.RECRUITER


@pytest.mark.asyncio
async def test_require_role_allows_every_member_of_its_own_allowed_set() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN, deps.Role.RECRUITER)
    assert await checker(role=deps.Role.ADMIN) == deps.Role.ADMIN
    assert await checker(role=deps.Role.RECRUITER) == deps.Role.RECRUITER


@pytest.mark.asyncio
async def test_require_role_403s_a_role_outside_the_allowed_set() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN, deps.Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await checker(role=deps.Role.AUDITOR)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_403s_hiring_manager_too_when_not_allowed() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN, deps.Role.RECRUITER)
    with pytest.raises(HTTPException) as excinfo:
        await checker(role=deps.Role.HIRING_MANAGER)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_with_a_single_allowed_role_admits_only_that_role() -> None:
    from src.api import deps

    checker = deps.require_role(deps.Role.ADMIN)
    assert await checker(role=deps.Role.ADMIN) == deps.Role.ADMIN
    with pytest.raises(HTTPException) as excinfo:
        await checker(role=deps.Role.RECRUITER)
    assert excinfo.value.status_code == 403


@pytest.mark.asyncio
async def test_require_role_with_all_four_roles_admits_every_role() -> None:
    from src.api import deps

    all_roles = tuple(deps.Role)
    checker = deps.require_role(*all_roles)
    for role in all_roles:
        assert await checker(role=role) == role


# ── require_role composes with resolve_role (the override mechanism every
#    test_route_*.py file relies on) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_require_role_composes_with_resolve_role_via_dependency_override() -> (
    None
):
    """Proves the exact mechanism ``test_route_*.py``'s ``_build_app`` helpers
    use in place of the retired ``dependency_overrides[require_api_key] =
    lambda: None`` bypass: overriding the SHARED ``resolve_role`` dependency
    (never the route-specific ``require_role(...)`` closure) propagates
    through every route's role check."""
    from src.api import deps

    app = FastAPI()

    @app.get("/protected", dependencies=[Depends(deps.require_role(deps.Role.ADMIN))])
    async def _protected() -> dict[str, bool]:
        return {"ok": True}

    app.dependency_overrides[deps.resolve_role] = lambda: deps.Role.RECRUITER
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/protected")
    assert resp.status_code == 403

    app.dependency_overrides[deps.resolve_role] = lambda: deps.Role.ADMIN
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/protected")
    assert resp.status_code == 200


# ── no role key is ever logged ───────────────────────────────────────────


def test_resolve_role_never_logs_any_configured_role_key(
    caplog: pytest.LogCaptureFixture, monkeypatch: Any
) -> None:
    """Regression pin (decision: "No role key is ever logged"): whatever
    ``resolve_role`` DOES log (if anything) on a failed lookup must never
    include any of the configured secrets, nor the wrong key that was
    presented."""
    import asyncio

    from src.api import deps

    settings = _settings(
        api_key_admin="admin-secret-value",
        api_key_recruiter="recruiter-secret-value",
        api_key_hiring_manager="hm-secret-value",
        api_key_auditor="auditor-secret-value",
    )
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(HTTPException):
        asyncio.run(deps.resolve_role(x_api_key="an-attacker-supplied-guess"))

    joined = " ".join(r.getMessage() for r in caplog.records)
    for leaked in (
        "admin-secret-value",
        "recruiter-secret-value",
        "hm-secret-value",
        "auditor-secret-value",
        "an-attacker-supplied-guess",
    ):
        assert leaked not in joined


# ── resolve_actor is RETIRED (FU-5 slice 7, ADR-019 §8.3) ────────────────
#
# DELETED (not rewritten): the five Phase-6 ``resolve_actor`` regression
# tests that used to live here (``..._returns_the_header_value_when_present``,
# ``..._defaults_to_api_when_absent``, ``..._defaults_to_api_for_an_empty_
# header``, ``..._caps_an_overlong_header``,
# ``..._is_never_consulted_by_resolve_role_or_require_role``). ADR-019 §8.3:
# "the optional, unverified ``X-Actor-Name`` header ... is removed entirely
# ... A spoofable identity-shaped header next to a cryptographically verified
# one invites confusion and misconfiguration; removing ``X-Actor-Name``
# closes that risk." Keeping those tests around (even rewritten) would pin a
# function this ADR says must not exist. The positive replacement pin is
# below; behavioural coverage of the new identity source
# (``src.api.deps.resolve_user``) lives in ``test_route_auth.py`` (already
# landed, slice 6) and the route-level call-site tests added in
# ``test_route_jobs.py`` / ``test_route_jobs_bulk.py`` /
# ``test_route_resumes.py`` / ``test_route_reveal.py`` (slice 7).


def test_resolve_actor_no_longer_exists() -> None:
    """ADR-019 §8.3 — ``resolve_actor`` must not exist on ``src.api.deps`` at
    all, not merely be unused. A future re-introduction (e.g. a well-meaning
    revert, or a merge conflict resolution that resurrects it) fails this
    test immediately instead of silently reopening the spoofable-header risk
    the ADR closed."""
    from src.api import deps

    assert not hasattr(deps, "resolve_actor")


def test_x_actor_name_header_alias_appears_nowhere_in_deps_module_source() -> None:
    """Belt-and-suspenders on the same ADR-019 §8.3 decision: even a
    differently-named function must not still declare a FastAPI ``Header``
    bound to the ``X-Actor-Name`` wire name anywhere in this module."""
    import inspect

    from src.api import deps

    source = inspect.getsource(deps)
    assert "X-Actor-Name" not in source


# ── get_arq (Phase 6, unchanged) ─────────────────────────────────────────


def _request_for(app: FastAPI) -> MagicMock:
    request = MagicMock()
    request.app = app
    return request


def test_get_arq_returns_the_pool_from_app_state() -> None:
    from src.api.deps import get_arq

    app = FastAPI()
    sentinel = object()
    app.state.arq = sentinel
    assert get_arq(_request_for(app)) is sentinel


def test_get_arq_raises_a_clear_runtime_error_when_absent() -> None:
    from src.api.deps import get_arq

    app = FastAPI()
    with pytest.raises(RuntimeError, match="(?i)arq"):
        get_arq(_request_for(app))


# ── log_auth_mode — now driven by Settings.auth_enabled ─────────────────


def test_log_auth_mode_warns_loudly_when_all_four_keys_are_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings())
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected at least one WARNING-level log record"
    joined = " ".join(r.getMessage().upper() for r in warnings)
    assert "AUTH" in joined
    assert "DISABLED" in joined


@pytest.mark.parametrize(
    "field",
    [
        "api_key_admin",
        "api_key_recruiter",
        "api_key_hiring_manager",
        "api_key_auditor",
    ],
)
def test_log_auth_mode_does_not_warn_when_any_single_role_key_is_set(
    field: str, caplog: pytest.LogCaptureFixture
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings(**{field: "secret123"}))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []


def test_log_auth_mode_never_logs_a_configured_role_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.DEBUG)
    log_auth_mode(_settings(api_key_admin="super-secret-admin-key"))
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "super-secret-admin-key" not in joined


# ── resolve_user — sliding-window refresh wiring (FU-5 slice 12, ADR-019
#    §10 step 5) ─────────────────────────────────────────────────────────
#
# ``resolve_user`` currently resolves a live session via
# ``session_service.get_active_session`` and stops there — nothing on the
# request path ever calls ``session_service.refresh_if_needed`` (already
# implemented and unit-tested in isolation by
# ``tests/unit/test_session_service.py``), so ``settings.
# session_idle_refresh_hours`` is unread and every session hard-expires at
# the fixed TTL instead of sliding, contradicting ADR-019 §10 step 5 ("with
# a sliding-window refresh"). These tests spy on
# ``session_service.refresh_if_needed`` to pin the WIRING — that
# ``resolve_user`` calls it, with which conn/session/ttl/idle arguments, and
# when it must NOT be called at all. The real ``expires_at`` extension is
# genuine Postgres behaviour and is pinned separately in
# ``tests/integration/test_auth_routes_pg.py`` — a mocked spy here cannot
# prove the database write actually lands.


def _cas_settings(
    *, cas_enabled: bool = True, ttl_hours: int = 8, idle_hours: int = 1
) -> Settings:
    return Settings(
        cas_enabled=cas_enabled,
        session_ttl_hours=ttl_hours,
        session_idle_refresh_hours=idle_hours,
        skill_hash_salt="test-salt",
        pii_key="test-key",
    )


def _live_session() -> Any:
    from src.schemas.auth import Session

    return Session(
        id="tok-live-session",
        user_id=uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        revoked_at=None,
        user_agent=None,
        ip_addr=None,
        created_at=datetime.now(UTC),
    )


def _live_user(user_id: Any) -> Any:
    from src.schemas.auth import User

    now = datetime.now(UTC)
    return User(
        id=user_id,
        cas_username="alice",
        display_name=None,
        email=None,
        role="recruiter",
        active=True,
        created_at=now,
        last_seen_at=now,
    )


@pytest.mark.asyncio
async def test_resolve_user_calls_refresh_if_needed_when_a_session_resolves(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    settings = _cas_settings(ttl_hours=8, idle_hours=1)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    session = _live_session()
    user = _live_user(session.user_id)

    monkeypatch.setattr(
        deps.session_service, "get_active_session", AsyncMock(return_value=session)
    )
    refresh_spy = AsyncMock(return_value=session)
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)
    monkeypatch.setattr(deps.user_service, "get_by_id", AsyncMock(return_value=user))

    db = MagicMock()
    result = await deps.resolve_user(request=MagicMock(), db=db, ra_session=session.id)

    assert result is not None
    assert result.cas_username == "alice"
    refresh_spy.assert_awaited_once()
    call = refresh_spy.await_args
    assert call is not None
    assert call.args[0] is db
    assert call.args[1] is session
    assert call.kwargs["ttl_seconds"] == 8 * 3600
    assert call.kwargs["idle_refresh_seconds"] == 1 * 3600


@pytest.mark.parametrize(
    "ttl_hours,idle_hours",
    [(8, 1), (4, 2), (12, 3)],
)
@pytest.mark.asyncio
async def test_resolve_user_derives_refresh_seconds_from_settings_not_hardcoded(
    ttl_hours: int, idle_hours: int, monkeypatch: Any
) -> None:
    """``session_ttl_hours``/``session_idle_refresh_hours`` must be READ from
    settings on every call, never a hard-coded value baked into
    ``resolve_user`` — CLAUDE.md's "config only via src/settings.py"."""
    from src.api import deps

    settings = _cas_settings(ttl_hours=ttl_hours, idle_hours=idle_hours)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    session = _live_session()
    user = _live_user(session.user_id)

    monkeypatch.setattr(
        deps.session_service, "get_active_session", AsyncMock(return_value=session)
    )
    refresh_spy = AsyncMock(return_value=session)
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)
    monkeypatch.setattr(deps.user_service, "get_by_id", AsyncMock(return_value=user))

    await deps.resolve_user(request=MagicMock(), db=MagicMock(), ra_session=session.id)

    refresh_spy.assert_awaited_once()
    call = refresh_spy.await_args
    assert call is not None
    assert call.kwargs["ttl_seconds"] == ttl_hours * 3600
    assert call.kwargs["idle_refresh_seconds"] == idle_hours * 3600


@pytest.mark.asyncio
async def test_resolve_user_with_no_cookie_never_calls_refresh_if_needed(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    settings = _cas_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    refresh_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)
    get_active_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "get_active_session", get_active_spy)

    result = await deps.resolve_user(
        request=MagicMock(), db=MagicMock(), ra_session=None
    )

    assert result is None
    get_active_spy.assert_not_awaited()
    refresh_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_user_with_no_live_session_never_calls_refresh_if_needed(
    monkeypatch: Any,
) -> None:
    """A cookie present but resolving to nothing live (missing/revoked/
    expired — ``get_active_session`` returns ``None`` for all three,
    deliberately indistinguishable) must never reach ``refresh_if_needed``
    at all."""
    from src.api import deps

    settings = _cas_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    monkeypatch.setattr(
        deps.session_service, "get_active_session", AsyncMock(return_value=None)
    )
    refresh_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)

    result = await deps.resolve_user(
        request=MagicMock(), db=MagicMock(), ra_session="stale-or-revoked-token"
    )

    assert result is None
    refresh_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_user_dev_anonymous_path_never_touches_sessions_at_all(
    monkeypatch: Any,
) -> None:
    """CAS disabled: the synthetic dev-admin identity is returned without
    ever consulting a session — no ``get_active_session`` call, and
    therefore no ``refresh_if_needed`` call either. There is no session to
    slide."""
    from src.api import deps

    settings = _cas_settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    get_active_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "get_active_session", get_active_spy)
    refresh_spy = AsyncMock()
    monkeypatch.setattr(deps.session_service, "refresh_if_needed", refresh_spy)

    result = await deps.resolve_user(
        request=MagicMock(), db=MagicMock(), ra_session="whatever-cookie-value"
    )

    assert result is not None
    assert result.cas_username == "dev-anonymous"
    get_active_spy.assert_not_awaited()
    refresh_spy.assert_not_awaited()
