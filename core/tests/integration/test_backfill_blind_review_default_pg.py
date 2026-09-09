"""Integration tests — ``core/scripts/backfill_blind_review_default.py``
against a REAL Postgres (testcontainers).

2026-09-09 sponsor decision: "reverse the blind review to be off by default,
keep the on switch button." Flipping the DDL/schema default (see
``tests/unit/test_ddl.py``, ``tests/integration/test_schema.py``) only ever
affects a NEW row — every job that already exists on the pilot box was
created under the OLD default and stays ``blind_review = TRUE`` forever
unless something touches it. That is exactly the ROADMAP A7 shape ("a
correct fix, gated green, that never touched a single row"), and the reason
this backfill script exists: a one-time pass over already-existing rows,
mirroring ``core/scripts/backfill_job_fields.py``'s own dry-run-by-default /
``--apply`` / idempotent shape.

**The rule under test** (from the task spec): flip ``blind_review`` to
``FALSE`` for every job that is currently ``TRUE`` **and** has **no**
``blind_review_toggled`` row in ``audit_log`` for it (``subject_type='job'``,
``subject_id=<job id>``) — a job someone already toggled by hand keeps their
own choice, even if that choice happens to currently read ``TRUE`` (toggled
away and back). Each applied flip must itself write exactly one new
``audit_log`` row: ``action='blind_review_toggled'``,
``details={"old": true, "new": false, "reason": "default reversed
2026-09-09"}``, a SERVICE actor (this file expects
``actor_kind='service'``/``actor_service='blind-review-backfill'`` — the
concrete label a legal same-shape alternative to ``job_source_service``'s own
``actor_service='taleo-sync'`` convention; adjust the assertions below to
match if the implementation picks a different string, but it MUST be a
legal ``actor_kind='service'`` row per the ``audit_log_actor_identity``
CHECK, never an unattributed write).

Nothing under ``core/scripts`` exists for this yet
(``backfill_blind_review_default.py``) — every test below is expected to
fail, most immediately at import (``FileNotFoundError``/``ImportError`` from
the module loader) — RED half of the TDD cycle. That import failure is
ACCEPTABLE for this file only; every other file in this branch's RED set
must fail on an assertion, not an import.

Import mechanics mirror ``tests/unit/test_backfill_job_fields.py`` (load by
file path via ``importlib``, since ``core/scripts`` is not an installed
package for the test runner) combined with ``tests/integration
/test_blind_review_audit_pg.py``'s testcontainers Postgres fixture pattern
and ``monkeypatch.setattr(module, "get_settings", ...)`` override (the same
override point ``test_blind_review_audit_pg.py`` uses on
``src.api.deps.get_settings``) — the loaded script keeps its OWN bound name
for ``get_settings`` (``from src.settings import get_settings``), so
monkeypatching the module's attribute redirects its real ``asyncpg.connect``
call at the DSN it reads from ``Settings.postgres_dsn``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.settings import Settings

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "backfill_blind_review_default.py"
)

_JD = "We need a research analyst for the neuroscience team. " * 3


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_backfill_blind_review_default", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, audit_log, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


def _settings(pg_dsn: str) -> Settings:
    return Settings(
        postgres_dsn=pg_dsn,
        skill_hash_salt="test-salt",
        pii_key="test-key",
    )


async def _insert_job(
    pool: asyncpg.Pool, *, title: str, blind_review: bool
) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ($1, $2, $3) RETURNING id",
            title,
            _JD,
            blind_review,
        )
    return job_id


async def _record_manual_toggle(pool: asyncpg.Pool, job_id: uuid.UUID) -> None:
    """A recruiter already flipped this job's ``blind_review`` by hand —
    stand in for the REAL route's audit write (``job_service.update_job``,
    proved in ``tests/integration/test_blind_review_audit_pg.py``) with a
    direct INSERT, since only the shape of the row (one
    ``blind_review_toggled`` row for this ``subject_id``) matters here."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO audit_log (actor_kind, actor_service, action, "
            "subject_type, subject_id, job_id, details) VALUES "
            "('service', 'dev-anonymous', 'blind_review_toggled', 'job', "
            "$1, $1, $2::jsonb)",
            job_id,
            json.dumps({"old": False, "new": True}),
        )


async def _blind_review(pool: asyncpg.Pool, job_id: uuid.UUID) -> bool:
    async with pool.acquire() as conn:
        value: bool = await conn.fetchval(
            "SELECT blind_review FROM jobs WHERE id = $1", job_id
        )
    return value


async def _backfill_audit_rows(
    pool: asyncpg.Pool, job_id: uuid.UUID
) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT actor_kind, actor_user_id, actor_service, action, "
            "subject_type, subject_id, job_id, details FROM audit_log "
            "WHERE subject_id = $1 AND action = 'blind_review_toggled' "
            "AND details->>'reason' = 'default reversed 2026-09-09'",
            job_id,
        )


def _details(row: asyncpg.Record) -> Any:
    raw = row["details"]
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.fixture
async def _three_jobs(
    pg_pool: asyncpg.Pool,
) -> dict[str, uuid.UUID]:
    """Three ``blind_review = TRUE`` jobs: two never touched by a human, one
    already toggled by hand (which must keep ITS choice, even though it
    currently reads TRUE)."""
    untouched_a = await _insert_job(pg_pool, title="Untouched A", blind_review=True)
    untouched_b = await _insert_job(pg_pool, title="Untouched B", blind_review=True)
    manually_toggled = await _insert_job(
        pg_pool, title="Manually Toggled", blind_review=True
    )
    await _record_manual_toggle(pg_pool, manually_toggled)
    return {
        "untouched_a": untouched_a,
        "untouched_b": untouched_b,
        "manually_toggled": manually_toggled,
    }


