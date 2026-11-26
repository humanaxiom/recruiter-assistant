"""S1a (job-page redesign) -- the per-user job page preview column, against a
REAL Postgres (testcontainers) through a real ASGI app and real CAS cookies.

RED: ``users.job_page_v2`` does not exist, ``GET /auth/cas/user`` and
``GET /users`` do not carry it, and ``PATCH /users/{id}/job-page-preview`` is
not registered.

Pinned contract (the coder implements exactly this):

* Schema: ``users.job_page_v2 BOOLEAN NOT NULL DEFAULT false`` after
  ``init_schema``, on a fresh database AND on one whose ``users`` table predates
  the column; idempotent. An INSERT that omits it (a first CAS login) reads false
  (the FU-5 lesson: NOT NULL without a DEFAULT broke the first real INSERT).
* ``GET /auth/cas/user`` for a signed-in user carries ``job_page_v2: bool``.
* ``GET /users`` items carry ``job_page_v2: bool`` (the admin page renders the
  current state from it).
* ``PATCH /users/{user_id}/job-page-preview`` with JSON ``{"enabled": bool}``,
  admin session only, returns the updated user (200). Unknown user is 404
  (``code == "resource.not_found"``), a non-bool or missing ``enabled`` is 422.
* Each CHANGE writes exactly one ``audit_log`` row: ``action ==
  "job_page_preview_changed"``, ``subject_type == "user"``, ``subject_id`` the
  target, the acting admin as actor, ``details == {"old_value": bool,
  "new_value": bool}`` and nothing else (no username, email or display name). A
  request that does not change the value is a no-op: 200, no audit row.
* The UPDATE and the audit write are atomic.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

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
from src.settings import Settings

DEFAULT_ADMIN = "asalah"
RECRUITER_KEY = "shared-recruiter-key-value-job-page-preview"
ACTION = "job_page_preview_changed"

_LEGACY_USERS_DDL = """
CREATE TABLE users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cas_username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    email        TEXT,
    role         TEXT,
    active       BOOLEAN NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


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
        "api_key_recruiter": RECRUITER_KEY,
        "skill_hash_salt": "test-salt",
        "pii_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    from src.api.routes import users as users_routes

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(users_routes.router)

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


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _login(
    client: AsyncClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    username: str,
) -> str:
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", AsyncMock(return_value=username)
    )
    resp = await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None
    return sid


def _wire(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = _settings()
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    return settings


async def _insert_user(
    pool: asyncpg.Pool,
    *,
    cas_username: str,
    role: str | None,
    display_name: str | None = "Seeded Person",
    email: str | None = "seeded@example.org",
) -> uuid.UUID:
    async with pool.acquire() as conn:
        user_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO users (cas_username, display_name, email, role) "
            "VALUES ($1, $2, $3, $4) RETURNING id",
            cas_username,
            display_name,
            email,
            role,
        )
    return user_id


async def _seed_role(pool: asyncpg.Pool, username: str, role: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET role = $1 WHERE cas_username = $2", role, username
        )


async def _flag(pool: asyncpg.Pool, user_id: uuid.UUID) -> bool:
    async with pool.acquire() as conn:
        value: bool = await conn.fetchval(
            "SELECT job_page_v2 FROM users WHERE id = $1", user_id
        )
    return value


async def _set_flag(pool: asyncpg.Pool, user_id: uuid.UUID, value: bool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET job_page_v2 = $2 WHERE id = $1", user_id, value
        )


async def _audit_rows(pool: asyncpg.Pool, user_id: uuid.UUID) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_kind, actor_user_id, actor_service, action, "
            "subject_type, subject_id, details::text AS details_text, details "
            "FROM audit_log WHERE subject_id = $1 AND action = $2",
            user_id,
            ACTION,
        )


def _details(row: asyncpg.Record) -> Any:
    raw = row["details"]
    return json.loads(raw) if isinstance(raw, str) else raw


