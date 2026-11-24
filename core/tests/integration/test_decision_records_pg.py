"""Integration (real Postgres) -- RED for the human decision record, slice S2:
the ``decision_records`` table and ``src.services.decision_service``.

What a real Postgres proves that the string-level unit tests cannot:

* the table exists with the contract's column types, and a **minimal INSERT
  supplying only the NOT-NULL-without-default columns works** (the trap that
  has shipped before: ``users.role``) -- ``id``, ``seq``, ``decided_at`` and
  ``snap_blind_review`` must default.
* the CHECKs actually fire (rationale 10..1000 after ``btrim``, decision set),
  both cascading FKs fire, ``seq`` is GENERATED ALWAYS.
* the service is append-only: a second decision supersedes without touching the
  first row; an exact repeat inserts nothing.
* ``persist_shortlist`` regenerating a shortlist (DELETE + INSERT of
  ``shortlist_entries``) leaves every decision intact, because decisions are
  keyed by (job, resume), never by shortlist-entry id.

The service module is imported lazily so a missing module fails each test on
its own instead of aborting collection of the suite.
"""

from __future__ import annotations

import datetime as dt
import importlib
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import ShortlistResult, ShortlistResultEntry
from src.schemas.auth import User
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    PipelineMeta,
    ScoreBreakdown,
    ShortlistEntry,
)
from src.services.shortlist_service import persist_shortlist

_JD = "We need a senior backend engineer. " * 3
_NOW = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.UTC)
_RATIONALE = "Strong systems background, fits the team."


def _svc() -> Any:
    return importlib.import_module("src.services.decision_service")


def _schemas() -> Any:
    return importlib.import_module("src.schemas.decisions")


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        # decision_records is truncated through its FKs, NOT named here, so a
        # missing table fails the tests about it rather than every fixture.
        await conn.execute("TRUNCATE jobs, resumes, users, audit_log CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


async def _job(pool: asyncpg.Pool, *, blind: bool = False) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ('Eng', $1, $2) RETURNING id",
            _JD,
            blind,
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


async def _raw_insert(
    pool: asyncpg.Pool,
    job_id: UUID,
    resume_id: UUID,
    *,
    decision: str = "accept",
    rationale: str = _RATIONALE,
) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO decision_records "
            "(job_id, resume_id, decision, rationale, decided_by, decided_by_role) "
            "VALUES ($1, $2, $3, $4, 'alice', 'recruiter') RETURNING *",
            job_id,
            resume_id,
            decision,
            rationale,
        )
    assert row is not None
    return row


async def _count(pool: asyncpg.Pool, job_id: UUID, resume_id: UUID) -> int:
    async with pool.acquire() as conn:
        n: int = await conn.fetchval(
            "SELECT count(*) FROM decision_records WHERE job_id=$1 AND resume_id=$2",
            job_id,
            resume_id,
        )
    return n


def _user(*, role: str = "recruiter", name: str = "alice") -> User:
    # A stable id per name, so the same reviewer is the same user on every call
    # (and _ensure_user can create the row audit_log's actor FK requires).
    return User(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"decision-test-user:{name}"),
        cas_username=name,
        display_name=name,
        email=None,
        role=role,
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
    )


def _meta() -> PipelineMeta:
    return PipelineMeta(
        model_gen="test-gen",
        model_emb="test-emb",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=DEFAULT_WEIGHTS,
        git_sha="abc123",
        generated_at=dt.datetime.now(dt.UTC),
        timings_ms={},
    )


