"""Integration test — ``resume_service.reset_for_reparse`` against a real
Postgres (testcontainers), same conventions as
``test_resume_parse_claim_pg.py``.

None of ``resume_service.reset_for_reparse``, ``resume_service.
_RESET_FOR_REPARSE_SQL``, the ``resumes.reparse_requested_at`` column, or
``reconcile._SELECT_STALLED``'s ``GREATEST(...)`` staleness expression exist
yet on this branch — every test below fails today, either at
``AttributeError``/``UndefinedColumnError`` or on a plain assertion. RED half
of the TDD cycle.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.parsing import MIME_TXT
from src.services import resume_service
from src.worker import reconcile

RESUME_TEXT = "Summary\nExperienced backend engineer.\n"


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "Build the ranking pipeline end to end.",
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    *,
    blob_key: str,
    status: str = "failed",
    failure_reason: str | None = "LLMUnavailableError: circuit breaker open",
    parsed: str | None = None,
    withdrawn_at: Any = None,
    reconcile_attempts: int | None = 2,
) -> uuid.UUID:
    data = RESUME_TEXT.encode("utf-8")
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status,
                failure_reason, parsed, parsed_at, withdrawn_at,
                reconcile_attempts
            ) VALUES (
                $1, $2, 'ada-lovelace.txt', $3, $4, $5, TRUE, $6,
                $7, $8::jsonb, CASE WHEN $8 IS NULL THEN NULL ELSE now() END,
                $9, $10
            )
            RETURNING id
            """,
            job_id,
            blob_key,
            MIME_TXT,
            len(data),
            hashlib.sha256(data).hexdigest(),
            status,
            failure_reason,
            parsed,
            withdrawn_at,
            reconcile_attempts,
        )
    return resume_id


async def _row(pool: asyncpg.Pool, resume_id: uuid.UUID) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM resumes WHERE id = $1", resume_id)
    assert row is not None
    return row


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=1, max_size=5)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


# ── reset_for_reparse: eligible rows ──────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_for_reparse_resets_a_failed_row(pg_pool: asyncpg.Pool) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, blob_key="resumes/a.txt", status="failed"
    )

    async with pg_pool.acquire() as conn:
        result = await resume_service.reset_for_reparse(conn, resume_id)

    assert result is True
    row = await _row(pg_pool, resume_id)
    assert row["status"] == "uploaded"
    assert row["failure_reason"] is None
    assert row["parsed"] is None
    assert row["parsed_at"] is None
    assert row["reconcile_attempts"] == 0
    assert row["reparse_requested_at"] is not None


@pytest.mark.asyncio
async def test_reset_for_reparse_resets_a_degraded_parsed_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        blob_key="resumes/b.txt",
        status="parsed",
        failure_reason=None,
        parsed='{"degraded": true, "degradation_reason": "skills fallback"}',
    )

    async with pg_pool.acquire() as conn:
        result = await resume_service.reset_for_reparse(conn, resume_id)

    assert result is True
    row = await _row(pg_pool, resume_id)
    assert row["status"] == "uploaded"
    assert row["parsed"] is None


# ── reset_for_reparse: ineligible rows, untouched ─────────────────────────


@pytest.mark.asyncio
async def test_reset_for_reparse_refuses_a_clean_parsed_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        blob_key="resumes/c.txt",
        status="parsed",
        failure_reason=None,
        parsed='{"degraded": false}',
    )

    async with pg_pool.acquire() as conn:
        result = await resume_service.reset_for_reparse(conn, resume_id)

    assert result is False
    row = await _row(pg_pool, resume_id)
    assert row["status"] == "parsed"
    assert row["parsed"] is not None


@pytest.mark.asyncio
async def test_reset_for_reparse_refuses_a_withdrawn_failed_row(
    pg_pool: asyncpg.Pool,
) -> None:
    import datetime as dt

    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        blob_key="resumes/d.txt",
        status="failed",
        withdrawn_at=dt.datetime.now(dt.UTC),
    )

    async with pg_pool.acquire() as conn:
        result = await resume_service.reset_for_reparse(conn, resume_id)

    assert result is False
    row = await _row(pg_pool, resume_id)
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_reset_for_reparse_refuses_an_in_flight_uploaded_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        blob_key="resumes/e.txt",
        status="uploaded",
        failure_reason=None,
    )

    async with pg_pool.acquire() as conn:
        result = await resume_service.reset_for_reparse(conn, resume_id)

    assert result is False
    row = await _row(pg_pool, resume_id)
    assert row["status"] == "uploaded"


@pytest.mark.asyncio
async def test_reset_for_reparse_refuses_an_in_flight_parsing_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool,
        job_id,
        blob_key="resumes/f.txt",
        status="parsing",
        failure_reason=None,
    )

    async with pg_pool.acquire() as conn:
        result = await resume_service.reset_for_reparse(conn, resume_id)

    assert result is False
    row = await _row(pg_pool, resume_id)
    assert row["status"] == "parsing"


# ── the reset row must actually be usable by the parse state machine ─────


@pytest.mark.asyncio
async def test_after_reset_the_state_machine_accepts_the_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """Proves the reset row is not just cosmetically 'uploaded' — it can
    actually be claimed and can actually record a subsequent failure, exactly
    the same path a fresh upload takes."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, blob_key="resumes/g.txt", status="failed"
    )

    async with pg_pool.acquire() as conn:
        reset_ok = await resume_service.reset_for_reparse(conn, resume_id)
    assert reset_ok is True

    async with pg_pool.acquire() as conn:
        claimed = await resume_service.claim_parsing(conn, resume_id)
    assert claimed is True

    async with pg_pool.acquire() as conn:
        await resume_service.record_parse_failure(conn, resume_id, "still broken")

    row = await _row(pg_pool, resume_id)
    assert row["status"] == "failed"
    assert row["failure_reason"] == "still broken"


# ── reconciler: a fresh re-parse must not be immediately re-queued ───────


@pytest.mark.asyncio
async def test_reconciler_skips_a_fresh_reparse_of_an_old_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """The double-enqueue guard the spec calls out: ``uploaded_at`` is 2h old
    (would trip the 30-minute stall window on its own), but
    ``reparse_requested_at`` is fresh — the reconciler must not pick this row
    up while the user's own re-parse is still in flight."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, blob_key="resumes/h.txt", status="failed"
    )
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE resumes SET uploaded_at = now() - interval '2 hours' "
            "WHERE id = $1",
            resume_id,
        )
        reset_ok = await resume_service.reset_for_reparse(conn, resume_id)
    assert reset_ok is True

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(reconcile._SELECT_STALLED)
    assert resume_id not in {r["id"] for r in rows}


@pytest.mark.asyncio
async def test_reconciler_does_pick_up_a_row_stale_on_both_timestamps(
    pg_pool: asyncpg.Pool,
) -> None:
    """Once BOTH ``uploaded_at`` and ``reparse_requested_at`` are old, the row
    really is stranded again and the reconciler must still catch it."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, blob_key="resumes/i.txt", status="failed"
    )
    async with pg_pool.acquire() as conn:
        reset_ok = await resume_service.reset_for_reparse(conn, resume_id)
        assert reset_ok is True
        await conn.execute(
            "UPDATE resumes SET uploaded_at = now() - interval '2 hours', "
            "reparse_requested_at = now() - interval '2 hours', "
            "status = 'uploaded' "
            "WHERE id = $1",
            resume_id,
        )

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(reconcile._SELECT_STALLED)
    assert resume_id in {r["id"] for r in rows}
