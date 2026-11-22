"""Integration (real Postgres) -- RED for interview-question generator slice S2:
the ``interview_questions`` table and ``interview_question_service``.

What a real Postgres proves that the string-level unit tests cannot:

* the table exists with the contract's column types, and a **minimal INSERT
  supplying only ``job_id``, ``resume_id``, ``status`` and ``created_by``
  works** -- the NOT-NULL-without-DEFAULT trap that has already shipped once
  (``users.role``): ``questions`` and ``requested_at`` must default.
* the CHECK, UNIQUE and both cascading FKs actually fire.
* ``claim`` is an atomic upsert: exactly one of N concurrent callers wins, a
  pending row younger than the stall window refuses, an older one (or any
  terminal row) is claimable again.

The service module is imported lazily so a missing module fails each test on
its own instead of aborting collection of the suite.
"""

from __future__ import annotations

import asyncio
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
from src.services.interview_questions import (
    PROMPT_VERSION,
    Gap,
    InterviewQuestionItem,
    RefusalReason,
    gaps_hash,
)
from src.settings import Settings

_JD = "We need a senior backend engineer. " * 3


def _svc() -> Any:
    return importlib.import_module("src.services.interview_question_service")


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=12)
    await init_schema(pool)
    async with pool.acquire() as conn:
        # interview_questions is truncated through its FKs, NOT named here, so a
        # missing table fails the tests about it rather than every fixture.
        await conn.execute("TRUNCATE jobs, resumes, users, audit_log CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


async def _job(pool: asyncpg.Pool) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ('Eng', $1) RETURNING id",
            _JD,
        )
    return job_id


async def _resume(pool: asyncpg.Pool, job_id: UUID) -> UUID:
    async with pool.acquire() as conn:
        resume_id: UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'r.pdf', 'application/pdf', 1024, $3, TRUE, 'parsed')
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
        )
    return resume_id


async def _pair(pool: asyncpg.Pool) -> tuple[UUID, UUID]:
    job_id = await _job(pool)
    return job_id, await _resume(pool, job_id)


async def _row(pool: asyncpg.Pool, job_id: UUID, resume_id: UUID) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM interview_questions WHERE job_id = $1 AND resume_id = $2",
            job_id,
            resume_id,
        )
    assert row is not None
    return row


async def _backdate(
    pool: asyncpg.Pool, job_id: UUID, resume_id: UUID, minutes: int
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE interview_questions "
            "SET requested_at = now() - make_interval(mins => $3) "
            "WHERE job_id = $1 AND resume_id = $2",
            job_id,
            resume_id,
            minutes,
        )


# ── the table ────────────────────────────────────────────────────────────

_COLUMNS: dict[str, str] = {
    "id": "uuid",
    "job_id": "uuid",
    "resume_id": "uuid",
    "status": "text",
    "reason": "text",
    "questions": "jsonb",
    "gaps_sha256": "text",
    "prompt_version": "text",
    "model": "text",
    "created_by": "text",
    "requested_at": "timestamp with time zone",
    "generated_at": "timestamp with time zone",
    "blind_review": "boolean",
    "claim_id": "uuid",
}


@pytest.mark.asyncio
async def test_table_has_the_contract_columns_and_types(pg_pool: asyncpg.Pool) -> None:
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'interview_questions'"
        )
    by_name = {r["column_name"]: r for r in rows}
    assert {k: by_name[k]["data_type"] for k in _COLUMNS if k in by_name} == _COLUMNS
    assert set(by_name) == set(_COLUMNS)
    for not_null in (
        "id",
        "job_id",
        "resume_id",
        "status",
        "questions",
        "requested_at",
        "blind_review",
    ):
        assert by_name[not_null]["is_nullable"] == "NO", not_null
    for nullable in (
        "reason",
        "gaps_sha256",
        "prompt_version",
        "model",
        "generated_at",
        "claim_id",
    ):
        assert by_name[nullable]["is_nullable"] == "YES", nullable


