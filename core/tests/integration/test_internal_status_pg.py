"""Integration tests — ``resume_service.set_internal_status`` against a REAL
Postgres (Sponsor Requirements PR2 slice 2).

Mirrors ``tests/integration/test_work_authorization_pg.py`` exactly — same
shape of proof, same reasons the mocked-connection unit tests
(``test_internal_status_write_path.py``) structurally cannot cover:

* **The column defaults actually back-fill.** ``internal_apsa``/
  ``internal_cupe`` are ``BOOLEAN NOT NULL DEFAULT FALSE`` on a table with
  live rows. A row inserted WITHOUT naming either column must read back
  ``FALSE`` — never NULL.
* **Idempotency and atomicity, for real.** A re-declaration of the same pair
  writes ZERO new ``audit_log`` rows (a real row count), and an audit failure
  must ROLL BACK the column change.
* **No outbox row**, for real — not a mocked call count.

``resume_service.set_internal_status`` does not exist yet — RED half of the
TDD cycle.
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

_ACTOR_ID = uuid.UUID("a11ce000-0000-4000-8000-000000000003")


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
            "VALUES ($1, 'internal-status-test-actor', 'recruiter') "
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
    """Insert WITHOUT naming ``internal_apsa``/``internal_cupe`` — exactly how
    every résumé already in the database was written, and the only way the
    DEFAULT gets exercised."""
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
    pool: asyncpg.Pool,
    resume_id: uuid.UUID,
    *,
    internal_apsa: bool,
    internal_cupe: bool,
) -> bool:
    async with pool.acquire() as conn:
        return await resume_service.set_internal_status(
            conn,
            resume_id,
            internal_apsa=internal_apsa,
            internal_cupe=internal_cupe,
            actor_kind="user",
            actor_user_id=_ACTOR_ID,
            actor_service=None,
        )


async def _audit_count(pool: asyncpg.Pool, resume_id: uuid.UUID) -> int:
    async with pool.acquire() as conn:
        n: int = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE subject_id = $1 "
            "AND action = 'set_internal_status'",
            resume_id,
        )
    return n


async def _columns(pool: asyncpg.Pool, resume_id: uuid.UUID) -> tuple[bool, bool]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT internal_apsa, internal_cupe FROM resumes WHERE id = $1",
            resume_id,
        )
    assert row is not None
    return bool(row["internal_apsa"]), bool(row["internal_cupe"])


# ------------------------------------------------------- schema-level truths


async def test_an_insert_that_omits_both_columns_reads_back_false(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    assert await _columns(pg_pool, resume_id) == (False, False)


# ------------------------------------------------------------ the write path


async def test_declaring_persists_and_audits(pg_pool: asyncpg.Pool) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    before = await _audit_count(pg_pool, resume_id)

    assert (
        await _declare(pg_pool, resume_id, internal_apsa=True, internal_cupe=False)
        is True
    )
    assert await _columns(pg_pool, resume_id) == (True, False)
    assert await _audit_count(pg_pool, resume_id) == before + 1


async def test_redeclaring_the_same_pair_writes_no_second_audit_row(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    assert (
        await _declare(pg_pool, resume_id, internal_apsa=True, internal_cupe=True)
        is True
    )
    assert await _audit_count(pg_pool, resume_id) == 1

    assert (
        await _declare(pg_pool, resume_id, internal_apsa=True, internal_cupe=True)
        is False
    )
    assert await _audit_count(pg_pool, resume_id) == 1
    assert await _columns(pg_pool, resume_id) == (True, True)


async def test_reverting_to_false_after_a_true_declaration_is_audited(
    pg_pool: asyncpg.Pool,
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)

    await _declare(pg_pool, resume_id, internal_apsa=True, internal_cupe=True)
    before = await _audit_count(pg_pool, resume_id)

    assert (
        await _declare(pg_pool, resume_id, internal_apsa=False, internal_cupe=False)
        is True
    )
    assert await _columns(pg_pool, resume_id) == (False, False)
    assert await _audit_count(pg_pool, resume_id) == before + 1


async def test_an_audit_failure_rolls_back_the_declaration(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mutant that moves the audit write outside the wrapping transaction
    commits the declaration anyway and fails here."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    monkeypatch.setattr(
        audit_service, "record_audit", AsyncMock(side_effect=RuntimeError("boom"))
    )
    with pytest.raises(RuntimeError):
        await _declare(pg_pool, resume_id, internal_apsa=True, internal_cupe=True)
    assert await _columns(pg_pool, resume_id) == (False, False)


async def test_declaring_on_a_missing_resume_raises_not_found(
    pg_pool: asyncpg.Pool,
) -> None:
    with pytest.raises(NotFoundError):
        await _declare(pg_pool, uuid.uuid4(), internal_apsa=True, internal_cupe=False)


async def test_the_declaration_writes_no_outbox_row(pg_pool: asyncpg.Pool) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _declare(pg_pool, resume_id, internal_apsa=True, internal_cupe=True)
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM outbox WHERE aggregate_id = $1", resume_id
        )
    assert n == 0
