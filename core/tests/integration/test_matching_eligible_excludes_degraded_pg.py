"""Integration tests — live demo, 2026-09-09 21:42: a degraded parse wedges
ranking forever, against a REAL Postgres (testcontainers).

Ten résumés parsed for one job; one had its skills LLM pass fail (empty
content), fell back to the keyword scan, and was persisted with
``parsed->>'degraded' = true``. By design (FU-7 §4 / ADR-030,
``resume_tasks.py``'s ``degraded_skip_projection``) a degraded parse is NEVER
enqueued for projection — no Neo4j node, no ranking. But
``src.worker.matching_tasks._ELIGIBLE_SQL`` counted it as eligible anyway
(``status = 'parsed' AND withdrawn_at IS NULL`` says nothing about
``degraded``), so ``projected`` (9) could never catch ``eligible`` (10) — the
run deferred every one of ``shortlist_max_tries`` (20) x 45s (15 minutes,
silently) before ranking the nine it could reach. Two fail-closed decisions,
each correct alone, wedged the demo together.

Mirrors ``test_resume_degraded_visibility_pg.py``'s fixtures/helpers (the
``parsed`` jsonb payload shape, seeding the ``degraded`` flag directly in that
column exactly as a real degraded parse would leave it) and
``test_reverse_match_read_excludes_withdrawn_pg.py``'s ``UPDATE resumes SET
withdrawn_at = now()`` withdrawal helper.

What a REAL Postgres proves that a mocked-conn unit test cannot: the
``(parsed->>'degraded')::bool`` jsonb-boolean cast in ``_ELIGIBLE_SQL``
resolves against a REAL jsonb column (a JSON string on the wire, not a
pre-parsed dict) with the ``NOT`` predicate combined correctly with the
pre-existing ``withdrawn_at IS NULL`` predicate — a mocked ``conn.fetchval``
can only be told what number to return, never prove the WHERE clause actually
computes it.

``_ELIGIBLE_SQL`` does not yet exclude a degraded parse — every test below
fails its count assertion (the degraded/withdrawn row is still included) until
the predicate lands. RED half of the TDD cycle.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.worker.matching_tasks import _ELIGIBLE_SQL


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


def _parsed_payload(*, degraded: bool) -> dict[str, Any]:
    return {
        "candidate": {"name": "Jane Smith", "email": "jane.smith@example.test"},
        "summary": "Jane Smith is a senior engineer.",
        "total_years_experience": 8,
        "skills": [{"name": "python"}],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
        "degraded": degraded,
    }


async def _insert_job(pool: asyncpg.Pool) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) " "RETURNING id",
            "Senior Backend Engineer",
            "raw jd text (irrelevant to these tests)",
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool,
    job_id: UUID,
    *,
    status: str = "parsed",
    degraded: bool | None = None,
    withdrawn: bool = False,
) -> UUID:
    parsed = (
        None if degraded is None else json.dumps(_parsed_payload(degraded=degraded))
    )
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status, parsed
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE,
                       $4, $5::jsonb)
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
            status,
            parsed,
        )
        if withdrawn:
            await conn.execute(
                "UPDATE resumes SET withdrawn_at = now(), "
                "withdrawal_reason = 'test withdrawal' WHERE id = $1",
                resume_id,
            )
    return resume_id


# ── the reported shape: 3 parsed, 1 degraded → eligible == 2 ───────────────


@pytest.mark.asyncio
async def test_eligible_sql_excludes_a_degraded_parse(pg_pool: asyncpg.Pool) -> None:
    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, degraded=False)
    await _insert_resume(pg_pool, job_id, degraded=False)
    await _insert_resume(pg_pool, job_id, degraded=True)

    async with pg_pool.acquire() as conn:
        eligible = await conn.fetchval(_ELIGIBLE_SQL, job_id)

    assert eligible == 2, (
        "the degraded résumé is never projected by design (ADR-030) and must "
        "not count toward 'eligible', or the graph can never catch up"
    )


@pytest.mark.asyncio
async def test_eligible_sql_counts_zero_when_every_parse_is_degraded(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, degraded=True)
    await _insert_resume(pg_pool, job_id, degraded=True)

    async with pg_pool.acquire() as conn:
        eligible = await conn.fetchval(_ELIGIBLE_SQL, job_id)

    assert eligible == 0


@pytest.mark.asyncio
async def test_eligible_sql_counts_all_when_none_is_degraded(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, degraded=False)
    await _insert_resume(pg_pool, job_id, degraded=False)
    await _insert_resume(pg_pool, job_id, degraded=False)

    async with pg_pool.acquire() as conn:
        eligible = await conn.fetchval(_ELIGIBLE_SQL, job_id)

    assert eligible == 3


# ── a withdrawn résumé is still excluded, independently of degraded ───────


@pytest.mark.asyncio
async def test_eligible_sql_still_excludes_a_withdrawn_non_degraded_resume(
    pg_pool: asyncpg.Pool,
) -> None:
    """The pre-existing ``withdrawn_at IS NULL`` predicate must survive the
    new ``degraded`` predicate being ANDed alongside it, not replaced by it."""
    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, degraded=False)
    await _insert_resume(pg_pool, job_id, degraded=False, withdrawn=True)

    async with pg_pool.acquire() as conn:
        eligible = await conn.fetchval(_ELIGIBLE_SQL, job_id)

    assert eligible == 1


@pytest.mark.asyncio
async def test_eligible_sql_excludes_withdrawn_and_degraded_together(
    pg_pool: asyncpg.Pool,
) -> None:
    """Four résumés: one clean, one degraded, one withdrawn, one both
    withdrawn AND degraded — only the first is eligible."""
    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, degraded=False)
    await _insert_resume(pg_pool, job_id, degraded=True)
    await _insert_resume(pg_pool, job_id, degraded=False, withdrawn=True)
    await _insert_resume(pg_pool, job_id, degraded=True, withdrawn=True)

    async with pg_pool.acquire() as conn:
        eligible = await conn.fetchval(_ELIGIBLE_SQL, job_id)

    assert eligible == 1


@pytest.mark.asyncio
async def test_eligible_sql_does_not_crash_on_a_not_yet_parsed_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """A row that hasn't reached ``status = 'parsed'`` yet (``parsed IS
    NULL``) must not blow up the ``(parsed->>'degraded')::bool`` cast — it is
    already excluded by the ``status = 'parsed'`` predicate, and the new
    ``degraded`` predicate must not change that."""
    job_id = await _insert_job(pg_pool)
    await _insert_resume(pg_pool, job_id, status="uploaded", degraded=None)
    await _insert_resume(pg_pool, job_id, degraded=False)

    async with pg_pool.acquire() as conn:
        eligible = await conn.fetchval(_ELIGIBLE_SQL, job_id)

    assert eligible == 1