async def _uid(pool: asyncpg.Pool, username: str) -> uuid.UUID:
    async with pool.acquire() as conn:
        value: uuid.UUID = await conn.fetchval(
            "SELECT id FROM users WHERE cas_username = $1", username
        )
    return value


def _path(user_id: uuid.UUID) -> str:
    return f"/users/{user_id}/job-page-preview"


# ── 1. schema ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_column_exists_not_null_default_false_on_a_fresh_database_pg(
    pg_pool: asyncpg.Pool,
) -> None:
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'job_page_v2'"
        )
    assert row is not None, "users.job_page_v2 must exist after init_schema"
    assert row["data_type"] == "boolean"
    assert row["is_nullable"] == "NO"
    assert "false" in str(row["column_default"]).lower()


@pytest.mark.asyncio
async def test_an_insert_that_omits_the_column_succeeds_and_reads_false_pg(
    pg_pool: asyncpg.Pool,
) -> None:
    """The FU-5 lesson: a first CAS login omits every column it does not know."""
    async with pg_pool.acquire() as conn:
        value = await conn.fetchval(
            "INSERT INTO users (cas_username) VALUES ($1) RETURNING job_page_v2",
            _unique("first-login"),
        )
    assert value is False


@pytest.mark.asyncio
async def test_a_real_first_cas_login_reads_false_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    username = _unique("fresh-face")
    app = _build_app(pg_pool)
    async with _client(app) as client:
        await _login(client, settings, monkeypatch, username)
    assert await _flag(pg_pool, await _uid(pg_pool, username)) is False


@pytest.mark.asyncio
async def test_init_schema_is_idempotent_and_keeps_a_set_value_pg(
    pg_pool: asyncpg.Pool,
) -> None:
    user_id = await _insert_user(pg_pool, cas_username=_unique("keep"), role=None)
    await _set_flag(pg_pool, user_id, True)
    await init_schema(pg_pool)
    await init_schema(pg_pool)
    assert await _flag(pg_pool, user_id) is True


@pytest.mark.asyncio
async def test_a_users_table_that_predates_the_column_is_upgraded_in_place_pg(
    pg_dsn: str,
) -> None:
    """A deployed database already has ``users`` rows and no column. The startup
    DDL must add it with every existing row reading false."""
    admin = await asyncpg.connect(dsn=pg_dsn)
    db_name = f"legacy_{uuid.uuid4().hex[:8]}"
    try:
        await admin.execute(f"CREATE DATABASE {db_name}")
    finally:
        await admin.close()
    legacy_dsn = pg_dsn.rsplit("/", 1)[0] + f"/{db_name}"
    conn = await asyncpg.connect(dsn=legacy_dsn)
    try:
        await conn.execute(_LEGACY_USERS_DDL)
        await conn.execute(
            "INSERT INTO users (cas_username, role) VALUES ('old-timer', 'admin')"
        )
        await init_schema(conn)
        await init_schema(conn)
        row = await conn.fetchrow(
            "SELECT job_page_v2, role FROM users WHERE cas_username = 'old-timer'"
        )
        nullable = await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'job_page_v2'"
        )
    finally:
        await conn.close()
    assert row is not None
    assert row["job_page_v2"] is False
    assert row["role"] == "admin"
    assert nullable == "NO"


# ── 2. CAS status payload and the admin listing ────────────────────────────


