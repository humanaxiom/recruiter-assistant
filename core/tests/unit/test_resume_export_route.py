"""RED -- slice H: role gates of ``GET /jobs/{id}/resumes/export.zip``.

Router expected at ``src.api.routes.resume_export`` (``router``), writer roles
(admin / recruiter) on BOTH the API-key role and the CAS session role -- the
same gate as the split routes. The behaviour behind the gate is pinned by
``tests/integration/test_resume_export_pg.py``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, resolve_role, resolve_user
from src.models.pool import get_db
from src.schemas.auth import User
from src.storage.blob_store import get_blob_store

_NOW = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)


def _user(role: str) -> User:
    return User(
        id=uuid4(),
        cas_username="priya",
        display_name="priya",
        email=None,
        role=role,
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


def _app(key_role: Role) -> tuple[FastAPI, MagicMock, MagicMock]:
    from src.api.routes import resume_export  # RED: module does not exist yet

    conn = MagicMock(name="conn")
    for fn in ("fetchrow", "fetch", "fetchval", "execute"):
        setattr(conn, fn, AsyncMock(return_value=None))
    store = MagicMock(name="blob_store")
    store.get = AsyncMock(return_value=b"")
    store.put = AsyncMock()
    app = FastAPI()
    app.include_router(resume_export.router)

    async def _db() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[resolve_role] = lambda: key_role
    return app, conn, store


async def _get(app: FastAPI) -> int:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes/export.zip")
    return resp.status_code


@pytest.mark.parametrize("role", [Role.HIRING_MANAGER, Role.AUDITOR])
@pytest.mark.asyncio
async def test_non_writer_api_key_is_403_and_touches_nothing(role: Role) -> None:
    app, conn, store = _app(role)

    assert await _get(app) == 403
    conn.fetchrow.assert_not_awaited()
    conn.fetch.assert_not_awaited()
    conn.execute.assert_not_awaited()
    store.get.assert_not_awaited()


@pytest.mark.parametrize("session_role", ["hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_non_writer_session_is_403_and_touches_nothing(
    session_role: str,
) -> None:
    app, conn, store = _app(Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(session_role)

    assert await _get(app) == 403
    conn.fetch.assert_not_awaited()
    conn.execute.assert_not_awaited()
    store.get.assert_not_awaited()


@pytest.mark.parametrize("session_role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_writer_session_passes_the_gate(session_role: str) -> None:
    app, _conn, _store = _app(Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(session_role)

    # The mock DB knows no job, so the gate passing shows up as a 404 here.
    assert await _get(app) == 404


@pytest.mark.parametrize("role", [Role.ADMIN, Role.RECRUITER])
@pytest.mark.asyncio
async def test_writer_api_key_passes_the_gate(role: Role) -> None:
    app, _conn, _store = _app(role)

    assert await _get(app) == 404


def test_router_is_mounted_in_the_main_app_behind_role_assignment() -> None:
    from src.api.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/jobs/{job_id}/resumes/export.zip" in paths