@pytest.mark.asyncio
async def test_a_minimal_pending_insert_works_and_defaults_the_rest(
    pg_pool: asyncpg.Pool,
) -> None:
    """The NOT-NULL-without-DEFAULT trap: supply ONLY the four columns the
    service's claim supplies."""
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO interview_questions "
            "(job_id, resume_id, status, created_by) "
            "VALUES ($1, $2, 'pending', 'alice')",
            job_id,
            resume_id,
        )
    row = await _row(pg_pool, job_id, resume_id)
    assert isinstance(row["id"], UUID)
    assert json.loads(row["questions"]) == []
    assert row["requested_at"] is not None
    assert row["generated_at"] is None
    assert row["reason"] is None
    assert row["gaps_sha256"] is None
    assert row["created_by"] == "alice"


@pytest.mark.parametrize("status", ["pending", "ready", "failed", "refused"])
@pytest.mark.asyncio
async def test_each_contract_status_is_accepted(
    pg_pool: asyncpg.Pool, status: str
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO interview_questions "
            "(job_id, resume_id, status, created_by) VALUES ($1, $2, $3, 'alice')",
            job_id,
            resume_id,
            status,
        )


@pytest.mark.parametrize("status", ["bogus", "", "READY", "done"])
@pytest.mark.asyncio
async def test_a_status_outside_the_contract_is_rejected(
    pg_pool: asyncpg.Pool, status: str
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO interview_questions "
                "(job_id, resume_id, status, created_by) VALUES ($1, $2, $3, 'alice')",
                job_id,
                resume_id,
                status,
            )


