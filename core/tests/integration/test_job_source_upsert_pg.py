"""Integration — the Taleo job upsert against a REAL Postgres (ADR-046).

Five things a real database proves that a mocked connection structurally
cannot, and each is a way this sync could ship green and quietly corrupt the
job list:

* **Idempotency is a UNIQUE INDEX, not an intention.** The whole design rests
  on ``ON CONFLICT (source, external_id)`` matching the partial index. If the
  target ever drifts from the index, the conflict clause matches nothing and
  every re-sync inserts duplicates — with no error.
* **The partial index must not collide manual jobs.** Every human-created job
  has ``external_id IS NULL``; a non-partial unique index would reject the
  second one outright.
* **A changed JD clears its own extraction.** Three columns move together
  (``description_parsed``/``parsed_at``/``failure_reason``) in a SQL CASE no
  Python test can execute.
* **The archive sweep is scoped.** It must retire vanished Taleo postings and
  never touch a manual job. That is one predicate away from wiping the job
  list, and only a real query proves the predicate.
* **``archived -> draft`` on return**, a transition the job_service state
  machine does not list.

None of this exists yet — RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator, Iterator

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.services.job_source_service import (
    TALEO_SOURCE,
    ExternalJobUpsert,
    mark_missing_as_archived,
    upsert_external_job,
    write_sync_audit,
)


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox, audit_log CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


def _payload(rid: str = "7124", **kw: object) -> ExternalJobUpsert:
    base: dict[str, object] = {
        "external_id": rid,
        "external_url": f"https://tre.tbe.taleo.net/req?rid={rid}",
        "title": "Research Analyst",
        "description_raw": "A detailed job description of the role. " * 3,
        "department": "Neuroscience",
        "location": "Burnaby",
        "employment_type": "full_time",
    }
    base.update(kw)
    return ExternalJobUpsert(**base)  # type: ignore[arg-type]


async def _one(pool: asyncpg.Pool, job_id: uuid.UUID) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
    assert row is not None
    return row


# ------------------------------------------------------------- insert / update


async def test_a_new_requisition_is_inserted_as_a_draft(pg_pool: asyncpg.Pool) -> None:
    async with pg_pool.acquire() as conn:
        result = await upsert_external_job(conn, _payload())

    assert result.was_inserted is True
    assert result.description_changed is True, (
        "a fresh insert must count as changed, or the caller never enqueues "
        "parse_job and the job sits unparsed forever"
    )
    row = await _one(pg_pool, result.job_id)
    assert row["source"] == TALEO_SOURCE
    assert row["external_id"] == "7124"
    assert row["status"] == "draft"
    assert row["created_by"] == "taleo-sync"
    assert row["external_last_seen_at"] is not None
    assert row["description_sha256"], (
        "a synced job with no description_sha256 is invisible to the bulk-JD "
        "dedup probe, so the same JD can be created twice by two routes"
    )


async def test_re_syncing_the_same_requisition_updates_rather_than_duplicates(
    pg_pool: asyncpg.Pool,
) -> None:
    """The claim the whole design rests on. If ``ON CONFLICT`` ever stops
    matching the partial unique index, this inserts a second row — silently,
    every single day."""
    async with pg_pool.acquire() as conn:
        first = await upsert_external_job(conn, _payload())
        second = await upsert_external_job(conn, _payload())

    assert second.was_inserted is False
    assert second.job_id == first.job_id
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE source = $1 AND external_id = $2",
            TALEO_SOURCE,
            "7124",
        )
    assert count == 1


async def test_an_unchanged_jd_is_not_reported_as_changed(
    pg_pool: asyncpg.Pool,
) -> None:
    """``description_changed`` gates the ``parse_job`` re-enqueue. Reporting a
    false change every day would re-run the LLM on every job daily AND could
    move extracted requirements under a shortlist someone is reading."""
    async with pg_pool.acquire() as conn:
        await upsert_external_job(conn, _payload())
        again = await upsert_external_job(conn, _payload())
    assert again.description_changed is False


async def test_a_changed_jd_clears_its_own_extraction(
    pg_pool: asyncpg.Pool,
) -> None:
    """All three fields move together. Clearing only ``description_parsed``
    would leave a ``parsed_at`` timestamp asserting a parse that no longer
    describes the text."""
    async with pg_pool.acquire() as conn:
        first = await upsert_external_job(conn, _payload())
        await conn.execute(
            'UPDATE jobs SET description_parsed = \'{"title":"x"}\'::jsonb, '
            "parsed_at = now(), failure_reason = 'stale' WHERE id = $1",
            first.job_id,
        )
        changed = await upsert_external_job(
            conn, _payload(description_raw="A completely different posting. " * 3)
        )

    assert changed.description_changed is True
    row = await _one(pg_pool, first.job_id)
    assert row["description_parsed"] is None
    assert row["parsed_at"] is None
    assert row["failure_reason"] is None


async def test_a_resync_does_not_undo_a_recruiters_blind_review_choice(
    pg_pool: asyncpg.Pool,
) -> None:
    """``blind_review`` is set on INSERT only. A daily sync that reset it would
    silently re-blind a requisition a recruiter deliberately opened — and they
    would have no way to make the change stick."""
    async with pg_pool.acquire() as conn:
        first = await upsert_external_job(conn, _payload(blind_review=True))
        await conn.execute(
            "UPDATE jobs SET blind_review = false WHERE id = $1", first.job_id
        )
        await upsert_external_job(conn, _payload(blind_review=True))

    assert (await _one(pg_pool, first.job_id))["blind_review"] is False


async def test_a_returning_posting_reopens_from_archived_to_draft(
    pg_pool: asyncpg.Pool,
) -> None:
    """``archived -> draft`` is NOT in job_service's strictly-forward state
    machine, deliberately: it is the system correcting an archive its own
    sweep performed, not a human transition."""
    async with pg_pool.acquire() as conn:
        first = await upsert_external_job(conn, _payload())
        await conn.execute(
            "UPDATE jobs SET status = 'archived' WHERE id = $1", first.job_id
        )
        await upsert_external_job(conn, _payload())

    assert (await _one(pg_pool, first.job_id))["status"] == "draft"


# ------------------------------------------------------------ the archive sweep


async def test_the_sweep_archives_a_posting_that_vanished_upstream(
    pg_pool: asyncpg.Pool,
) -> None:
    async with pg_pool.acquire() as conn:
        gone = await upsert_external_job(conn, _payload("7124"))
        # A later run that observed only the OTHER requisition.
        run_started = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1)
        archived = await mark_missing_as_archived(conn, run_started_at=run_started)

    assert gone.job_id in archived
    assert (await _one(pg_pool, gone.job_id))["status"] == "archived"


async def test_the_sweep_never_touches_a_manual_job(pg_pool: asyncpg.Pool) -> None:
    """One predicate away from wiping the job list. Manual jobs have no
    ``external_last_seen_at`` at all, so a sweep keyed only on the timestamp
    would archive every human-created requisition on its first run."""
    async with pg_pool.acquire() as conn:
        manual_id = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, status) "
            "VALUES ('Manual Job', $1, 'open') RETURNING id",
            "A detailed job description of the role. " * 3,
        )
        run_started = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1)
        archived = await mark_missing_as_archived(conn, run_started_at=run_started)

    assert manual_id not in archived
    assert (await _one(pg_pool, manual_id))["status"] == "open"


async def test_a_posting_seen_this_run_is_not_archived(
    pg_pool: asyncpg.Pool,
) -> None:
    async with pg_pool.acquire() as conn:
        run_started = dt.datetime.now(dt.UTC)
        seen = await upsert_external_job(conn, _payload())  # stamps now() > start
        archived = await mark_missing_as_archived(conn, run_started_at=run_started)

    assert seen.job_id not in archived
    assert (await _one(pg_pool, seen.job_id))["status"] == "draft"


# ---------------------------------------------------------------- the audit row


async def test_a_run_that_changed_nothing_still_writes_an_audit_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """THE case the audit exists for. Zero rows is how "the template changed
    and we now parse nothing" looks exactly like "a quiet day with no new
    postings" — so the row must appear even when every counter is zero."""
    async with pg_pool.acquire() as conn:
        await write_sync_audit(conn, inserted=0, updated=0, archived=0)
        row = await conn.fetchrow(
            "SELECT action, subject_type, details FROM audit_log "
            "WHERE action = 'taleo_sync'"
        )

    assert row is not None
    assert row["subject_type"] == "job_source"


async def test_manual_jobs_do_not_collide_on_the_partial_unique_index(
    pg_pool: asyncpg.Pool,
) -> None:
    """Every manual job has ``external_id IS NULL``. A non-partial unique index
    on ``(source, external_id)`` would reject the second one — which is the
    kind of breakage that only appears once someone creates a second job."""
    async with pg_pool.acquire() as conn:
        for title in ("First", "Second", "Third"):
            await conn.execute(
                "INSERT INTO jobs (title, description_raw) VALUES ($1, $2)",
                title,
                "A detailed job description of the role. " * 3,
            )
        count = await conn.fetchval("SELECT count(*) FROM jobs WHERE source='manual'")
    assert count == 3
