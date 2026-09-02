"""Integration tests — Canadian work authorization against a REAL Postgres.

SPONSOR 2026-09-02 §O2 / answer 4. Four things a real database proves that the
mocked-connection unit tests (``test_work_authorization_banding.py``)
structurally cannot, and each of the four is a way this feature could ship
green and still be wrong:

* **The column default actually back-fills.** ``work_authorization`` is
  ``NOT NULL DEFAULT 'unknown'`` on a table with ~200 live rows. A row inserted
  WITHOUT naming the column must read back ``'unknown'`` — never NULL, never
  ``'not_eligible'``. This is precisely the FU-5 slice-1 lesson recorded in
  ``CLAUDE.md``: a ``NOT NULL`` column with no ``DEFAULT`` passed 2,764 unit
  tests and would have failed the first real INSERT.
* **The CHECK constraint is real.** A fourth state must be rejected BY THE
  DATABASE, not merely absent from a ``Literal``. This repo's characteristic
  defect is an invariant stated in prose with nothing enforcing it, and a
  screening decision is not the place to repeat it.
* **The band actually orders rows.** A real ``ORDER BY`` over real rows, with
  an ineligible HIGH-scoring candidate that must still sort below an eligible
  LOW-scoring one. A string assertion on the SQL cannot prove the clause does
  what it reads like it does.
* **Idempotency and atomicity, for real.** A re-declaration of the same state
  writes ZERO new ``audit_log`` rows (a real row count, not a mocked call
  count), and an audit failure must ROLL BACK the column change — a mutant
  that moves the audit write outside the transaction commits the declaration
  anyway and fails here.

``resume_service.set_work_authorization`` and the banded read do not exist yet
— RED half of the TDD cycle.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.errors import NotFoundError
from src.models.ddl import init_schema
from src.services import audit_service, resume_service

_ACTOR_ID = uuid.UUID("a11ce000-0000-4000-8000-000000000002")


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
        await conn.execute(
            "INSERT INTO users (id, cas_username, role) "
            "VALUES ($1, 'work-auth-test-actor', 'recruiter') "
            "ON CONFLICT (id) DO NOTHING",
            _ACTOR_ID,
        )
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "raw jd text",
        )
    return job_id


async def _insert_resume(pool: asyncpg.Pool, job_id: uuid.UUID) -> uuid.UUID:
    """Insert WITHOUT naming ``work_authorization`` — exactly how every row on
    the pilot box was written, and the only way the DEFAULT gets exercised."""
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE,
                      'parsed')
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
        )
    return resume_id


async def _declare(
    pool: asyncpg.Pool, resume_id: uuid.UUID, status: str, note: str | None = None
) -> bool:
    async with pool.acquire() as conn:
        return await resume_service.set_work_authorization(
            conn,
            resume_id,
            status=status,  # type: ignore[arg-type]
            note=note,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )


async def _audit_count(pool: asyncpg.Pool, resume_id: uuid.UUID) -> int:
    async with pool.acquire() as conn:
        n: int = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE subject_id = $1 "
            "AND action = 'set_work_authorization'",
            resume_id,
        )
    return n


async def _column(pool: asyncpg.Pool, resume_id: uuid.UUID) -> str:
    async with pool.acquire() as conn:
        value: str = await conn.fetchval(
            "SELECT work_authorization FROM resumes WHERE id = $1", resume_id
        )
    return value


# ------------------------------------------------------- schema-level truths


async def test_an_insert_that_omits_the_column_reads_back_unknown(
    pg_pool: asyncpg.Pool,
) -> None:
    """The back-fill. Every résumé already on the pilot box was written before
    this column existed; the DEFAULT is what makes them all read ``unknown``
    the instant the ALTER lands, rather than NULL — which any consumer that
    coerced falsy to "not eligible" would turn into a mass adverse finding."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    assert await _column(pg_pool, resume_id) == "unknown"


