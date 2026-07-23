"""Integration tests — FU-5 slice 6's CAS routes against a REAL Postgres
(testcontainers) via a real ASGI app (``app.dependency_overrides[get_db]``
routed to a real pooled ``asyncpg`` connection), the real
``session_service``/``user_service`` from slice 5, and the real ``users`` /
``sessions`` DDL from slices 1/3. The one boundary mocked is CAS's own
network call — ``auth_routes.cas_service.validate_ticket`` is monkeypatched
to an ``AsyncMock`` returning a canned username, standing in for the real
round trip to ``cas.sfu.ca``.

Nothing here exists yet (``core/src/api/routes/auth.py``,
``core/src/api/deps.py::resolve_user``), so every test below fails at
collection or the first request. RED half of the TDD cycle.

What a REAL Postgres proves that ``test_route_auth.py``'s mocked-conn route
tests structurally cannot:

* ``GET /auth/cas/validate``'s happy path REALLY inserts a ``users`` row
  (role picked per ADR-019 §10a's default-admin allowlist) and a REAL
  ``sessions`` row satisfying the FK — not just that the route called some
  function with plausible arguments,
* the ``Set-Cookie`` response header really carries an opaque session id that
  really resolves, end-to-end, back through ``resolve_user`` to that same
  real ``users`` row,
* ``GET /auth/cas/logout`` really flips ``sessions.revoked_at`` so a REAL
  subsequent ``get_active_session`` lookup returns ``None`` — a mocked
  connection cannot observe a revoke actually taking effect against the
  ``WHERE revoked_at IS NULL AND expires_at > now()`` predicate.

Follows the exact asyncpg/testcontainers fixture wiring already used in
``tests/integration/test_api_jobs_pg.py`` and
``tests/integration/test_session_user_service_pg.py`` — no new harness.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api import deps
from src.api.routes import auth as auth_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.services import session_service
from src.settings import Settings

DEFAULT_ADMIN = "asalah"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE sessions, users, audit_log, jobs, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "cas_enabled": True,
        "cas_server_url": "https://cas.example.edu/cas",
        "session_cookie_name": "ra_session",
        "session_ttl_hours": 8,
        "default_admin_cas_username": DEFAULT_ADMIN,
        "skill_hash_salt": "test-salt",
        "pii_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def _unique_username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _mock_validate_ticket(username: str) -> AsyncMock:
    """Stands in for the real ``httpx`` round trip to the CAS server — the
    one genuinely-external boundary these tests mock."""
    return AsyncMock(return_value=username)


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── GET /auth/cas/validate — happy path against real Postgres ─────────────


@pytest.mark.asyncio
async def test_cas_validate_happy_path_creates_a_real_admin_user_and_session(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        auth_routes.cas_service,
        "validate_ticket",
        _mock_validate_ticket(DEFAULT_ADMIN),
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get(
            "/auth/cas/validate", params={"ticket": "any-ticket", "next": "/jobs"}
        )

    assert resp.status_code == 302
    assert resp.headers["location"] == "/jobs"
    set_cookie = resp.headers.get("set-cookie", "")
    assert settings.session_cookie_name in set_cookie
    assert "httponly" in set_cookie.lower()

    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None

    async with pg_pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, role FROM users WHERE cas_username = $1", DEFAULT_ADMIN
        )
    assert user_row is not None
    assert user_row["role"] == "admin", "asalah is the ADR-019 §10a default admin"

    async with pg_pool.acquire() as conn:
        session_row = await conn.fetchrow(
            "SELECT user_id, revoked_at FROM sessions WHERE id = $1", sid
        )
    assert session_row is not None
    assert session_row["user_id"] == user_row["id"]
    assert session_row["revoked_at"] is None


@pytest.mark.asyncio
async def test_cas_validate_for_a_non_default_username_creates_a_recruiter_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-019 §10a through the real route — a username other than the
    configured default admin is provisioned ``role='recruiter'``."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    username = _unique_username("erin")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})

    assert resp.status_code == 302
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role FROM users WHERE cas_username = $1", username
        )
    assert row is not None
    assert row["role"] == "recruiter"


# ── cookie -> session -> user, end-to-end through resolve_user ────────────


@pytest.mark.asyncio
async def test_resolve_user_resolves_the_real_user_from_the_validate_response_cookie(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    username = _unique_username("carol")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        resp = await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None

    async with pg_pool.acquire() as conn:
        request = MagicMock()
        user = await deps.resolve_user(request=request, db=conn, ra_session=sid)

    assert user is not None
    assert user.cas_username == username
    assert user.role == "recruiter"


@pytest.mark.asyncio
async def test_cas_user_endpoint_reflects_the_real_session_after_validate(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same chain, proved through the public ``/auth/cas/user`` status route
    instead of calling the dependency directly — belt and suspenders."""
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    username = _unique_username("dana")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
        status_resp = await client.get("/auth/cas/user")

    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["authenticated"] is True
    assert body["username"] == username


# ── GET /auth/cas/logout — revokes the real session row ───────────────────


@pytest.mark.asyncio
async def test_logout_revokes_the_session_so_it_no_longer_resolves(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    username = _unique_username("frank")
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", _mock_validate_ticket(username)
    )
    app = _build_app(pg_pool)

    async with await _client(app) as client:
        validate_resp = await client.get(
            "/auth/cas/validate", params={"ticket": "any-ticket"}
        )
        sid = validate_resp.cookies.get(settings.session_cookie_name)
        assert sid is not None

        logout_resp = await client.get(
            "/auth/cas/logout", cookies={settings.session_cookie_name: sid}
        )

    assert logout_resp.status_code in (200, 302, 303, 307)

    async with pg_pool.acquire() as conn:
        got = await session_service.get_active_session(conn, sid)
    assert got is None, "a logged-out session must never resolve as active again"

    async with pg_pool.acquire() as conn:
        revoked_at = await conn.fetchval(
            "SELECT revoked_at FROM sessions WHERE id = $1", sid
        )
    assert revoked_at is not None
