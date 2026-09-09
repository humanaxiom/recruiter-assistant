"""Integration — the JD write-back's merge rules, against a real Postgres.

The unit tests for this assert on ``_RECORD_PARSED_SQL`` as a *string*: they
prove the SQL says ``COALESCE(NULLIF(jobs.department, ''), $5)`` and can prove
nothing about what a row actually holds afterwards. The whole point of the
change is a merge, and a merge is a claim about rows.

Four claims that only real data settles:

* **``title_provisional`` exists.** The column is added by an idempotent
  ``ALTER``; the UPDATE references it in two places and the INSERT in one. Get
  the DDL wrong and every parse in production raises
  ``UndefinedColumnError`` — the exact failure mode the Phase 0 cut of hris's
  ``title_autofilled`` was avoiding, and a mocked ``conn.execute`` will happily
  accept SQL naming a column that does not exist.
* **An override survives a re-parse.** The "override/fill" half of what the
  sponsor asked for is a promise about the SECOND parse, so it needs two.
* **An empty string counts as unset.** The create form posts ``''`` for a
  blank field. Without ``NULLIF`` those rows are permanently unfillable, and
  no string assertion can tell the two spellings apart.
* **A chosen title is untouchable.** The flag is what licenses the rewrite, so
  a row without it has to come back unchanged.
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
from src.schemas.jobs import JDExtracted, JobCreate

_NOW = dt.datetime(2026, 9, 3, 12, 0, tzinfo=dt.UTC)


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


async def _row(pool: asyncpg.Pool, job_id: UUID) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT title, title_provisional, department, location "
            "FROM jobs WHERE id = $1",
            job_id,
        )


async def _create(
    pool: asyncpg.Pool, *, provisional: bool = False, **over: object
) -> UUID:
    """Insert through the service so the real INSERT column list is exercised."""
    from src.services import job_service

    async with pool.acquire() as conn:
        row = await job_service._insert_job(
            conn,
            _payload(**over),
            created_by=None,
            description_sha256=f"sha-{over.get('title', 'x')}-{provisional}",
            title_provisional=provisional,
        )
    return UUID(str(row["id"]))


# ── the column is really there ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_insert_and_the_update_both_reach_a_real_column(
    pg_pool: asyncpg.Pool,
) -> None:
    """If ``title_provisional`` were missing from the DDL, both statements
    would raise ``UndefinedColumnError`` here and neither unit test would
    notice — a mocked connection accepts any SQL text at all."""
    from src.services import job_service

    job_id = await _create(pg_pool, provisional=True)
    async with pg_pool.acquire() as conn:
        applied = await job_service.record_parsed(conn, job_id, _extracted(), _NOW)
    assert applied is True


# ── title ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_provisional_title_is_replaced_and_the_flag_cleared(
    pg_pool: asyncpg.Pool,
) -> None:
    """The pilot box's 23 filename-titled requisitions, in one test."""
    from src.services import job_service

    job_id = await _create(
        pg_pool, provisional=True, title="20260612 00138559 APSA JDFN 20260612"
    )
    async with pg_pool.acquire() as conn:
        await job_service.record_parsed(conn, job_id, _extracted(), _NOW)

    row = await _row(pg_pool, job_id)
    assert row["title"] == "Multimedia Specialist"
    assert row["title_provisional"] is False


@pytest.mark.asyncio
async def test_a_chosen_title_is_left_alone(pg_pool: asyncpg.Pool) -> None:
    from src.services import job_service

    job_id = await _create(pg_pool, title="Research Analyst, Neuroscience")
    async with pg_pool.acquire() as conn:
        await job_service.record_parsed(conn, job_id, _extracted(), _NOW)

    assert (await _row(pg_pool, job_id))["title"] == "Research Analyst, Neuroscience"