@pytest.mark.parametrize("value", [False, True])
@pytest.mark.asyncio
async def test_cas_user_payload_reflects_the_column_pg(
    value: bool, pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    username = _unique("status")
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, username)
        await _set_flag(pg_pool, await _uid(pg_pool, username), value)
        resp = await client.get(
            "/auth/cas/user", cookies={settings.session_cookie_name: sid}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["username"] == username
    assert body["job_page_v2"] is value
    assert isinstance(body["job_page_v2"], bool)


@pytest.mark.asyncio
async def test_admin_listing_carries_the_column_per_user_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    on_id = await _insert_user(pg_pool, cas_username=_unique("on"), role="recruiter")
    off_id = await _insert_user(pg_pool, cas_username=_unique("off"), role="recruiter")
    await _set_flag(pg_pool, on_id, True)
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.get("/users", cookies={settings.session_cookie_name: sid})
    assert resp.status_code == 200
    by_id = {u["id"]: u for u in resp.json()}
    assert by_id[str(on_id)]["job_page_v2"] is True
    assert by_id[str(off_id)]["job_page_v2"] is False


# ── 3. admin route ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.asyncio
async def test_admin_changes_the_flag_and_one_audit_row_is_written_pg(
    enabled: bool, pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    target_id = await _insert_user(
        pg_pool,
        cas_username=_unique("target"),
        role="recruiter",
        display_name="Tara Target",
        email="tara.target@example.org",
    )
    await _set_flag(pg_pool, target_id, not enabled)

    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.patch(
            _path(target_id),
            json={"enabled": enabled},
            cookies={settings.session_cookie_name: sid},
        )

    assert resp.status_code == 200
    assert resp.json()["id"] == str(target_id)
    assert resp.json()["job_page_v2"] is enabled
    assert await _flag(pg_pool, target_id) is enabled

    rows = await _audit_rows(pg_pool, target_id)
    assert len(rows) == 1, "exactly one audit_log row per change"
    row = rows[0]
    assert row["actor_kind"] == "user"
    assert row["actor_user_id"] == await _uid(pg_pool, DEFAULT_ADMIN)
    assert row["actor_service"] is None
    assert row["subject_type"] == "user"
    assert row["subject_id"] == target_id
    assert _details(row) == {"old_value": not enabled, "new_value": enabled}
    # No PII beyond what role_changed rows carry: nothing about who the target is.
    blob = str(row["details_text"]).lower()
    for pii in ("tara", "target", "example.org", "seeded"):
        assert pii not in blob


@pytest.mark.asyncio
async def test_a_second_different_change_writes_a_second_row_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    target_id = await _insert_user(
        pg_pool, cas_username=_unique("target"), role="recruiter"
    )
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, DEFAULT_ADMIN)
        cookies = {settings.session_cookie_name: sid}
        on = await client.patch(
            _path(target_id), json={"enabled": True}, cookies=cookies
        )
        off = await client.patch(
            _path(target_id), json={"enabled": False}, cookies=cookies
        )
    assert on.status_code == 200 and off.status_code == 200
    assert await _flag(pg_pool, target_id) is False
    assert len(await _audit_rows(pg_pool, target_id)) == 2


@pytest.mark.asyncio
async def test_setting_the_value_it_already_has_is_a_no_op_without_an_audit_row_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    target_id = await _insert_user(
        pg_pool, cas_username=_unique("target"), role="recruiter"
    )
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.patch(
            _path(target_id),
            json={"enabled": False},
            cookies={settings.session_cookie_name: sid},
        )
    assert resp.status_code == 200
    assert resp.json()["job_page_v2"] is False
    assert await _audit_rows(pg_pool, target_id) == []


@pytest.mark.asyncio
async def test_an_admin_may_set_the_flag_on_their_own_row_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, DEFAULT_ADMIN)
        admin_id = await _uid(pg_pool, DEFAULT_ADMIN)
        resp = await client.patch(
            _path(admin_id),
            json={"enabled": True},
            cookies={settings.session_cookie_name: sid},
        )
    assert resp.status_code == 200
    assert await _flag(pg_pool, admin_id) is True
    assert len(await _audit_rows(pg_pool, admin_id)) == 1


