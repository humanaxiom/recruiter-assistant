"""Integration (real Postgres) -- RED for interview-question generator review
round 2, G1 and G2. The contract is documented in
``tests/unit/test_interview_questions_round2_claim.py``.

What only a real database proves here:

* a stale run (claim A) can never overwrite the newer claim's (B's) settle;
* ``claim_id`` is minted fresh by every winning claim and is untouched by a
  losing one;
* ``heartbeat`` refreshes ``requested_at`` only for the live claim, and a
  refreshed row is no longer reclaimable;
* the stall window ``claim()`` applies is the honest one (900 s model timeout,
  2 retries -> 52 minutes), not the raw 20-minute knob.
"""

from __future__ import annotations

import importlib
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
from src.services.interview_questions import RefusalReason
from src.settings import Settings
from tests.integration.test_interview_questions_ddl_pg import (
    _backdate,
    _gaps,
    _items,
    _pair,
    _row,
)


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=12)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, users, audit_log CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


def _svc() -> Any:
    return importlib.import_module("src.services.interview_question_service")


async def _claim(pool: asyncpg.Pool, job_id: UUID, resume_id: UUID, who: str) -> Any:
    async with pool.acquire() as conn:
        return await _svc().claim(conn, job_id, resume_id, who)


async def _ready(
    pool: asyncpg.Pool, job_id: UUID, resume_id: UUID, cid: Any, text: str
) -> Any:
    async with pool.acquire() as conn:
        return await _svc().record_ready(
            conn,
            job_id,
            resume_id,
            claim_id=cid,
            questions=_items(text),
            gaps=_gaps(),
            model="m",
        )


async def _failed(
    pool: asyncpg.Pool, job_id: UUID, resume_id: UUID, cid: Any, reason: str
) -> Any:
    async with pool.acquire() as conn:
        return await _svc().record_failed(conn, job_id, resume_id, reason, claim_id=cid)


# -- column -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_id_is_a_nullable_uuid_column(pg_pool: asyncpg.Pool) -> None:
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'interview_questions' AND column_name = 'claim_id'"
        )
    assert row is not None
    assert (row["data_type"], row["is_nullable"]) == ("uuid", "YES")


