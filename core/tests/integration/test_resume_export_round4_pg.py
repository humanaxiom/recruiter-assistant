"""RED -- round 4: a build-time cap truncation is ANNOUNCED, against real Postgres.

``POST /jobs/{id}/resumes/exports`` refuses a job over
``resume_export_max_applicants`` (422), but a job can grow between the click and
the build. The worker then builds the first ``cap`` applicants -- and the
manifest of the ZIP must say how many were left out, in one line holding the
count and the phrase ``applicants not exported (over the cap)``. The export is
``ready`` (a truncated, labelled ZIP), not ``failed``. Withdrawn applicants never
count, and an export at or under the cap carries no such line.

Reuses the round-3 seeding helpers and fixtures.
"""

from __future__ import annotations

import re

import asyncpg
import pytest

from src.storage.blob_store import BlobStore
from tests.integration.test_resume_export_round3_pg import (  # noqa: F401
    _build_app,
    _counts,
    _download,
    _env,
    _fresh_settings,
    _insert_export,
    _job,
    _manifest,
    _pii_key,
    _resume,
    _row,
    _run_worker,
    _user,
    _zip,
    pg_dsn,
    pool,
    store,
)

PHRASE = "applicants not exported (over the cap)"


async def _built(
    pool: asyncpg.Pool,  # noqa: F811
    store: BlobStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    *,
    cap: int,
    live: int,
    withdrawn: int = 0,
) -> list[str]:
    _env(monkeypatch, resume_export_max_applicants=cap)
    job_id = await _job(pool)
    for idx in range(live):
        await _resume(pool, store, job_id, idx, minute=idx)
    for extra in range(withdrawn):
        await _resume(pool, store, job_id, live + extra, withdrawn=True, minute=9)
    user = await _user(pool)
    # Inserted directly: the POST would have refused a job that big with a 422.
    export_id = await _insert_export(pool, job_id, user, status="queued")

    assert await _run_worker(pool, store, export_id) == "ready"

    row = await _row(pool, export_id)
    assert _counts(row)["applicants"] == min(cap, live)
    zf = _zip(await _download(_build_app(pool, store, user), export_id))
    return _manifest(zf)


@pytest.mark.parametrize(("cap", "live", "left_out"), [(2, 3, 1), (1, 4, 3)])
@pytest.mark.asyncio
async def test_a_truncated_build_says_how_many_were_left_out(
    pool: asyncpg.Pool,  # noqa: F811
    store: BlobStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
    live: int,
    left_out: int,
) -> None:
    lines = await _built(pool, store, monkeypatch, cap=cap, live=live)

    notes = [ln for ln in lines if PHRASE in ln]
    assert len(notes) == 1, lines
    assert re.search(rf"\b{left_out}\b", notes[0]), notes[0]
    assert len([ln for ln in lines if ln.startswith("applicant-")]) == cap


@pytest.mark.asyncio
async def test_withdrawn_applicants_are_not_counted_as_left_out(
    pool: asyncpg.Pool,  # noqa: F811
    store: BlobStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = await _built(pool, store, monkeypatch, cap=2, live=3, withdrawn=1)

    notes = [ln for ln in lines if PHRASE in ln]
    assert len(notes) == 1, lines
    assert re.search(r"\b1\b", notes[0]), notes[0]
    assert not re.search(r"\b2\b", notes[0]), notes[0]


@pytest.mark.parametrize(("cap", "live"), [(2, 2), (3, 2)])
@pytest.mark.asyncio
async def test_an_export_within_the_cap_has_no_truncation_line(
    pool: asyncpg.Pool,  # noqa: F811
    store: BlobStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
    live: int,
) -> None:
    lines = await _built(pool, store, monkeypatch, cap=cap, live=live, withdrawn=1)

    assert not [ln for ln in lines if "over the cap" in ln or "not exported" in ln]