async def _persist(
    pool: asyncpg.Pool, job_id: UUID, resume_id: UUID, *, rank: int, score: float
) -> ShortlistEntry:
    result = ShortlistResult(
        job_id=job_id,
        entries=[
            ShortlistResultEntry(
                resume_id=resume_id,
                rank=rank,
                score_final=score,
                score_structured=0.8,
                score_evidence=0.7,
                breakdown=_breakdown(),
                evidence=None,
            )
        ],
        pipeline_meta=_meta(),
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            await persist_shortlist(conn, result)
        row = await conn.fetchrow(
            "SELECT id FROM shortlist_entries WHERE job_id=$1 AND resume_id=$2",
            job_id,
            resume_id,
        )
    assert row is not None
    return ShortlistEntry.model_validate(
        {
            "id": row["id"],
            "job_id": job_id,
            "resume_id": resume_id,
            "rank": rank,
            "score_final": score,
            "score_breakdown": _breakdown(),
            "evidence": {"requirements": []},
            "pipeline_meta": None,
            "generated_at": _NOW,
        }
    )


async def _record(
    pool: asyncpg.Pool,
    entry: ShortlistEntry,
    *,
    decision: str = "accept",
    rationale: str = _RATIONALE,
    user: User | None = None,
) -> tuple[Any, bool]:
    body = _schemas().DecisionIn.model_validate(
        {"decision": decision, "rationale": rationale}
    )
    who = user or _user()
    async with pool.acquire() as conn:
        await _ensure_user(conn, who)
        return await _svc().record(conn, entry=entry, user=who, body=body)


_DEV_SENTINEL_ID = UUID("00000000-0000-0000-0000-000000000000")


async def _ensure_user(conn: asyncpg.Connection, user: User) -> None:
    """A real (non-sentinel) reviewer is audited with ``actor_user_id``, a real
    FK to ``users`` -- so the row has to exist, as it does for a signed-in user."""
    if user.id == _DEV_SENTINEL_ID:
        return
    await conn.execute(
        "INSERT INTO users (id, cas_username, display_name, email, role) "
        "VALUES ($1, $2, $2, NULL, $3) ON CONFLICT DO NOTHING",
        user.id,
        user.cas_username,
        user.role,
    )


# -- the table -------------------------------------------------------------

_COLUMNS: dict[str, str] = {
    "id": "uuid",
    "seq": "bigint",
    "job_id": "uuid",
    "resume_id": "uuid",
    "decision": "text",
    "rationale": "text",
    "decided_by": "text",
    "decided_by_user_id": "uuid",
    "decided_by_role": "text",
    "decided_at": "timestamp with time zone",
    "snap_rank": "integer",
    "snap_score_final": "double precision",
    "snap_entry_id": "uuid",
    "snap_blind_review": "boolean",
}


@pytest.mark.asyncio
async def test_table_has_exactly_the_contract_columns(pg_pool: asyncpg.Pool) -> None:
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'decision_records'"
        )
    assert {r["column_name"]: r["data_type"] for r in rows} == _COLUMNS


