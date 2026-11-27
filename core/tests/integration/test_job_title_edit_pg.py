"""Integration -- a human title edit must survive the JD parse, real Postgres.

``record_parsed`` replaces ``jobs.title`` whenever ``title_provisional`` is
true, and clears the flag as it does. A title typed by a recruiter while the
JD is still parsing (S6, editable job details) therefore had no protection:
``update_job`` changed the title and left the flag set, so the parse that
finished a moment later overwrote it. A mocked connection accepts any SQL text,
so only a real row can say whether the title is still there afterwards.

Fixture wiring mirrors ``test_jd_writeback_merge_pg.py``.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.schemas.jobs import JDExtracted, JobCreate, JobUpdate

_NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
_ACTOR: dict[str, Any] = {
    "actor_kind": "service",
    "actor_user_id": None,
    "actor_service": "test-harness",
}


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


def _payload(**over: object) -> JobCreate:
    base: dict[str, object] = {
        "title": "Placeholder",
        "description_raw": "A job description long enough to pass the floor. " * 3,
    }
    base.update(over)
    return JobCreate.model_validate(base)


def _extracted(**over: Any) -> JDExtracted:
    base: dict[str, Any] = {"title": "Multimedia Specialist"}
    base.update(over)
    return JDExtracted(**base)


async def _create(pool: asyncpg.Pool, *, provisional: bool, title: str) -> UUID:
    from src.services import job_service

    async with pool.acquire() as conn:
        row = await job_service._insert_job(
            conn,
            _payload(title=title),
            created_by=None,
            description_sha256=f"sha-{title}-{provisional}",
            title_provisional=provisional,
        )
    return UUID(str(row["id"]))


async def _row(pool: asyncpg.Pool, job_id: UUID) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT title, title_provisional FROM jobs WHERE id = $1", job_id
        )


async def _patch(pool: asyncpg.Pool, job_id: UUID, body: dict[str, Any]) -> None:
    from src.services import job_service

    async with pool.acquire() as conn:
        await job_service.update_job(
            conn, job_id, JobUpdate.model_validate(body), **_ACTOR
        )


async def _parse(pool: asyncpg.Pool, job_id: UUID, **over: Any) -> bool:
    from src.services import job_service

    async with pool.acquire() as conn:
        return await job_service.record_parsed(conn, job_id, _extracted(**over), _NOW)


@pytest.mark.asyncio
async def test_editing_the_title_clears_the_provisional_flag(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _create(pg_pool, provisional=True, title="20260612 JDFN")
    assert (await _row(pg_pool, job_id))["title_provisional"] is True

    await _patch(pg_pool, job_id, {"title": "Research Analyst"})

    row = await _row(pg_pool, job_id)
    assert row["title"] == "Research Analyst"
    assert row["title_provisional"] is False


@pytest.mark.asyncio
async def test_a_title_typed_before_the_parse_finishes_survives_the_parse(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _create(pg_pool, provisional=True, title="20260612 JDFN")
    await _patch(pg_pool, job_id, {"title": "Research Analyst"})

    applied = await _parse(pg_pool, job_id, title="Multimedia Specialist")

    assert applied is True, "the parse itself must still apply"
    row = await _row(pg_pool, job_id)
    assert row["title"] == "Research Analyst"
    assert row["title_provisional"] is False


@pytest.mark.asyncio
async def test_a_job_never_edited_still_gets_the_parsed_title(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _create(pg_pool, provisional=True, title="20260612 JDFN")

    await _parse(pg_pool, job_id, title="Multimedia Specialist")

    row = await _row(pg_pool, job_id)
    assert row["title"] == "Multimedia Specialist"
    assert row["title_provisional"] is False


@pytest.mark.asyncio
async def test_editing_another_field_leaves_the_job_provisional(
    pg_pool: asyncpg.Pool,
) -> None:
    """Only a TITLE edit is a human choice of title; a campus fix is not."""
    job_id = await _create(pg_pool, provisional=True, title="20260612 JDFN")
    await _patch(pg_pool, job_id, {"department": "Library", "min_years": 3})

    assert (await _row(pg_pool, job_id))["title_provisional"] is True

    await _parse(pg_pool, job_id, title="Multimedia Specialist")
    assert (await _row(pg_pool, job_id))["title"] == "Multimedia Specialist"


@pytest.mark.asyncio
async def test_a_title_edit_on_a_chosen_title_keeps_it_not_provisional(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _create(pg_pool, provisional=False, title="Research Analyst")
    await _patch(pg_pool, job_id, {"title": "Senior Research Analyst"})

    row = await _row(pg_pool, job_id)
    assert row["title"] == "Senior Research Analyst"
    assert row["title_provisional"] is False

    await _parse(pg_pool, job_id, title="Something Else")
    assert (await _row(pg_pool, job_id))["title"] == "Senior Research Analyst"
