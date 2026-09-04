"""Integration — the manager's note actually persists, against a real Postgres.

Two claims, both about rows, both invisible to the unit suite:

* **``PATCH /jobs/{id}`` writes ``additional_requirements``.** The unit test
  can only assert the string is in ``_UPDATABLE_JOB_COLUMNS``. What was broken
  before was the round trip: the field was accepted by ``JobUpdate``, filtered
  out by ``update_job``, and answered with a 200 carrying the OLD value — a
  mocked ``conn`` returns whatever canned row it was handed, so it agrees with
  either behaviour.
* **``record_manager_requirements`` touches one column and is not gated on
  'draft'.** The unit test greps the SQL text. Only a real row shows that the
  extraction lands while ``description_parsed``, ``parsed_at`` and ``status``
  do not move — and that it applies to an OPEN requisition, which is the case
  the whole feature exists for and the one a ``status = 'draft'`` scope would
  silently skip.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.schemas.jobs import JobCreate, JobUpdate, ManagerRequirements

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
        await conn.execute("TRUNCATE jobs, outbox, audit_log CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


def _payload(**over: object) -> JobCreate:
    base: dict[str, object] = {
        "title": "Multimedia Specialist",
        "description_raw": "A job description long enough to pass the floor. " * 3,
    }
    base.update(over)
    return JobCreate.model_validate(base)


async def _create(pg_pool: asyncpg.Pool, **over: object) -> UUID:
    from src.services import job_service

    async with pg_pool.acquire() as conn:
        job = await job_service.create_job(conn, _payload(**over), created_by=None)
    return job.id


async def _row(pg_pool: asyncpg.Pool, job_id: UUID) -> Any:
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT additional_requirements, additional_requirements_parsed, "
            "description_parsed, parsed_at, status FROM jobs WHERE id = $1",
            job_id,
        )


# ── the note is writable through the normal update path ─────────────────────


@pytest.mark.asyncio
async def test_a_patch_persists_the_manager_note(pg_pool: asyncpg.Pool) -> None:
    """THE regression: a bulk-uploaded requisition acquiring a note for the
    first time. Before this the UPDATE simply did not include the column."""
    from src.services import job_service

    job_id = await _create(pg_pool)
    async with pg_pool.acquire() as conn:
        out = await job_service.update_job(
            conn,
            job_id,
            JobUpdate(additional_requirements="Must have MEG analysis."),
            **_ACTOR,
        )

    assert out.additional_requirements == "Must have MEG analysis."
    row = await _row(pg_pool, job_id)
    assert row["additional_requirements"] == "Must have MEG analysis."


@pytest.mark.asyncio
async def test_the_note_can_be_cleared(pg_pool: asyncpg.Pool) -> None:
    """ "I no longer want this scored" has to be expressible. NULL, not '' —
    the combine reads null as *nobody asked* and marks the sub-score
    unmeasured, where an empty string would assert the manager listed nothing.
    """
    from src.services import job_service

    job_id = await _create(pg_pool, additional_requirements="Must have MEG.")
    async with pg_pool.acquire() as conn:
        await job_service.update_job(
            conn, job_id, JobUpdate(additional_requirements=None), **_ACTOR
        )

    assert (await _row(pg_pool, job_id))["additional_requirements"] is None


@pytest.mark.asyncio
async def test_a_patch_that_omits_the_note_leaves_it_alone(
    pg_pool: asyncpg.Pool,
) -> None:
    """Omit means unchanged — ``JobUpdate``'s convention, and the reason
    ``update_job`` builds its SET clause from ``exclude_unset``. Fixing a
    campus must not wipe the manager's requirements."""
    from src.services import job_service

    job_id = await _create(pg_pool, additional_requirements="Must have MEG.")
    async with pg_pool.acquire() as conn:
        await job_service.update_job(
            conn, job_id, JobUpdate(location="Surrey"), **_ACTOR
        )

    row = await _row(pg_pool, job_id)
    assert row["additional_requirements"] == "Must have MEG."


# ── the re-extraction write ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_reextraction_writes_only_its_own_column(
    pg_pool: asyncpg.Pool,
) -> None:
    """Proved on a row, not by grepping SQL. Re-running the JD parse here would
    re-derive the posting's own requirements underneath a shortlist somebody is
    reading, so the other three columns must be untouched."""
    from src.services import job_service

    job_id = await _create(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET description_parsed = $2::jsonb, parsed_at = now() "
            "WHERE id = $1",
            job_id,
            json.dumps({"title": "Multimedia Specialist"}),
        )
        before = await _row(pg_pool, job_id)
        await job_service.record_manager_requirements(
            conn,
            job_id,
            ManagerRequirements(must_have_skills=[{"name": "MEG", "min_years": None}]),
        )

    after = await _row(pg_pool, job_id)
    assert after["additional_requirements_parsed"] is not None
    assert after["description_parsed"] == before["description_parsed"]
    assert after["parsed_at"] == before["parsed_at"]
    assert after["status"] == before["status"]


@pytest.mark.asyncio
async def test_the_reextraction_applies_to_an_open_requisition(
    pg_pool: asyncpg.Pool,
) -> None:
    """The case the feature exists for, and the one a ``status = 'draft'``
    scope — copied from ``record_parsed`` without thinking — would silently
    skip while reporting success."""
    from src.services import job_service

    job_id = await _create(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute("UPDATE jobs SET status = 'open' WHERE id = $1", job_id)
        await job_service.record_manager_requirements(
            conn,
            job_id,
            ManagerRequirements(must_have_skills=[{"name": "MEG", "min_years": None}]),
        )

    row = await _row(pg_pool, job_id)
    assert row["status"] == "open"
    assert row["additional_requirements_parsed"] is not None


@pytest.mark.asyncio
async def test_clearing_the_note_can_clear_its_extraction(
    pg_pool: asyncpg.Pool,
) -> None:
    """``None`` has to reach the column as SQL NULL. Otherwise the extraction
    outlives its own input and keeps scoring candidates against requirements
    that were deleted."""
    from src.services import job_service

    job_id = await _create(pg_pool)
    async with pg_pool.acquire() as conn:
        await job_service.record_manager_requirements(
            conn,
            job_id,
            ManagerRequirements(must_have_skills=[{"name": "MEG", "min_years": None}]),
        )
        await job_service.record_manager_requirements(conn, job_id, None)

    assert (await _row(pg_pool, job_id))["additional_requirements_parsed"] is None