@pytest.mark.asyncio
async def test_job_and_resume_are_unique_together(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    sql = (
        "INSERT INTO interview_questions "
        "(job_id, resume_id, status, created_by) VALUES ($1, $2, 'pending', 'alice')"
    )
    async with pg_pool.acquire() as conn:
        await conn.execute(sql, job_id, resume_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(sql, job_id, resume_id)


@pytest.mark.asyncio
async def test_questions_cannot_be_null(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(
                "INSERT INTO interview_questions "
                "(job_id, resume_id, status, created_by, questions) "
                "VALUES ($1, $2, 'pending', 'alice', NULL)",
                job_id,
                resume_id,
            )


@pytest.mark.asyncio
async def test_foreign_keys_reject_a_missing_job_or_resume(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    sql = (
        "INSERT INTO interview_questions "
        "(job_id, resume_id, status, created_by) VALUES ($1, $2, 'pending', 'alice')"
    )
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(sql, uuid.uuid4(), resume_id)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(sql, job_id, uuid.uuid4())


@pytest.mark.asyncio
async def test_deleting_the_resume_cascades_to_its_row_only(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _job(pg_pool)
    keep, drop = await _resume(pg_pool, job_id), await _resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        for rid in (keep, drop):
            await conn.execute(
                "INSERT INTO interview_questions "
                "(job_id, resume_id, status, created_by) "
                "VALUES ($1, $2, 'ready', 'alice')",
                job_id,
                rid,
            )
        await conn.execute("DELETE FROM resumes WHERE id = $1", drop)
        remaining = await conn.fetch("SELECT resume_id FROM interview_questions")
    assert [r["resume_id"] for r in remaining] == [keep]


@pytest.mark.asyncio
async def test_deleting_the_job_cascades_to_its_rows(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO interview_questions "
            "(job_id, resume_id, status, created_by) VALUES ($1, $2, 'ready', 'alice')",
            job_id,
            resume_id,
        )
        await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)
        count = await conn.fetchval("SELECT count(*) FROM interview_questions")
    assert count == 0


@pytest.mark.asyncio
async def test_init_schema_is_idempotent_with_the_table_populated(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO interview_questions "
            "(job_id, resume_id, status, created_by) VALUES ($1, $2, 'ready', 'alice')",
            job_id,
            resume_id,
        )
    await init_schema(pg_pool)
    assert (await _row(pg_pool, job_id, resume_id))["status"] == "ready"


# ── claim ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_claim_wins_and_writes_a_pending_row(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _svc().claim(conn, job_id, resume_id, "alice") is not None
    row = await _row(pg_pool, job_id, resume_id)
    assert row["status"] == "pending"
    assert row["created_by"] == "alice"
    assert row["reason"] is None
    assert json.loads(row["questions"]) == []


@pytest.mark.asyncio
async def test_claim_accepts_an_anonymous_creator(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _svc().claim(conn, job_id, resume_id, None) is not None
    assert (await _row(pg_pool, job_id, resume_id))["created_by"] is None


@pytest.mark.asyncio
async def test_a_second_claim_on_a_fresh_pending_row_loses(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _svc().claim(conn, job_id, resume_id, "alice") is not None
        before = await _row(pg_pool, job_id, resume_id)
        assert await _svc().claim(conn, job_id, resume_id, "bob") is None
    after = await _row(pg_pool, job_id, resume_id)
    assert after["requested_at"] == before["requested_at"]
    assert after["created_by"] == "alice"


@pytest.mark.asyncio
async def test_a_stale_pending_row_can_be_reclaimed_after_the_stall_window(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await _svc().claim(conn, job_id, resume_id, "alice")
        await _backdate(pg_pool, job_id, resume_id, 21)
        old = (await _row(pg_pool, job_id, resume_id))["requested_at"]
        assert await _svc().claim(conn, job_id, resume_id, "bob") is not None
    row = await _row(pg_pool, job_id, resume_id)
    assert row["status"] == "pending"
    assert row["requested_at"] > old
    assert row["created_by"] == "bob"


@pytest.mark.asyncio
async def test_a_pending_row_inside_the_stall_window_is_not_reclaimable(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await _svc().claim(conn, job_id, resume_id, "alice")
        await _backdate(pg_pool, job_id, resume_id, 19)
        assert await _svc().claim(conn, job_id, resume_id, "bob") is None


@pytest.mark.asyncio
async def test_the_stall_window_comes_from_settings(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard-coded 20 would pass the two tests above; this one moves the
    window to 15 minutes (above the honest-window floor of 13 for the default
    LLM timeout) and expects a 16-minute-old pending row to be claimable.
    The service must therefore read ``get_settings`` from its own module."""
    monkeypatch.setattr(
        _svc(), "get_settings", lambda: Settings(interview_questions_stall_minutes=15)
    )
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await _svc().claim(conn, job_id, resume_id, "alice")
        await _backdate(pg_pool, job_id, resume_id, 16)
        assert await _svc().claim(conn, job_id, resume_id, "bob") is not None


async def _terminate(
    conn: Any, how: str, job_id: UUID, resume_id: UUID, cid: UUID | None
) -> None:
    svc = _svc()
    if how == "ready":
        gaps = [Gap(index=2, requirement="Budget ownership", status="missing")]
        items = [
            InterviewQuestionItem(requirement_index=2, question="Describe a budget.")
        ]
        await svc.record_ready(
            conn,
            job_id,
            resume_id,
            questions=items,
            gaps=gaps,
            model="m",
            claim_id=cid,
        )
    elif how == "failed":
        await svc.record_failed(conn, job_id, resume_id, "invalid_output", claim_id=cid)
    else:
        await svc.record_refused(
            conn, job_id, resume_id, RefusalReason.NO_GAPS, claim_id=cid
        )


@pytest.mark.parametrize("how", ["ready", "failed", "refused"])
@pytest.mark.asyncio
async def test_a_terminal_row_is_always_reclaimable_and_goes_back_to_pending(
    pg_pool: asyncpg.Pool, how: str
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        cid = await _svc().claim(conn, job_id, resume_id, "alice")
        await _terminate(conn, how, job_id, resume_id, cid)
        assert (await _row(pg_pool, job_id, resume_id))["status"] == how
        assert await _svc().claim(conn, job_id, resume_id, "bob") is not None
    row = await _row(pg_pool, job_id, resume_id)
    assert row["status"] == "pending"
    assert row["reason"] is None


@pytest.mark.asyncio
async def test_exactly_one_of_many_concurrent_claims_wins(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)

    async def _try(i: int) -> bool:
        async with pg_pool.acquire() as conn:
            cid = await _svc().claim(conn, job_id, resume_id, f"user-{i}")
            return cid is not None

    results = await asyncio.gather(*(_try(i) for i in range(8)))
    assert results.count(True) == 1
    async with pg_pool.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM interview_questions")
    assert count == 1


@pytest.mark.asyncio
async def test_claims_on_different_pairs_do_not_interfere(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _job(pg_pool)
    a, b = await _resume(pg_pool, job_id), await _resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        assert await _svc().claim(conn, job_id, a, "alice") is not None
        assert await _svc().claim(conn, job_id, b, "alice") is not None


# ── record_* / get ───────────────────────────────────────────────────────


def _gaps() -> list[Gap]:
    return [
        Gap(
            index=1,
            requirement="Kubernetes operations",
            status="partial",
            quote="Deployed a pod once in a lab",
        ),
        Gap(index=2, requirement="Budget ownership", status="missing"),
    ]


def _items(*texts: str) -> list[InterviewQuestionItem]:
    return [
        InterviewQuestionItem(requirement_index=1 + i % 2, question=t)
        for i, t in enumerate(texts)
    ]


@pytest.mark.asyncio
async def test_get_is_none_when_nothing_was_claimed(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _svc().get(conn, job_id, resume_id) is None


@pytest.mark.asyncio
async def test_get_of_a_fresh_claim_is_pending_with_no_questions(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await _svc().claim(conn, job_id, resume_id, "alice")
        rec = await _svc().get(conn, job_id, resume_id)
    assert rec.status == "pending"
    assert rec.questions == []
    assert rec.gaps_sha256 is None
    assert rec.generated_at is None


@pytest.mark.asyncio
async def test_record_ready_round_trips_through_real_jsonb(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    gaps = _gaps()
    async with pg_pool.acquire() as conn:
        cid = await _svc().claim(conn, job_id, resume_id, "alice")
        await _svc().record_ready(
            conn,
            job_id,
            resume_id,
            questions=[
                InterviewQuestionItem(
                    requirement_index=1,
                    question="Walk me through the lab deployment -- what broke?",
                    probes_quote="Deployed a pod once in a lab",
                ),
                InterviewQuestionItem(
                    requirement_index=2, question="Describe a budget you owned."
                ),
            ],
            gaps=gaps,
            model="m-test",
            claim_id=cid,
        )
        rec = await _svc().get(conn, job_id, resume_id)
    assert rec.status == "ready"
    assert rec.reason is None
    assert rec.questions == [
        {
            "requirement_index": 1,
            "requirement": "Kubernetes operations",
            "question": "Walk me through the lab deployment -- what broke?",
            "probes_quote": "Deployed a pod once in a lab",
        },
        {
            "requirement_index": 2,
            "requirement": "Budget ownership",
            "question": "Describe a budget you owned.",
            "probes_quote": None,
        },
    ]
    assert rec.gaps_sha256 == gaps_hash(gaps)
    assert rec.prompt_version == PROMPT_VERSION
    assert rec.model == "m-test"
    assert rec.generated_at is not None


@pytest.mark.asyncio
async def test_record_ready_replaces_rather_than_appends(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        cid = await _svc().claim(conn, job_id, resume_id, "alice")
        await _svc().record_ready(
            conn,
            job_id,
            resume_id,
            questions=_items("First?", "Second?"),
            gaps=_gaps(),
            model="m1",
            claim_id=cid,
        )
        cid = await _svc().claim(conn, job_id, resume_id, "alice")
        await _svc().record_ready(
            conn,
            job_id,
            resume_id,
            questions=_items("Third?"),
            gaps=_gaps(),
            model="m2",
            claim_id=cid,
        )
        rec = await _svc().get(conn, job_id, resume_id)
        count = await conn.fetchval("SELECT count(*) FROM interview_questions")
    assert [q["question"] for q in rec.questions] == ["Third?"]
    assert rec.model == "m2"
    assert count == 1


@pytest.mark.asyncio
async def test_record_failed_stores_status_and_fixed_reason(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        cid = await _svc().claim(conn, job_id, resume_id, "alice")
        await _svc().record_failed(conn, job_id, resume_id, "entry_gone", claim_id=cid)
        rec = await _svc().get(conn, job_id, resume_id)
    assert (rec.status, rec.reason) == ("failed", "entry_gone")
    assert rec.questions == []


@pytest.mark.asyncio
async def test_record_refused_stores_status_and_the_refusal_reason(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        cid = await _svc().claim(conn, job_id, resume_id, "alice")
        await _svc().record_refused(
            conn, job_id, resume_id, RefusalReason.NOT_EVALUATED, claim_id=cid
        )
        rec = await _svc().get(conn, job_id, resume_id)
    assert (rec.status, rec.reason) == ("refused", "not_evaluated")


@pytest.mark.asyncio
async def test_free_text_reasons_never_reach_the_table(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        cid = await _svc().claim(conn, job_id, resume_id, "alice")
        with pytest.raises(ValueError):
            await _svc().record_failed(
                conn, job_id, resume_id, "Jane Smith failed", claim_id=cid
            )
        with pytest.raises(ValueError):
            await _svc().record_refused(
                conn, job_id, resume_id, "Jane Smith refused", claim_id=cid
            )
        rec = await _svc().get(conn, job_id, resume_id)
    assert (rec.status, rec.reason) == ("pending", None)


# ── review round 1: blind_review ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_blind_review_defaults_false_on_the_claims_minimal_insert(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO interview_questions "
            "(job_id, resume_id, status, created_by) "
            "VALUES ($1, $2, 'pending', 'alice')",
            job_id,
            resume_id,
        )
    assert (await _row(pg_pool, job_id, resume_id))["blind_review"] is False


@pytest.mark.asyncio
async def test_blind_review_cannot_be_null(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(
                "INSERT INTO interview_questions "
                "(job_id, resume_id, status, created_by, blind_review) "
                "VALUES ($1, $2, 'pending', 'alice', NULL)",
                job_id,
                resume_id,
            )


@pytest.mark.asyncio
async def test_init_schema_adds_blind_review_to_a_table_that_predates_it(
    pg_pool: asyncpg.Pool,
) -> None:
    """``CREATE TABLE IF NOT EXISTS`` skips an existing table, so a deployment
    that already has ``interview_questions`` needs the dedicated ALTER; its
    existing rows must come out ``false`` (the conservative stamp)."""
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute("ALTER TABLE interview_questions DROP COLUMN blind_review")
        await conn.execute(
            "INSERT INTO interview_questions "
            "(job_id, resume_id, status, created_by) VALUES ($1, $2, 'ready', 'a')",
            job_id,
            resume_id,
        )
    await init_schema(pg_pool)
    assert (await _row(pg_pool, job_id, resume_id))["blind_review"] is False


@pytest.mark.parametrize("blind", [True, False])
@pytest.mark.asyncio
async def test_record_ready_persists_the_blind_review_stamp(
    blind: bool, pg_pool: asyncpg.Pool
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        cid = await _svc().claim(conn, job_id, resume_id, "alice")
        await _svc().record_ready(
            conn,
            job_id,
            resume_id,
            questions=[],
            gaps=[],
            model="m",
            claim_id=cid,
            blind_review=blind,
        )
        rec = await _svc().get(conn, job_id, resume_id)
    assert (await _row(pg_pool, job_id, resume_id))["blind_review"] is blind
    assert rec.blind_review is blind
    assert rec.requested_at is not None


@pytest.mark.asyncio
async def test_a_regenerate_overwrites_the_previous_blind_stamp(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        for blind in (True, False):
            cid = await _svc().claim(conn, job_id, resume_id, "alice")
            await _svc().record_ready(
                conn,
                job_id,
                resume_id,
                questions=[],
                gaps=[],
                model="m",
                claim_id=cid,
                blind_review=blind,
            )
            assert (await _row(pg_pool, job_id, resume_id))["blind_review"] is blind


@pytest.mark.asyncio
async def test_record_failed_accepts_llm_unavailable(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        cid = await _svc().claim(conn, job_id, resume_id, "alice")
        await _svc().record_failed(
            conn, job_id, resume_id, "llm_unavailable", claim_id=cid
        )
    row = await _row(pg_pool, job_id, resume_id)
    assert (row["status"], row["reason"]) == ("failed", "llm_unavailable")
