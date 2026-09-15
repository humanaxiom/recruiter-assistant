"""Integration tests — real Postgres (testcontainers): a job whose JD parse
extracted ZERO ``required_skills`` AND ZERO ``nice_to_have_skills`` must be
refused ranking (409), never silently 202'd into a meaningless shortlist.

Pilot defect 2026-09-10 (mirrors ADR-017 decision 1's "refuse rather than
silently degrade" shape). ``src.services.shortlist_service.
assert_job_has_requirements`` does not exist yet and the route does not call
it -- every test below fails either at import/AttributeError or because the
route still 202s and sets ``shortlist_state='ranking'`` on an all-zero JD.
RED half of the TDD cycle.

What a REAL Postgres proves that a mocked-conn unit test cannot: the actual
``coalesce(jsonb_array_length(description_parsed -> 'required_skills'), 0)``
expression evaluates correctly against real JSONB (including a genuinely
NULL ``description_parsed`` column and a parsed blob that lacks either key
entirely -- neither of which a hand-typed mock row can misrepresent the same
way a real driver's NULL handling can).

Harness mirrored from ``test_shortlist_ranking_state_pg.py`` (fixtures,
``_insert_job``, ``_build_app``, ``_client``, ``_job_state_row``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import shortlist as shortlist_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db



# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=4, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_job(pool: asyncpg.Pool, *, description_parsed: str | None) -> UUID:
    """``description_parsed=None`` seeds a never-parsed JD (``NULL`` column);
    otherwise the caller passes a JSON string literal."""
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, description_parsed) "
            "VALUES ($1, $2, $3::jsonb) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text (irrelevant to these tests)",
            description_parsed,
        )
    return job_id


async def _job_state_row(pool: asyncpg.Pool, job_id: UUID) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT shortlist_state FROM jobs WHERE id = $1", job_id
        )


def _build_app(pool: asyncpg.Pool, *, arq: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(shortlist_routes.router)

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


# ── (a) both lists empty -> 409, shortlist_state left NULL ─────────────────


@pytest.mark.asyncio
async def test_both_lists_empty_refuses_with_409_and_leaves_state_null(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(
        pg_pool,
        description_parsed=(
            '{"title": "x", "required_skills": [], "nice_to_have_skills": []}'
        ),
    )
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 409
    assert resp.json()["code"] == "resource.conflict"
    arq.enqueue_job.assert_not_awaited()

    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] is None, (
        "a refused generate must never have set shortlist_state='ranking' -- "
        "the guard must run BEFORE that write, not after"
    )


# ── (b) required_skills present -> 202, shortlist_state='ranking' ──────────


@pytest.mark.asyncio
async def test_required_skills_present_proceeds_and_sets_ranking_state(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(
        pg_pool,
        description_parsed=(
            '{"title": "x", "required_skills": [{"name": "Python"}], '
            '"nice_to_have_skills": []}'
        ),
    )
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("shortlist_job", str(job_id))

    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] == "ranking"


# ── (c) description_parsed NULL (never parsed) -> 409 ──────────────────────


@pytest.mark.asyncio
async def test_never_parsed_jd_refuses_with_409(pg_pool: asyncpg.Pool) -> None:
    job_id = await _insert_job(pg_pool, description_parsed=None)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 409
    arq.enqueue_job.assert_not_awaited()

    row = await _job_state_row(pg_pool, job_id)
    assert row is not None
    assert row["shortlist_state"] is None


# ── (d) description_parsed lacks both keys entirely -> 409, not 500 ────────


@pytest.mark.asyncio
async def test_description_parsed_missing_both_keys_refuses_409_not_500(
    pg_pool: asyncpg.Pool,
) -> None:
    """A parsed blob that predates either skills field, or an LLM extraction
    that dropped them entirely -- ``jsonb_array_length(NULL)`` on a missing
    key must ``coalesce`` to 0, not raise inside Postgres and 500 the route."""
    job_id = await _insert_job(pg_pool, description_parsed='{"title": "x"}')
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(pg_pool, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 409
    arq.enqueue_job.assert_not_awaited()