@pytest.mark.asyncio
async def test_a_second_parse_cannot_rename_a_corrected_title(
    pg_pool: asyncpg.Pool,
) -> None:
    """Clearing the flag is what makes this true. Without it a recruiter who
    fixed a title would watch the next re-parse undo them — and a JD can be
    re-parsed by hand (``POST /jobs/{id}/reparse``) or by a Taleo re-sync."""
    from src.services import job_service

    job_id = await _create(pg_pool, provisional=True, title="filename-stem")
    async with pg_pool.acquire() as conn:
        await job_service.record_parsed(conn, job_id, _extracted(), _NOW)
        await conn.execute(
            "UPDATE jobs SET title = 'Multimedia Specialist II' WHERE id = $1", job_id
        )
        await job_service.record_parsed(
            conn, job_id, _extracted(title="Something Else"), _NOW
        )

    assert (await _row(pg_pool, job_id))["title"] == "Multimedia Specialist II"


# ── department / location ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_department_and_location_are_filled(
    pg_pool: asyncpg.Pool,
) -> None:
    from src.services import job_service

    job_id = await _create(pg_pool)
    async with pg_pool.acquire() as conn:
        await job_service.record_parsed(
            conn,
            job_id,
            _extracted(department="School of Medicine", location="Surrey"),
            _NOW,
        )

    row = await _row(pg_pool, job_id)
    assert row["department"] == "School of Medicine"
    assert row["location"] == "Surrey"


@pytest.mark.asyncio
async def test_the_written_campus_is_canonical(pg_pool: asyncpg.Pool) -> None:
    """ "Burnaby, BC" from a model and "bby" from a form have to land on the
    same stored value, or the column cannot be grouped or counted."""
    from src.services import job_service

    job_id = await _create(pg_pool)
    async with pg_pool.acquire() as conn:
        await job_service.record_parsed(
            conn, job_id, _extracted(location="SFU Burnaby Campus, BC"), _NOW
        )

    assert (await _row(pg_pool, job_id))["location"] == "Burnaby"


@pytest.mark.asyncio
async def test_a_recruiters_override_survives_a_reparse(
    pg_pool: asyncpg.Pool,
) -> None:
    """THE test for what the sponsor asked for. Somebody sets the campus by
    hand, the JD is re-parsed, and the extraction must not win."""
    from src.services import job_service

    job_id = await _create(pg_pool, department="Faculty of Health Sciences")
    async with pg_pool.acquire() as conn:
        await job_service.record_parsed(
            conn, job_id, _extracted(location="Surrey"), _NOW
        )
        # The recruiter disagrees with the parse.
        await conn.execute(
            "UPDATE jobs SET location = 'Vancouver' WHERE id = $1", job_id
        )
        await job_service.record_parsed(
            conn,
            job_id,
            _extracted(department="Somewhere Else", location="Burnaby"),
            _NOW,
        )

    row = await _row(pg_pool, job_id)
    assert row["location"] == "Vancouver"
    assert row["department"] == "Faculty of Health Sciences"


@pytest.mark.asyncio
async def test_an_empty_string_column_is_treated_as_unset(
    pg_pool: asyncpg.Pool,
) -> None:
    """``NULLIF``, proved on a row rather than in a regex. The create form posts
    '' for a blank field, so without this those jobs could never acquire a
    department at all — and '' is invisible to every test that only checks for
    NULL."""
    from src.services import job_service

    job_id = await _create(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET department = '', location = '' WHERE id = $1", job_id
        )
        await job_service.record_parsed(
            conn,
            job_id,
            _extracted(department="School of Medicine", location="Surrey"),
            _NOW,
        )

    row = await _row(pg_pool, job_id)
    assert row["department"] == "School of Medicine"
    assert row["location"] == "Surrey"


@pytest.mark.asyncio
async def test_an_extraction_with_neither_field_changes_neither(
    pg_pool: asyncpg.Pool,
) -> None:
    """The common case for these JDs: the posting states no campus, and 20 of
    26 state no department either. Writing NULL over NULL must stay a no-op,
    and must not blank a value that was already there."""
    from src.services import job_service

    job_id = await _create(pg_pool, department="Library", location="Burnaby")
    async with pg_pool.acquire() as conn:
        await job_service.record_parsed(conn, job_id, _extracted(), _NOW)

    row = await _row(pg_pool, job_id)
    assert row["department"] == "Library"
    assert row["location"] == "Burnaby"