@pytest.mark.parametrize("role", ["recruiter", "hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_non_admin_roles_get_403_and_nothing_changes_pg(
    role: str, pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    actor = _unique(f"actor-{role}")
    target_id = await _insert_user(
        pg_pool, cas_username=_unique("target"), role="recruiter"
    )
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, actor)
        await _seed_role(pg_pool, actor, role)
        resp = await client.patch(
            _path(target_id),
            json={"enabled": True},
            cookies={settings.session_cookie_name: sid},
        )
    assert resp.status_code == 403
    assert await _flag(pg_pool, target_id) is False
    assert await _audit_rows(pg_pool, target_id) == []


@pytest.mark.asyncio
async def test_a_no_role_session_gets_403_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    target_id = await _insert_user(
        pg_pool, cas_username=_unique("target"), role="recruiter"
    )
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, _unique("nobody"))
        resp = await client.patch(
            _path(target_id),
            json={"enabled": True},
            cookies={settings.session_cookie_name: sid},
        )
    assert resp.status_code == 403
    assert await _flag(pg_pool, target_id) is False


@pytest.mark.asyncio
async def test_a_bare_api_key_with_no_session_gets_403_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire(monkeypatch)
    target_id = await _insert_user(
        pg_pool, cas_username=_unique("target"), role="recruiter"
    )
    app = _build_app(pg_pool)
    async with _client(app) as client:
        resp = await client.patch(
            _path(target_id),
            json={"enabled": True},
            headers={"X-API-Key": RECRUITER_KEY},
        )
    assert resp.status_code == 403
    assert await _flag(pg_pool, target_id) is False
    assert await _audit_rows(pg_pool, target_id) == []


@pytest.mark.asyncio
async def test_an_unknown_user_is_404_with_the_app_error_envelope_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.patch(
            _path(uuid.uuid4()),
            json={"enabled": True},
            cookies={settings.session_cookie_name: sid},
        )
    assert resp.status_code == 404
    assert resp.json().get("code") == "resource.not_found"


@pytest.mark.parametrize(
    "body",
    [{}, {"enabled": None}, {"enabled": "maybe"}, {"enabled": [True]}, {"on": True}],
)
@pytest.mark.asyncio
async def test_a_malformed_body_is_422_and_changes_nothing_pg(
    body: dict[str, Any], pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    target_id = await _insert_user(
        pg_pool, cas_username=_unique("target"), role="recruiter"
    )
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.patch(
            _path(target_id),
            json=body,
            cookies={settings.session_cookie_name: sid},
        )
    assert resp.status_code == 422
    assert await _flag(pg_pool, target_id) is False
    assert await _audit_rows(pg_pool, target_id) == []


@pytest.mark.asyncio
async def test_audit_write_failure_rolls_the_flag_change_back_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api.routes import users as users_routes

    settings = _wire(monkeypatch)
    monkeypatch.setattr(
        users_routes.audit_service,
        "record_audit",
        AsyncMock(side_effect=RuntimeError("simulated audit-write crash")),
    )
    target_id = await _insert_user(
        pg_pool, cas_username=_unique("target"), role="recruiter"
    )
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, DEFAULT_ADMIN)
        with pytest.raises(RuntimeError):
            await client.patch(
                _path(target_id),
                json={"enabled": True},
                cookies={settings.session_cookie_name: sid},
            )
    assert await _flag(pg_pool, target_id) is False
    assert await _audit_rows(pg_pool, target_id) == []


@pytest.mark.asyncio
async def test_changing_the_flag_does_not_touch_the_role_pg(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _wire(monkeypatch)
    target_id = await _insert_user(
        pg_pool, cas_username=_unique("target"), role="auditor"
    )
    app = _build_app(pg_pool)
    async with _client(app) as client:
        sid = await _login(client, settings, monkeypatch, DEFAULT_ADMIN)
        resp = await client.patch(
            _path(target_id),
            json={"enabled": True},
            cookies={settings.session_cookie_name: sid},
        )
    assert resp.status_code == 200
    async with pg_pool.acquire() as conn:
        role = await conn.fetchval("SELECT role FROM users WHERE id = $1", target_id)
        role_audits = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'role_changed' "
            "AND subject_id = $1",
            target_id,
        )
    assert role == "auditor"
    assert role_audits == 0
