"""RED -- slice H3: role gates and mounting of the background-export routes.

Router stays at ``src.api.routes.resume_export`` (``router``). Routes pinned::

    POST /jobs/{job_id}/resumes/exports            202 {"id", "status"}; 404; 422
    GET  /jobs/{job_id}/resumes/exports/latest     the requester's newest, or 404
    GET  /resume-exports/{export_id}               status + counts (requester/admin)
    POST /resume-exports/{export_id}/download      the ZIP (requester only)

All four are admin/recruiter on BOTH the API-key role and the CAS session role,
and the gates run BEFORE any DB, blob or queue access. The old synchronous
``GET /jobs/{job_id}/resumes/export.zip`` is gone. Behaviour behind the gates
is pinned by ``tests/integration/test_resume_export_jobs_pg.py``.
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

from src.api.deps import Role, get_arq, resolve_role, resolve_user
from src.models.pool import get_db
from src.schemas.auth import User
from src.storage.blob_store import get_blob_store

_NOW = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)

ROUTES: list[tuple[str, str]] = [
    ("POST", "/jobs/{id}/resumes/exports"),
    ("GET", "/jobs/{id}/resumes/exports/latest"),
    ("GET", "/resume-exports/{id}"),
    ("POST", "/resume-exports/{id}/download"),
]


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


def _app(key_role: Role) -> tuple[FastAPI, MagicMock, MagicMock, MagicMock]:
    from src.api.routes import resume_export

    conn = MagicMock(name="conn")
    for fn in ("fetchrow", "fetch", "fetchval", "execute"):
        setattr(conn, fn, AsyncMock(return_value=None))
    store = MagicMock(name="blob_store")
    store.get = AsyncMock(return_value=b"")
    store.put = AsyncMock()
    arq = MagicMock(name="arq")
    arq.enqueue_job = AsyncMock()
    app = FastAPI()
    app.include_router(resume_export.router)

    async def _db() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_arq] = lambda: arq
    app.dependency_overrides[resolve_role] = lambda: key_role
    return app, conn, store, arq


async def _call(app: FastAPI, method: str, template: str) -> int:
    path = template.replace("{id}", str(uuid4()))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.request(method, path)
    return resp.status_code


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize("role", [Role.HIRING_MANAGER, Role.AUDITOR])
@pytest.mark.asyncio
async def test_non_writer_api_key_is_403_and_touches_nothing(
    role: Role, method: str, path: str
) -> None:
    app, conn, store, arq = _app(role)

    assert await _call(app, method, path) == 403
    for fn in (conn.fetchrow, conn.fetch, conn.fetchval, conn.execute):
        fn.assert_not_awaited()
    store.get.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize("session_role", ["hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_non_writer_session_is_403_and_touches_nothing(
    session_role: str, method: str, path: str
) -> None:
    app, conn, store, arq = _app(Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(session_role)

    assert await _call(app, method, path) == 403
    for fn in (conn.fetchrow, conn.fetch, conn.fetchval, conn.execute):
        fn.assert_not_awaited()
    store.get.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize("session_role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_writer_session_passes_the_gate(
    session_role: str, method: str, path: str
) -> None:
    app, _conn, _store, arq = _app(Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(session_role)

    # The mock DB knows no job or export, so passing the gate shows up as 404.
    assert await _call(app, method, path) == 404
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.parametrize(("method", "path"), ROUTES)
@pytest.mark.parametrize("role", [Role.ADMIN, Role.RECRUITER])
@pytest.mark.asyncio
async def test_writer_api_key_passes_the_gate(
    role: Role, method: str, path: str
) -> None:
    app, _conn, _store, _arq = _app(role)

    assert await _call(app, method, path) == 404


def test_the_four_routes_are_mounted_and_the_synchronous_one_is_gone() -> None:
    from src.api.main import app
    from tests.unit.test_api import _all_route_paths

    paths = _all_route_paths(app.routes)

    assert {
        "/jobs/{job_id}/resumes/exports",
        "/jobs/{job_id}/resumes/exports/latest",
        "/resume-exports/{export_id}",
        "/resume-exports/{export_id}/download",
    } <= paths
    assert "/jobs/{job_id}/resumes/export.zip" not in paths


@pytest.mark.asyncio
async def test_the_synchronous_export_route_is_gone() -> None:
    app, _conn, _store, _arq = _app(Role.ADMIN)

    assert await _call(app, "GET", "/jobs/{id}/resumes/export.zip") in (404, 405)
