"""Integration (real Postgres) -- review round 1, F2: concurrent
``decision_service.record`` calls for one (job, resume) are serialised.

Without a per-pair lock, two identical concurrent calls both read "no latest",
both INSERT and both audit (two rows, two audit rows for one human action).
Two different concurrent decisions race the "superseded" link in the audit.
"""

from __future__ import annotations

import asyncio
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
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=4, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, users, audit_log CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


def _user(name: str = "alice") -> User:
    return User(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"decision-test-user:{name}"),
        cas_username=name,
        display_name=name,
        email=None,
        role="recruiter",
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


async def _ensure_user(conn: asyncpg.Connection, user: User) -> None:
    """The audit row's actor is a real FK to ``users``."""
    await conn.execute(
        "INSERT INTO users (id, cas_username, display_name, email, role) "
        "VALUES ($1, $2, $2, NULL, $3) ON CONFLICT DO NOTHING",
        user.id,
        user.cas_username,
        user.role,
    )


async def _entry(pool: asyncpg.Pool) -> ShortlistEntry:
    breakdown = ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
    )
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ('Eng', $1, FALSE) RETURNING id",
            _JD,
        )
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
        result = ShortlistResult(
            job_id=job_id,
            entries=[
                ShortlistResultEntry(
                    resume_id=resume_id,
                    rank=1,
                    score_final=0.7,
                    score_structured=0.8,
                    score_evidence=0.7,
                    breakdown=breakdown,
                    evidence=None,
                )
            ],
            pipeline_meta=PipelineMeta(
                model_gen="test-gen",
                model_emb="test-emb",
                prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
                weights=DEFAULT_WEIGHTS,
                git_sha="abc123",
                generated_at=dt.datetime.now(dt.UTC),
                timings_ms={},
            ),
        )
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
            "rank": 1,
            "score_final": 0.7,
            "score_breakdown": breakdown,
            "evidence": {"requirements": []},
            "pipeline_meta": None,
            "generated_at": _NOW,
        }
    )


async def _record_on_own_conn(
    pool: asyncpg.Pool,
    entry: ShortlistEntry,
    user: User,
    decision: str,
    rationale: str,
    gate: asyncio.Event,
) -> tuple[Any, bool]:
    body = _schemas().DecisionIn.model_validate(
        {"decision": decision, "rationale": rationale}
    )
    async with pool.acquire() as conn:
        await gate.wait()  # both connections are held before either starts
        return await _svc().record(conn, entry=entry, user=user, body=body)


async def _race(
    pool: asyncpg.Pool,
    entry: ShortlistEntry,
    user: User,
    calls: list[tuple[str, str]],
) -> list[tuple[Any, bool]]:
    async with pool.acquire() as conn:
        await _ensure_user(conn, user)
    gate = asyncio.Event()
    tasks = [
        asyncio.create_task(_record_on_own_conn(pool, entry, user, d, r, gate))
        for d, r in calls
    ]
    await asyncio.sleep(0.2)
    gate.set()
    return list(await asyncio.gather(*tasks))


@pytest.mark.asyncio
async def test_two_identical_concurrent_records_make_one_row_and_one_audit(
    pg_pool: asyncpg.Pool,
) -> None:
    entry = await _entry(pg_pool)
    results = await _race(
        pg_pool,
        entry,
        _user(),
        [("accept", _RATIONALE), ("accept", _RATIONALE)],
    )
    assert sorted(created for _out, created in results) == [False, True]
    assert results[0][0].id == results[1][0].id
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM decision_records WHERE job_id=$1 AND resume_id=$2",
            entry.job_id,
            entry.resume_id,
        )
        audits = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action='record_decision' "
            "AND subject_id=$1",
            entry.resume_id,
        )
    assert n == 1
    assert audits == 1


@pytest.mark.asyncio
async def test_two_different_concurrent_decisions_are_serialised(
    pg_pool: asyncpg.Pool,
) -> None:
    entry = await _entry(pg_pool)
    results = await _race(
        pg_pool,
        entry,
        _user(),
        [("accept", _RATIONALE), ("reject", "A different but long rationale.")],
    )
    assert [created for _out, created in results] == [True, True]
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, seq FROM decision_records WHERE job_id=$1 AND resume_id=$2 "
            "ORDER BY seq",
            entry.job_id,
            entry.resume_id,
        )
        audits = await conn.fetch(
            "SELECT details->>'decision_id' AS did, "
            "details->>'superseded_decision_id' AS sup "
            "FROM audit_log WHERE action='record_decision' AND subject_id=$1",
            entry.resume_id,
        )
    assert len(rows) == 2
    assert rows[0]["seq"] != rows[1]["seq"]
    first, second = str(rows[0]["id"]), str(rows[1]["id"])
    by_decision = {a["did"]: a["sup"] for a in audits}
    assert by_decision == {first: None, second: first}