# ── dry run: proposes the right set, writes nothing ────────────────────────


@pytest.mark.asyncio
async def test_dry_run_writes_no_column_changes(
    pg_pool: asyncpg.Pool,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    _three_jobs: dict[str, uuid.UUID],
) -> None:
    module = _module()
    monkeypatch.setattr(module, "get_settings", lambda: _settings(pg_dsn))

    await module._run(apply=False)

    assert await _blind_review(pg_pool, _three_jobs["untouched_a"]) is True
    assert await _blind_review(pg_pool, _three_jobs["untouched_b"]) is True
    assert await _blind_review(pg_pool, _three_jobs["manually_toggled"]) is True


@pytest.mark.asyncio
async def test_dry_run_writes_no_audit_rows(
    pg_pool: asyncpg.Pool,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    _three_jobs: dict[str, uuid.UUID],
) -> None:
    module = _module()
    monkeypatch.setattr(module, "get_settings", lambda: _settings(pg_dsn))

    await module._run(apply=False)

    for job_id in _three_jobs.values():
        assert await _backfill_audit_rows(pg_pool, job_id) == []


# ── --apply: flips exactly the two untouched jobs ───────────────────────────


@pytest.mark.asyncio
async def test_apply_flips_only_the_untouched_jobs(
    pg_pool: asyncpg.Pool,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    _three_jobs: dict[str, uuid.UUID],
) -> None:
    module = _module()
    monkeypatch.setattr(module, "get_settings", lambda: _settings(pg_dsn))

    await module._run(apply=True)

    assert await _blind_review(pg_pool, _three_jobs["untouched_a"]) is False
    assert await _blind_review(pg_pool, _three_jobs["untouched_b"]) is False
    # A job a human already toggled by hand keeps ITS choice — untouched by
    # the backfill even though it currently reads TRUE.
    assert await _blind_review(pg_pool, _three_jobs["manually_toggled"]) is True


@pytest.mark.asyncio
async def test_apply_writes_exactly_two_audit_rows_with_the_expected_shape(
    pg_pool: asyncpg.Pool,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    _three_jobs: dict[str, uuid.UUID],
) -> None:
    module = _module()
    monkeypatch.setattr(module, "get_settings", lambda: _settings(pg_dsn))

    await module._run(apply=True)

    a_rows = await _backfill_audit_rows(pg_pool, _three_jobs["untouched_a"])
    b_rows = await _backfill_audit_rows(pg_pool, _three_jobs["untouched_b"])
    manual_rows = await _backfill_audit_rows(pg_pool, _three_jobs["manually_toggled"])

    assert len(a_rows) == 1
    assert len(b_rows) == 1
    assert manual_rows == []

    for row in (a_rows[0], b_rows[0]):
        assert row["actor_kind"] == "service"
        assert row["actor_user_id"] is None
        assert row["actor_service"] == "blind-review-backfill"
        assert row["subject_type"] == "job"
        assert _details(row) == {
            "old": True,
            "new": False,
            "reason": "default reversed 2026-09-09",
        }


@pytest.mark.asyncio
async def test_apply_return_code_is_zero(
    pg_pool: asyncpg.Pool,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    _three_jobs: dict[str, uuid.UUID],
) -> None:
    module = _module()
    monkeypatch.setattr(module, "get_settings", lambda: _settings(pg_dsn))

    result = await module._run(apply=True)

    assert result == 0


# ── idempotence: a second --apply proposes and writes nothing ──────────────


@pytest.mark.asyncio
async def test_a_second_apply_proposes_and_writes_nothing(
    pg_pool: asyncpg.Pool,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    _three_jobs: dict[str, uuid.UUID],
) -> None:
    """An operator must be able to run ``--apply`` twice safely — the second
    run finds zero eligible jobs (the two it already flipped are no longer
    ``TRUE``) and writes zero new audit rows."""
    module = _module()
    monkeypatch.setattr(module, "get_settings", lambda: _settings(pg_dsn))

    await module._run(apply=True)
    await module._run(apply=True)

    a_rows = await _backfill_audit_rows(pg_pool, _three_jobs["untouched_a"])
    b_rows = await _backfill_audit_rows(pg_pool, _three_jobs["untouched_b"])
    assert len(a_rows) == 1, "the second run must not write a duplicate row"
    assert len(b_rows) == 1, "the second run must not write a duplicate row"

    assert await _blind_review(pg_pool, _three_jobs["untouched_a"]) is False
    assert await _blind_review(pg_pool, _three_jobs["untouched_b"]) is False


# ── a job that is already FALSE is left alone regardless of audit history ──


@pytest.mark.asyncio
async def test_a_job_already_false_is_never_touched(
    pg_pool: asyncpg.Pool, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = await _insert_job(pg_pool, title="Already Opt-Out", blind_review=False)
    module = _module()
    monkeypatch.setattr(module, "get_settings", lambda: _settings(pg_dsn))

    await module._run(apply=True)

    assert await _blind_review(pg_pool, job_id) is False
    assert await _backfill_audit_rows(pg_pool, job_id) == []
