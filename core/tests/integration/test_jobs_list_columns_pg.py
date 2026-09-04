"""Integration — the jobs LIST query, against a real Postgres.

The unit suite asserts on ``_LIST_JOBS_BASE_SQL`` as a *string*. That is
enough to prove a column name appears in the SELECT and nothing more, and
this exact query has already produced one defect the string assertions could
not see: an earlier version of the résumé list JOINed a second table and
raised ``AmbiguousColumnError`` on the first real call, behind a green unit
suite.

Two things here only a real database can settle:

* **The query parses and runs at all** — with the correlated subquery, and
  with the ``WHERE`` clause the assembler appends after ``FROM jobs`` (the
  status filter and FU-6's ``EXISTS`` against ``job_assignees``), which is
  the interaction a subquery is most likely to break.
* **``resume_count`` is correlated to THIS job.** A subquery missing its
  ``WHERE resumes.job_id = jobs.id`` still parses, still returns an integer,
  and still passes every string assertion — it just returns the count of
  every résumé in the database on every row. Only real data distinguishes
  "17 for this job" from "17 in the whole table", so the fixture below
  deliberately gives two jobs DIFFERENT counts and a third none at all.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.schemas.jobs import JobCreate


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
        "title": "Senior Backend Engineer",
        "description_raw": "We are looking for a senior backend engineer " * 3,
    }
    base.update(over)
    return JobCreate.model_validate(base)


async def _add_resumes(pool: asyncpg.Pool, job_id: UUID, n: int) -> None:
    """Minimal résumé rows — only the NOT NULL columns. No PII: these exist
    to be counted, so ``candidate_*`` stays null."""
    async with pool.acquire() as conn:
        for _ in range(n):
            await conn.execute(
                "INSERT INTO resumes (job_id, blob_key, original_filename, "
                "mime_type, file_size_bytes, sha256, consent_acknowledged) "
                "VALUES ($1, $2, 'cv.pdf', 'application/pdf', 1024, $3, true)",
                job_id,
                f"blob/{uuid4()}",
                uuid4().hex,  # UNIQUE (job_id, sha256)
            )


@pytest.mark.asyncio
async def test_the_list_counts_each_jobs_own_resumes(pg_pool: asyncpg.Pool) -> None:
    """The correlation, which is the whole point. Three jobs, three different
    counts: an uncorrelated subquery would return 5 for all three."""
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        busy = await job_service.create_job(
            conn, _payload(title="Busy"), created_by=None
        )
        quiet = await job_service.create_job(
            conn, _payload(title="Quiet"), created_by=None
        )
        empty = await job_service.create_job(
            conn, _payload(title="Empty"), created_by=None
        )

    await _add_resumes(pg_pool, busy.id, 3)
    await _add_resumes(pg_pool, quiet.id, 2)

    async with pg_pool.acquire() as conn:
        rows = await job_service.list_jobs(conn)

    counts = {r.title: r.resume_count for r in rows}
    assert counts["Busy"] == 3
    assert counts["Quiet"] == 2
    assert counts[empty.title] == 0, "a job with no résumés must read 0, not null"


@pytest.mark.asyncio
async def test_the_list_survives_the_status_filter(pg_pool: asyncpg.Pool) -> None:
    """The ``WHERE`` the assembler appends after ``FROM jobs``. A subquery in
    the select list and an appended predicate are the pair most likely to
    produce a syntax error or an ambiguous column, and neither shows up in a
    string assertion."""
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        job = await job_service.create_job(conn, _payload(), created_by=None)
    await _add_resumes(pg_pool, job.id, 4)

    async with pg_pool.acquire() as conn:
        rows = await job_service.list_jobs(conn, status="draft")

    assert [r.resume_count for r in rows] == [4]


@pytest.mark.asyncio
async def test_the_list_survives_the_assignee_scoping_predicate(
    pg_pool: asyncpg.Pool,
) -> None:
    """FU-6's ``EXISTS (SELECT 1 FROM job_assignees ...)`` — a second
    subquery, in the WHERE, referencing ``jobs.id`` exactly as the new one
    does. If either loses its qualification the planner says so here."""
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        job = await job_service.create_job(conn, _payload(), created_by=None)
        user_id: UUID = await conn.fetchval(
            "INSERT INTO users (cas_username, role) VALUES ($1, 'hiring_manager') "
            "RETURNING id",
            f"hm-{uuid4().hex[:8]}",
        )
        await conn.execute(
            "INSERT INTO job_assignees (job_id, user_id, assigned_by) "
            "VALUES ($1, $2, $2)",
            job.id,
            user_id,
        )
    await _add_resumes(pg_pool, job.id, 5)

    async with pg_pool.acquire() as conn:
        rows = await job_service.list_jobs(conn, user_id=user_id)

    assert [(r.title, r.resume_count) for r in rows] == [(job.title, 5)]


@pytest.mark.asyncio
async def test_the_list_returns_location_and_provenance_from_real_rows(
    pg_pool: asyncpg.Pool,
) -> None:
    """The three columns that were in the template but not the query. A dict
    handed to a mocked conn always has the keys; a real row only has them if
    the SELECT asked for them."""
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        job = await job_service.create_job(
            conn, _payload(location="Burnaby"), created_by=None
        )
        await conn.execute(
            "UPDATE jobs SET source = 'taleo', external_url = $2 WHERE id = $1",
            job.id,
            "https://tre.tbe.taleo.net/req?rid=7124",
        )

    async with pg_pool.acquire() as conn:
        (row,) = await job_service.list_jobs(conn)

    assert row.location == "Burnaby"
    assert row.source == "taleo"
    assert row.external_url == "https://tre.tbe.taleo.net/req?rid=7124"
    assert row.updated_at is not None
