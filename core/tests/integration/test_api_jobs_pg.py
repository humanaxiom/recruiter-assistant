"""Integration tests — Phase 6's job routes against a REAL Postgres
(testcontainers) via the real ASGI app (``app.state.pg_pool``), a real
``BlobStore`` rooted at a temp dir, and a FAKE ``ArqRedis`` recording
``enqueue_job`` calls (no real Redis/arq — nothing here needs the broker
itself, only proof that the route calls ``enqueue_job`` with the right
arguments once a job row is REALLY committed).

``src.api.routes.jobs`` does not exist yet — every test below fails at
collection or the first request. RED half of the TDD cycle.

What a REAL Postgres proves that the mocked-conn route tests
(``test_route_jobs.py``) cannot: ``create_job``'s INSERT actually satisfies
the DDL's ``job_status`` enum / ``blind_review BOOLEAN NOT NULL DEFAULT
FALSE`` (REVERSED 2026-09-09 — was ``DEFAULT TRUE`` under decision 4; the
per-job toggle is unchanged, only the create-time default flips) /
``retention_days`` CHECK, and ``transition_status``'s UPDATE resolves against
a row that REALLY started life in 'draft'.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import jobs as jobs_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


def _build_app(pool: asyncpg.Pool, *, arq: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(jobs_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: arq
    app.dependency_overrides[resolve_role] = lambda: Role.ADMIN

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_create_and_get_job_round_trips_through_real_postgres(
    pg_pool: asyncpg.Pool,
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        create_resp = await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
        assert create_resp.status_code == 201
        job_id = create_resp.json()["id"]

        get_resp = await client.get(f"/jobs/{job_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["title"] == "Senior Backend Engineer"
    assert get_resp.json()["status"] == "draft"
    arq.enqueue_job.assert_awaited_once_with("parse_job", job_id)


@pytest.mark.asyncio
async def test_create_job_blind_review_defaults_false_against_real_ddl(
    pg_pool: asyncpg.Pool,
) -> None:
    """REVERSED 2026-09-09 (sponsor decision): blind review is opt-in per
    job now, not default-on. A POST that omits ``blind_review`` entirely must
    round-trip through the real ``JobCreate`` default AND the real DDL
    default and come back ``False`` — this is the end-to-end proof neither
    ``test_schemas_jobs.py`` (schema only) nor ``test_ddl.py``/``test_schema.py``
    (DDL only) can give on their own."""
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json={
                "title": "Staff Engineer",
                "description_raw": "We need a staff engineer. " * 3,
            },
        )
    assert resp.json()["blind_review"] is False


@pytest.mark.asyncio
async def test_create_job_blind_review_can_still_be_opted_in(
    pg_pool: asyncpg.Pool,
) -> None:
    """The reversal changes the DEFAULT only — a caller must still be able to
    opt in explicitly at create time (mirrors the per-job toggle button)."""
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json={
                "title": "Staff Engineer",
                "description_raw": "We need a staff engineer. " * 3,
                "blind_review": True,
            },
        )
    assert resp.json()["blind_review"] is True


@pytest.mark.asyncio
async def test_transition_status_draft_to_open_persists_against_real_postgres(
    pg_pool: asyncpg.Pool,
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        create_resp = await client.post(
            "/jobs",
            json={
                "title": "Product Manager",
                "description_raw": "We need a product manager. " * 3,
            },
        )
        job_id = create_resp.json()["id"]
        patch_resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})
        get_resp = await client.get(f"/jobs/{job_id}")
    assert patch_resp.status_code == 200
    assert get_resp.json()["status"] == "open"


@pytest.mark.asyncio
async def test_get_job_404_for_a_real_missing_uuid(pg_pool: asyncpg.Pool) -> None:
    from uuid import uuid4

    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 404


# ── Item 5 — zero-requirements JD recovery: PATCH description_raw ─────────


@pytest.mark.asyncio
async def test_patch_description_raw_on_a_draft_job_nulls_parse_output_and_enqueues(
    pg_pool: asyncpg.Pool,
) -> None:
    """A REAL Postgres proof that ``description_parsed``/``parsed_at`` are
    genuinely NULLED (not just that the route called something plausible) —
    ``job_service.clear_parse_output`` must apply against the real DDL."""
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    async with await _client(app) as client:
        create_resp = await client.post(
            "/jobs",
            json={
                "title": "Multimedia Specialist",
                "description_raw": "An empty-requirements JD. " * 3,
            },
        )
        job_id = create_resp.json()["id"]

        # Simulate a completed (zero-requirements) parse directly against the
        # real row, mirroring what `record_parsed` would have written.
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET description_parsed = $2::jsonb, "
                "parsed_at = now(), failure_reason = NULL WHERE id = $1",
                job_id,
                '{"title": "Multimedia Specialist", "required_skills": [], '
                '"nice_to_have_skills": []}',
            )

        arq.enqueue_job.reset_mock()
        patch_resp = await client.patch(
            f"/jobs/{job_id}",
            json={"description_raw": "A rewritten JD with real content. " * 3},
        )
        get_resp = await client.get(f"/jobs/{job_id}")

    assert patch_resp.status_code == 200, patch_resp.text
    body = get_resp.json()
    assert body["description_parsed"] is None
    assert body["parsed_at"] is None
    assert body["description_raw"] == "A rewritten JD with real content. " * 3
    arq.enqueue_job.assert_awaited_once_with("parse_job", job_id)


@pytest.mark.asyncio
async def test_patch_description_raw_on_an_open_job_409s_and_row_is_unchanged(
    pg_pool: asyncpg.Pool,
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)
    original_description = "The original JD text, never replaced. " * 3
    async with await _client(app) as client:
        create_resp = await client.post(
            "/jobs",
            json={
                "title": "Research Officer",
                "description_raw": original_description,
            },
        )
        job_id = create_resp.json()["id"]
        transition_resp = await client.patch(
            f"/jobs/{job_id}/status", json={"to": "open"}
        )
        assert transition_resp.status_code == 200

        arq.enqueue_job.reset_mock()
        patch_resp = await client.patch(
            f"/jobs/{job_id}",
            json={"description_raw": "An attempted rewrite. " * 5},
        )
        get_resp = await client.get(f"/jobs/{job_id}")

    assert patch_resp.status_code == 409
    body = get_resp.json()
    assert body["description_raw"] == original_description
    assert body["status"] == "open"
    arq.enqueue_job.assert_not_awaited()