@pytest.mark.asyncio
async def test_minimal_insert_works_and_defaults_fill_in(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    row = await _raw_insert(pg_pool, job_id, resume_id)
    assert isinstance(row["id"], UUID)
    assert row["seq"] >= 1
    assert row["decided_at"] is not None
    assert row["snap_blind_review"] is False
    assert row["snap_rank"] is None
    assert row["snap_score_final"] is None
    assert row["snap_entry_id"] is None
    assert row["decided_by_user_id"] is None


@pytest.mark.asyncio
async def test_seq_increases_across_inserts(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    first = await _raw_insert(pg_pool, job_id, resume_id)
    second = await _raw_insert(pg_pool, job_id, resume_id, decision="hold")
    assert second["seq"] > first["seq"]


@pytest.mark.asyncio
async def test_seq_is_generated_always_and_cannot_be_supplied(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.GeneratedAlwaysError):
            await conn.execute(
                "INSERT INTO decision_records (seq, job_id, resume_id, decision, "
                "rationale, decided_by, decided_by_role) "
                "VALUES (1, $1, $2, 'accept', $3, 'a', 'recruiter')",
                job_id,
                resume_id,
                _RATIONALE,
            )


@pytest.mark.parametrize("bad", ["", "approve", "ACCEPT", "advance"])
@pytest.mark.asyncio
async def test_check_rejects_a_decision_outside_the_set(
    pg_pool: asyncpg.Pool, bad: str
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    with pytest.raises(asyncpg.CheckViolationError):
        await _raw_insert(pg_pool, job_id, resume_id, decision=bad)


@pytest.mark.parametrize(
    "rationale",
    ["a" * 9, "   " + "a" * 9 + "   ", " " * 40, "a" * 1001, ""],
)
@pytest.mark.asyncio
async def test_check_rejects_a_rationale_outside_10_to_1000_after_btrim(
    pg_pool: asyncpg.Pool, rationale: str
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    with pytest.raises(asyncpg.CheckViolationError):
        await _raw_insert(pg_pool, job_id, resume_id, rationale=rationale)
    assert await _count(pg_pool, job_id, resume_id) == 0


@pytest.mark.parametrize("rationale", ["a" * 10, "a" * 1000, "  " + "a" * 10 + "  "])
@pytest.mark.asyncio
async def test_check_accepts_the_boundary_rationales(
    pg_pool: asyncpg.Pool, rationale: str
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    await _raw_insert(pg_pool, job_id, resume_id, rationale=rationale)
    assert await _count(pg_pool, job_id, resume_id) == 1


@pytest.mark.asyncio
async def test_foreign_keys_reject_unknown_job_or_resume(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _raw_insert(pg_pool, uuid.uuid4(), resume_id)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _raw_insert(pg_pool, job_id, uuid.uuid4())


@pytest.mark.asyncio
async def test_deleting_the_resume_cascades_its_decisions(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    await _raw_insert(pg_pool, job_id, resume_id)
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM resumes WHERE id = $1", resume_id)
    assert await _count(pg_pool, job_id, resume_id) == 0


@pytest.mark.asyncio
async def test_deleting_the_job_cascades_its_decisions(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    await _raw_insert(pg_pool, job_id, resume_id)
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)
    assert await _count(pg_pool, job_id, resume_id) == 0


@pytest.mark.asyncio
async def test_init_schema_is_idempotent_and_keeps_rows(pg_pool: asyncpg.Pool) -> None:
    job_id, resume_id = await _pair(pg_pool)
    await _raw_insert(pg_pool, job_id, resume_id)
    await init_schema(pg_pool)
    assert await _count(pg_pool, job_id, resume_id) == 1


@pytest.mark.asyncio
async def test_lookup_index_exists(pg_pool: asyncpg.Pool) -> None:
    async with pg_pool.acquire() as conn:
        names = {
            r["indexname"]
            for r in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename='decision_records'"
            )
        }
    assert "decision_records_job_resume_idx" in names


# -- the service -----------------------------------------------------------


@pytest.mark.asyncio
async def test_record_inserts_with_a_snapshot_of_rank_score_entry_and_blind(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=3, score=0.4321)
    out, created = await _record(pg_pool, entry)
    assert created is True
    assert out.decision == "accept"
    assert out.snap_rank == 3
    assert out.snap_score_final == pytest.approx(0.4321)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM decision_records WHERE id = $1", out.id
        )
    assert row is not None
    assert row["snap_entry_id"] == entry.id
    assert row["snap_blind_review"] is False
    assert row["decided_by"] == "alice"
    assert row["decided_by_role"] == "recruiter"


@pytest.mark.asyncio
async def test_record_stores_the_session_user_id_for_a_real_user(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    user = _user(role="hiring_manager", name="hm-bob")
    out, _ = await _record(pg_pool, entry, user=user)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT decided_by, decided_by_user_id, decided_by_role "
            "FROM decision_records WHERE id = $1",
            out.id,
        )
    assert row is not None
    assert row["decided_by"] == "hm-bob"
    assert row["decided_by_user_id"] == user.id
    assert row["decided_by_role"] == "hiring_manager"


@pytest.mark.asyncio
async def test_record_dev_sentinel_stores_a_null_user_id(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    dev = _user(role="admin", name="dev-anonymous").model_copy(
        update={"id": UUID("00000000-0000-0000-0000-000000000000")}
    )
    out, _ = await _record(pg_pool, entry, user=dev)
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT decided_by, decided_by_user_id FROM decision_records "
            "WHERE id = $1",
            out.id,
        )
    assert row is not None
    assert row["decided_by"] == "dev-anonymous"
    assert row["decided_by_user_id"] is None


@pytest.mark.asyncio
async def test_record_snapshots_a_blind_job(pg_pool: asyncpg.Pool) -> None:
    job_id = await _job(pg_pool, blind=True)
    resume_id = await _resume(pg_pool, job_id)
    entry = (await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)).model_copy(
        update={"blinded": True}
    )
    out, _ = await _record(pg_pool, entry)
    assert out.snap_blind_review is True


@pytest.mark.asyncio
async def test_exact_repeat_is_idempotent_one_row_created_false(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    first, created_first = await _record(pg_pool, entry)
    again, created_again = await _record(pg_pool, entry)
    assert created_first is True and created_again is False
    assert again.id == first.id
    assert await _count(pg_pool, job_id, resume_id) == 1


@pytest.mark.asyncio
async def test_a_different_reviewer_with_the_same_text_is_a_new_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    await _record(pg_pool, entry, user=_user(name="alice"))
    _, created = await _record(pg_pool, entry, user=_user(name="carol"))
    assert created is True
    assert await _count(pg_pool, job_id, resume_id) == 2


@pytest.mark.asyncio
async def test_repeating_an_older_decision_after_a_newer_one_is_a_new_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """Idempotency compares only the LATEST row: accept, reject, accept again is
    three decisions, not two (the third supersedes the second)."""
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    await _record(pg_pool, entry, decision="accept")
    await _record(pg_pool, entry, decision="reject")
    _, created = await _record(pg_pool, entry, decision="accept")
    assert created is True
    assert await _count(pg_pool, job_id, resume_id) == 3


@pytest.mark.asyncio
async def test_append_only_a_new_decision_leaves_the_first_row_unchanged(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    first, _ = await _record(pg_pool, entry, decision="hold")
    async with pg_pool.acquire() as conn:
        before = await conn.fetchrow(
            "SELECT * FROM decision_records WHERE id = $1", first.id
        )
    await _record(pg_pool, entry, decision="reject", rationale="Changed my mind.....")
    async with pg_pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT * FROM decision_records WHERE id = $1", first.id
        )
    assert before is not None and after is not None
    assert dict(before) == dict(after)


@pytest.mark.asyncio
async def test_history_is_newest_first_and_latest_per_resume_is_the_newest(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    for d in ("hold", "accept", "reject"):
        await _record(pg_pool, entry, decision=d)
    async with pg_pool.acquire() as conn:
        hist = await _svc().history(conn, job_id, resume_id)
        latest = await _svc().latest_for_job(conn, job_id, None)
    assert [h.decision for h in hist] == ["reject", "accept", "hold"]
    assert [(b.resume_id, b.decision) for b in latest] == [(resume_id, "reject")]


@pytest.mark.asyncio
async def test_latest_for_job_has_one_badge_per_resume_and_ignores_other_jobs(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _job(pg_pool)
    other_job = await _job(pg_pool)
    r1, r2 = await _resume(pg_pool, job_id), await _resume(pg_pool, job_id)
    r_other = await _resume(pg_pool, other_job)
    await _raw_insert(pg_pool, job_id, r1, decision="hold")
    await _raw_insert(pg_pool, job_id, r1, decision="accept")
    await _raw_insert(pg_pool, job_id, r2, decision="reject")
    await _raw_insert(pg_pool, other_job, r_other, decision="accept")
    async with pg_pool.acquire() as conn:
        badges = await _svc().latest_for_job(conn, job_id, None)
    assert {b.resume_id: b.decision for b in badges} == {r1: "accept", r2: "reject"}
    assert len(badges) == 2
    assert all(not hasattr(b, "rationale") for b in badges)


@pytest.mark.asyncio
async def test_history_of_a_resume_with_no_decisions_is_empty(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    async with pg_pool.acquire() as conn:
        assert await _svc().history(conn, job_id, resume_id) == []
        assert await _svc().latest_for_job(conn, job_id, None) == []


@pytest.mark.asyncio
async def test_regenerating_the_shortlist_leaves_decisions_intact(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    await _record(pg_pool, entry, decision="accept")
    async with pg_pool.acquire() as conn:
        before = [dict(r) for r in await conn.fetch("SELECT * FROM decision_records")]

    new_entry = await _persist(pg_pool, job_id, resume_id, rank=5, score=0.1)
    assert new_entry.id != entry.id  # the entry row really was replaced

    async with pg_pool.acquire() as conn:
        after = [dict(r) for r in await conn.fetch("SELECT * FROM decision_records")]
        hist = await _svc().history(conn, job_id, resume_id)
        latest = await _svc().latest_for_job(conn, job_id, None)
    assert after == before
    assert [h.decision for h in hist] == ["accept"]
    assert hist[0].snap_rank == 1  # the snapshot is the rank AT DECISION TIME
    assert [(b.resume_id, b.decision) for b in latest] == [(resume_id, "accept")]


@pytest.mark.asyncio
async def test_a_decision_after_regeneration_supersedes_by_job_and_resume(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    await _record(pg_pool, entry, decision="accept")
    new_entry = await _persist(pg_pool, job_id, resume_id, rank=2, score=0.5)
    await _record(pg_pool, new_entry, decision="reject")
    async with pg_pool.acquire() as conn:
        hist = await _svc().history(conn, job_id, resume_id)
    assert [h.decision for h in hist] == ["reject", "accept"]
    assert [h.snap_rank for h in hist] == [2, 1]


@pytest.mark.asyncio
async def test_a_failed_insert_in_the_service_leaves_no_row_and_no_audit(
    pg_pool: asyncpg.Pool,
) -> None:
    """Body validation normally stops a short rationale first; bypass it so the
    CHECK constraint is what fires, and prove the whole unit rolls back."""
    job_id, resume_id = await _pair(pg_pool)
    entry = await _persist(pg_pool, job_id, resume_id, rank=1, score=0.9)
    body = _schemas().DecisionIn.model_construct(decision="accept", rationale="short")
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await _svc().record(conn, entry=entry, user=_user(), body=body)
        n_audit = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'record_decision'"
        )
    assert n_audit == 0
    assert await _count(pg_pool, job_id, resume_id) == 0