async def test_the_database_rejects_a_fourth_state(pg_pool: asyncpg.Pool) -> None:
    """The CHECK constraint, not the Literal. A typo'd state must be
    impossible to store, not merely impossible to type."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "UPDATE resumes SET work_authorization = 'probably' WHERE id = $1",
                resume_id,
            )


async def test_the_column_cannot_be_set_null(pg_pool: asyncpg.Pool) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            await conn.execute(
                "UPDATE resumes SET work_authorization = NULL WHERE id = $1",
                resume_id,
            )


# ------------------------------------------------------------ the write path


@pytest.mark.parametrize("status", ["eligible", "not_eligible", "unknown"])
async def test_declaring_persists_and_audits(
    pg_pool: asyncpg.Pool, status: str
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    # Seed a different state first so even "unknown" is a real change.
    await _declare(
        pg_pool, resume_id, "eligible" if status != "eligible" else "unknown"
    )
    before = await _audit_count(pg_pool, resume_id)

    assert await _declare(pg_pool, resume_id, status) is True
    assert await _column(pg_pool, resume_id) == status
    assert await _audit_count(pg_pool, resume_id) == before + 1


async def test_redeclaring_the_same_state_writes_no_second_audit_row(
    pg_pool: asyncpg.Pool,
) -> None:
    """A REAL row count. The trail records decisions, not clicks — a recruiter
    reloading the form must not manufacture audit rows for a decision nobody
    re-made."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    assert await _declare(pg_pool, resume_id, "not_eligible") is True
    assert await _audit_count(pg_pool, resume_id) == 1

    assert await _declare(pg_pool, resume_id, "not_eligible") is False
    assert await _audit_count(pg_pool, resume_id) == 1
    assert await _column(pg_pool, resume_id) == "not_eligible"


async def test_an_audit_failure_rolls_back_the_declaration(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomicity, for real. A mutant that moves the audit write outside the
    wrapping transaction commits the declaration anyway and fails here — and
    an unaudited adverse decision is exactly the thing this feature must never
    produce."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    monkeypatch.setattr(
        audit_service, "record_audit", AsyncMock(side_effect=RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        await _declare(pg_pool, resume_id, "not_eligible")
    assert await _column(pg_pool, resume_id) == "unknown"


async def test_declaring_on_a_missing_resume_raises_not_found(
    pg_pool: asyncpg.Pool,
) -> None:
    with pytest.raises(NotFoundError):
        await _declare(pg_pool, uuid.uuid4(), "eligible")


async def test_the_declaration_writes_no_outbox_row(pg_pool: asyncpg.Pool) -> None:
    """Eligibility is a screening attribute, not a ranking input, and the band
    is applied at read time — so nothing needs re-projecting. An outbox row
    here would trigger pointless graph work AND imply to a future reader that
    eligibility feeds the graph."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _declare(pg_pool, resume_id, "not_eligible")
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM outbox WHERE aggregate_id = $1", resume_id
        )
    assert n == 0


# -------------------------------------------------------------- the band, live


async def test_an_ineligible_high_scorer_sorts_below_an_eligible_low_scorer(
    pg_pool: asyncpg.Pool,
) -> None:
    """The band, executed by Postgres rather than asserted as a substring.

    The setup is deliberately adversarial to the merit sort: the ineligible
    candidate is rank 1 with the HIGHEST score. If the band were a secondary
    key, a Python post-sort, or applied after a cap, this candidate would still
    come back first.
    """
    job_id = await _insert_job(pg_pool)
    top = await _insert_resume(pg_pool, job_id)
    mid = await _insert_resume(pg_pool, job_id)
    low = await _insert_resume(pg_pool, job_id)

    async with pg_pool.acquire() as conn:
        for rank, (rid, score) in enumerate(
            [(top, 0.95), (mid, 0.80), (low, 0.40)], start=1
        ):
            await conn.execute(
                """
                INSERT INTO shortlist_entries (
                    job_id, resume_id, rank, score_final, score_breakdown,
                    evidence, pipeline_meta
                ) VALUES ($1, $2, $3, $4, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)
                """,
                job_id,
                rid,
                rank,
                score,
            )
        # The best candidate on merit is the one who cannot work here.
        await conn.execute(
            "UPDATE resumes SET work_authorization = 'not_eligible' WHERE id = $1",
            top,
        )
        rows = await conn.fetch(resume_service_list_query(), job_id)

    order = [r["resume_id"] for r in rows]
    assert order == [mid, low, top], (
        "the ineligible top scorer must sort LAST despite holding rank 1 and "
        f"the highest score_final; got {order}"
    )
    assert [r["work_authorization"] for r in rows] == [
        "unknown",
        "unknown",
        "not_eligible",
    ]


def resume_service_list_query() -> str:
    """The REAL shortlist read query, imported rather than retyped.

    Retyping it here would let the two drift and leave this test passing
    against a band the product does not actually apply — the same "the test
    plays both parts" failure ROADMAP records.
    """
    from src.services import shortlist_service

    return shortlist_service._LIST_QUERY
