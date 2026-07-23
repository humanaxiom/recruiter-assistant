"""Route-level tests for FU-5 slice 6 — the CAS routes (``src.api.routes.auth``,
new module; does not exist yet) plus the cookie -> session -> user resolution
dependency (``src.api.deps.resolve_user``, new function; does not exist yet).

The whole file fails at collection (``ModuleNotFoundError`` /
``ImportError``) — RED half of the TDD cycle.

Scope boundary (slice 6 ONLY): the CAS routes, session-resolution dependency,
and dev-anonymous authz resolution per ADR-019 §10/§10b. Slice 6 does NOT
remove ``X-Actor-Name``/``resolve_actor`` (slice 7), does NOT write
``audit_log`` rows or touch reveal (slice 8), and does NOT touch the Flask
frontend (slice 11) — none of that is exercised here.

Uses a real ASGI app (a fresh ``FastAPI()``, not the whole ``src.api.main``
app) with ``httpx.AsyncClient`` + ``ASGITransport``, mirroring
``test_route_jobs.py``'s pattern: mock ``db`` via ``app.dependency_overrides``
targeting the shared ``get_db``, let the real service layer
(``cas_service``/``session_service``/``user_service``) run against a mocked
asyncpg-shaped connection double — no service-internals monkeypatching,
except the one genuinely-external boundary: CAS's own network call, which is
monkeypatched at ``auth_routes.cas_service.validate_ticket`` (an ``AsyncMock``
standing in for the real ``httpx`` round trip to ``cas.sfu.ca``).

**Ambiguities this file locks (the coder's implementation MUST match):**

* ``src.api.routes.auth`` exposes an ``APIRouter`` named ``router`` with
  ``prefix="/auth"`` — ``GET /auth/cas/login``, ``GET /auth/cas/validate``,
  ``GET /auth/cas/logout``, ``GET /auth/cas/user`` (GET for logout too,
  matching the ``hris`` port source). ``src.api.main`` mounts it with
  ``app.include_router(auth.router)`` — pinned separately by this red commit's
  edit to ``test_api.py``'s ``_PHASE_6_ROUTES`` set (ADR-019 §10 widens the
  route table; that assertion is a closed positive set and would otherwise
  wrongly still pass green with the new routes silently unregistered).
* Settings are read via a plain module-level ``get_settings()`` call inside
  the route functions — NOT ``Depends(get_settings)`` — mirroring
  ``resolve_role``'s existing convention in ``src.api.deps`` (every other
  settings consumer in this codebase, per ``grep``, calls ``get_settings()``
  directly; none uses ``Depends(get_settings)``). Tests here monkeypatch
  ``auth_routes.get_settings`` for the routes and ``deps.get_settings`` for
  the dependency.
* ``src.api.deps.resolve_user(request: Request, db: Db, ra_session: str |
  None = Cookie(default=None)) -> User | None`` — called DIRECTLY with
  keyword arguments in these tests (bypassing FastAPI's DI, exactly the way
  ``test_api_deps.py`` calls ``resolve_role``/``require_role`` directly). This
  is the ONE new dependency function slice 6 adds; it does not replace or
  modify ``resolve_role``/``require_role``/``resolve_actor``.
* CAS-disabled (``cas_enabled=False``) dev-anonymous resolution (§10b): the
  ROLE resolves to ``"admin"``. Whether the synthetic user is DB-backed
  (upserted, hris-style) or a plain in-memory object is explicitly left to
  the coder — these tests assert only the observable shape/role, never
  ``db.execute``/``db.fetchrow`` call counts for that branch.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import deps
from src.api.routes import auth as auth_routes
from src.models.pool import get_db
from src.settings import Settings

_NOW = dt.datetime(2026, 7, 22, tzinfo=dt.UTC)


class _Row(dict[str, Any]):
    """Dict double that behaves like an ``asyncpg.Record`` for ``row["x"]``
    AND for ``dict(row)`` — missing keys read as ``None`` rather than
    ``KeyError``, matching ``test_route_jobs.py``'s convention."""

    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


class _NullAsyncCtx:
    async def __aenter__(self) -> _NullAsyncCtx:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeAuthConn:
    """Minimal asyncpg-connection double for the auth routes.

    Dispatches ``fetchrow`` by which of the two fixed table names
    (``sessions`` / ``users`` — both fixed by the slice-1/slice-3 DDL, not by
    this route's SQL phrasing) appears in the query text, rather than
    string-matching an exact query. A query naming neither table returns
    ``None``.
    """

    def __init__(
        self,
        *,
        session_row: _Row | None = None,
        user_row: _Row | None = None,
    ) -> None:
        self.session_row = session_row
        self.user_row = user_row
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> _Row | None:
        self.fetchrow_calls.append((query, args))
        q = query.lower()
        if "sessions" in q:
            return self.session_row
        if "users" in q:
            return self.user_row
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return "OK"

    def transaction(self) -> _NullAsyncCtx:
        return _NullAsyncCtx()


def _settings(
    *,
    cas_enabled: bool = True,
    cas_server_url: str = "https://cas.example.edu/cas",
    cas_validate_route: str = "/serviceValidate",
    session_cookie_name: str = "ra_session",
    default_admin_cas_username: str = "asalah",
) -> Settings:
    return Settings(
        cas_enabled=cas_enabled,
        cas_server_url=cas_server_url,
        cas_validate_route=cas_validate_route,
        session_cookie_name=session_cookie_name,
        default_admin_cas_username=default_admin_cas_username,
        skill_hash_salt="test-salt",
        pii_key="test-key",
    )