@pytest.mark.asyncio
async def test_init_schema_adds_claim_id_to_a_table_that_predates_it(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute("ALTER TABLE interview_questions DROP COLUMN claim_id")
        await conn.execute(
            "INSERT INTO interview_questions "
            "(job_id, resume_id, status, created_by) VALUES ($1, $2, 'ready', 'a')",
            job_id,
            resume_id,
        )
    await init_schema(pg_pool)
    assert (await _row(pg_pool, job_id, resume_id))["claim_id"] is None


# -- G1: claim ids ----------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_returns_a_uuid_that_is_stored_on_the_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    cid = await _claim(pg_pool, job_id, resume_id, "alice")
    assert isinstance(cid, UUID)
    assert (await _row(pg_pool, job_id, resume_id))["claim_id"] == cid


@pytest.mark.asyncio
async def test_every_winning_claim_mints_a_fresh_id(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    assert await _ready(pg_pool, job_id, resume_id, a, "One?") is True
    b = await _claim(pg_pool, job_id, resume_id, "alice")
    assert isinstance(b, UUID) and b != a
    assert (await _row(pg_pool, job_id, resume_id))["claim_id"] == b


@pytest.mark.asyncio
async def test_a_losing_claim_leaves_the_live_claim_id_alone(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    assert await _claim(pg_pool, job_id, resume_id, "bob") is None
    assert (await _row(pg_pool, job_id, resume_id))["claim_id"] == a
    assert await _ready(pg_pool, job_id, resume_id, a, "Mine?") is True


# -- G1: a stale run cannot overwrite a newer claim -------------------------


@pytest.mark.asyncio
async def test_a_stale_failure_cannot_overwrite_a_newer_claims_ready_set(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    await _backdate(pg_pool, job_id, resume_id, 600)  # A is presumed dead
    b = await _claim(pg_pool, job_id, resume_id, "bob")
    assert b is not None and b != a

    assert await _ready(pg_pool, job_id, resume_id, b, "B question?") is True
    assert await _failed(pg_pool, job_id, resume_id, a, "llm_unavailable") is False

    row = await _row(pg_pool, job_id, resume_id)
    assert row["status"] == "ready"
    assert row["reason"] is None
    assert [q["question"] for q in json.loads(row["questions"])] == ["B question?"]


@pytest.mark.asyncio
async def test_a_stale_ready_cannot_overwrite_a_newer_claims_pending_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    await _backdate(pg_pool, job_id, resume_id, 600)
    b = await _claim(pg_pool, job_id, resume_id, "bob")

    assert await _ready(pg_pool, job_id, resume_id, a, "A late answer?") is False

    row = await _row(pg_pool, job_id, resume_id)
    assert (row["status"], row["claim_id"]) == ("pending", b)
    assert json.loads(row["questions"]) == []


@pytest.mark.asyncio
async def test_a_stale_refusal_cannot_overwrite_a_newer_claims_ready_set(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    await _backdate(pg_pool, job_id, resume_id, 600)
    b = await _claim(pg_pool, job_id, resume_id, "bob")
    assert await _ready(pg_pool, job_id, resume_id, b, "B?") is True

    async with pg_pool.acquire() as conn:
        got = await _svc().record_refused(
            conn, job_id, resume_id, RefusalReason.NO_GAPS, claim_id=a
        )

    assert got is False
    assert (await _row(pg_pool, job_id, resume_id))["status"] == "ready"


@pytest.mark.asyncio
async def test_the_live_claim_settles_once_and_only_while_pending(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    assert await _ready(pg_pool, job_id, resume_id, a, "First?") is True
    # same claim id, but the row is no longer pending
    assert await _failed(pg_pool, job_id, resume_id, a, "invalid_output") is False
    assert await _ready(pg_pool, job_id, resume_id, a, "Second?") is False
    row = await _row(pg_pool, job_id, resume_id)
    assert row["status"] == "ready"
    assert [q["question"] for q in json.loads(row["questions"])] == ["First?"]


@pytest.mark.asyncio
async def test_a_settle_for_a_row_that_does_not_exist_is_a_noop_not_an_error(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    got = await _failed(pg_pool, job_id, resume_id, uuid.uuid4(), "entry_gone")
    assert got is False
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM interview_questions")
    assert count == 0


@pytest.mark.asyncio
async def test_a_settle_with_the_wrong_claim_leaves_a_pending_row_pending(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    got = await _failed(pg_pool, job_id, resume_id, uuid.uuid4(), "entry_gone")
    assert got is False
    row = await _row(pg_pool, job_id, resume_id)
    assert (row["status"], row["claim_id"]) == ("pending", a)


# -- G2: heartbeat ----------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_refreshes_requested_at_for_the_live_claim(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    await _backdate(pg_pool, job_id, resume_id, 10)
    old = (await _row(pg_pool, job_id, resume_id))["requested_at"]
    async with pg_pool.acquire() as conn:
        assert await _svc().heartbeat(conn, job_id, resume_id, claim_id=a) is True
        age = await conn.fetchval(
            "SELECT extract(epoch FROM now() - requested_at) "
            "FROM interview_questions WHERE job_id = $1 AND resume_id = $2",
            job_id,
            resume_id,
        )
    assert (await _row(pg_pool, job_id, resume_id))["requested_at"] > old
    assert float(age) < 60


@pytest.mark.asyncio
async def test_heartbeat_ignores_a_stale_claim_and_a_settled_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    await _backdate(pg_pool, job_id, resume_id, 10)
    before = (await _row(pg_pool, job_id, resume_id))["requested_at"]
    async with pg_pool.acquire() as conn:
        stale = await _svc().heartbeat(conn, job_id, resume_id, claim_id=uuid.uuid4())
        assert stale is False
        assert (await _row(pg_pool, job_id, resume_id))["requested_at"] == before
        assert await _ready(pg_pool, job_id, resume_id, a, "Q?") is True
        assert await _svc().heartbeat(conn, job_id, resume_id, claim_id=a) is False


@pytest.mark.asyncio
async def test_a_heartbeat_keeps_a_long_running_claim_from_being_reclaimed(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    await _backdate(pg_pool, job_id, resume_id, 25)  # past the raw 20-minute knob
    async with pg_pool.acquire() as conn:
        assert await _svc().heartbeat(conn, job_id, resume_id, claim_id=a) is True
    assert await _claim(pg_pool, job_id, resume_id, "bob") is None
    assert (await _row(pg_pool, job_id, resume_id))["claim_id"] == a


# -- G2: the effective window in the real claim SQL -------------------------


def _slow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _svc(),
        "get_settings",
        lambda: Settings(llm_timeout_s=900.0, llm_max_retries=2),
    )


@pytest.mark.parametrize("minutes", [21, 30, 51])
@pytest.mark.asyncio
async def test_a_pending_row_inside_the_effective_window_is_not_reclaimable(
    minutes: int, pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow(monkeypatch)
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    await _backdate(pg_pool, job_id, resume_id, minutes)
    assert await _claim(pg_pool, job_id, resume_id, "bob") is None
    assert (await _row(pg_pool, job_id, resume_id))["claim_id"] == a


@pytest.mark.parametrize("minutes", [53, 120])
@pytest.mark.asyncio
async def test_a_pending_row_past_the_effective_window_is_reclaimable(
    minutes: int, pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _slow(monkeypatch)
    job_id, resume_id = await _pair(pg_pool)
    a = await _claim(pg_pool, job_id, resume_id, "alice")
    await _backdate(pg_pool, job_id, resume_id, minutes)
    b = await _claim(pg_pool, job_id, resume_id, "bob")
    assert b is not None and b != a