def _session_row(*, id: str = "tok-abc123", user_id: UUID | None = None) -> _Row:
    return _Row(
        {
            "id": id,
            "user_id": user_id or uuid4(),
            "expires_at": _NOW + dt.timedelta(hours=8),
            "revoked_at": None,
            "user_agent": "pytest",
            "ip_addr": None,
            "created_at": _NOW,
        }
    )


def _user_row(
    *,
    id: UUID | None = None,
    cas_username: str = "alice",
    role: str = "recruiter",
) -> _Row:
    return _Row(
        {
            "id": id or uuid4(),
            "cas_username": cas_username,
            "display_name": None,
            "email": None,
            "role": role,
            "active": True,
            "created_at": _NOW,
            "last_seen_at": _NOW,
        }
    )


def _build_app(conn: _FakeAuthConn) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── GET /auth/cas/login ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cas_login_redirects_to_cas_server_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, cas_server_url="https://cas.example.edu/cas")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs"})
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://cas.example.edu/cas/login?")
    outer_qs = parse_qs(urlparse(location).query)
    assert "service" in outer_qs, "login redirect must carry a service= param"


@pytest.mark.asyncio
async def test_cas_login_service_url_round_trips_the_next_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, cas_server_url="https://cas.example.edu/cas")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs/42"})
    location = resp.headers["location"]
    outer_qs = parse_qs(urlparse(location).query)
    service_url = outer_qs["service"][0]
    # The service URL itself must point back at our own /cas/validate and
    # carry the caller's `next` — decode its own query string in turn.
    service_parsed = urlparse(service_url)
    assert service_parsed.path.endswith("/auth/cas/validate")
    service_qs = parse_qs(service_parsed.query)
    assert service_qs.get("next") == ["/jobs/42"]


@pytest.mark.asyncio
async def test_cas_login_dev_passthrough_when_cas_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/jobs"})
    assert resp.status_code == 302
    assert resp.headers["location"] == "/jobs"


@pytest.mark.asyncio
async def test_cas_login_dev_passthrough_never_redirects_to_a_cas_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        cas_enabled=False, cas_server_url="https://cas.example.edu/cas"
    )
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/login", params={"next": "/"})
    assert "cas.example.edu" not in resp.headers["location"]


# ── GET /auth/cas/validate ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cas_validate_with_no_ticket_redirects_back_to_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/validate")
    assert resp.status_code in (302, 303, 307)
    assert "/auth/cas/login" in resp.headers["location"]


@pytest.mark.asyncio
async def test_cas_validate_with_no_ticket_never_calls_validate_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    spy = AsyncMock()
    monkeypatch.setattr(auth_routes.cas_service, "validate_ticket", spy)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        await client.get("/auth/cas/validate", params={"next": "/jobs"})
    spy.assert_not_called()


# ── GET /auth/cas/user ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cas_user_anonymous_never_401s(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is False
    assert body["cas_enabled"] is True
    assert "username" in body


@pytest.mark.asyncio
async def test_cas_user_dev_anonymous_mode_never_401s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    app = _build_app(_FakeAuthConn())
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cas_enabled"] is False


@pytest.mark.asyncio
async def test_cas_user_returns_the_logged_in_username_from_a_valid_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, session_cookie_name="ra_session")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    user_id = uuid4()
    conn = _FakeAuthConn(
        session_row=_session_row(id="tok-live", user_id=user_id),
        user_row=_user_row(id=user_id, cas_username="alice"),
    )
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get("/auth/cas/user", cookies={"ra_session": "tok-live"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["username"] == "alice"


@pytest.mark.asyncio
async def test_cas_user_never_401s_for_an_expired_or_unknown_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True, session_cookie_name="ra_session")
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    conn = _FakeAuthConn(session_row=None)  # no active session found
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/user", cookies={"ra_session": "bogus-or-expired"}
        )
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False


# ── src.api.deps.resolve_user ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_user_dev_anonymous_resolves_a_synthetic_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-019 §10b — CAS disabled: every request resolves an admin identity
    for AUTHORIZATION purposes. This slice writes no audit rows at all."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    conn = _FakeAuthConn()

    user = await deps.resolve_user(request=request, db=conn, ra_session=None)

    assert user is not None
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_resolve_user_dev_anonymous_ignores_any_presented_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS-disabled mode short-circuits before any cookie/session lookup —
    a bogus cookie must not change the outcome."""
    settings = _settings(cas_enabled=False)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    conn = _FakeAuthConn()

    user = await deps.resolve_user(
        request=request, db=conn, ra_session="whatever-this-is-ignored"
    )

    assert user is not None
    assert user.role == "admin"


@pytest.mark.asyncio
async def test_resolve_user_returns_none_with_no_session_cookie_when_cas_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    conn = _FakeAuthConn()

    user = await deps.resolve_user(request=request, db=conn, ra_session=None)

    assert user is None


@pytest.mark.asyncio
async def test_resolve_user_returns_none_for_an_expired_or_unknown_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    conn = _FakeAuthConn(session_row=None)

    user = await deps.resolve_user(
        request=request, db=conn, ra_session="stale-or-forged-token"
    )

    assert user is None


@pytest.mark.asyncio
async def test_resolve_user_resolves_the_real_user_for_a_live_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(cas_enabled=True)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    request = MagicMock()
    user_id = uuid4()
    conn = _FakeAuthConn(
        session_row=_session_row(id="tok-live", user_id=user_id),
        user_row=_user_row(id=user_id, cas_username="bob", role="admin"),
    )

    user = await deps.resolve_user(request=request, db=conn, ra_session="tok-live")

    assert user is not None
    assert user.cas_username == "bob"
    assert user.role == "admin"
