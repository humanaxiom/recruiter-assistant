"""Slice C1 (RED) -- ``resume_split_requests`` + ``split_request_service`` against a
REAL Postgres (testcontainers) and a real filesystem ``BlobStore``.

Pins what unit mocks structurally cannot: the DDL (CHECK constraints, FK
cascade, NOT NULL-without-DEFAULT vs the first real INSERT -- the CLAUDE.md
``users.role`` lesson), compare-and-set semantics under concurrency, pgcrypto
round trips, and that terminal rows really hold no blob and no proposal.

Conventions beyond the unit-test docstring of ``test_split_request_service.py``
(the API is the same):

* ``create`` inserts ``status='queued'``, ``proposal_version=0``,
  ``consent_acknowledged`` as given, and writes the blob BEFORE the row is
  visible to a sweeper only in the sense that a failed ``put`` leaves no row.
* ``store_proposal`` bumps ``proposal_version`` to 1 and records ``page_count``,
  ``method``, ``fallback_reason`` and ``diagnostics`` (jsonb object, non-PII).
* ``resume_split_requests.method`` is CHECKed to ``llm|heuristic``;
  ``status`` is CHECKed over the six statuses.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src import doctor
from src.models.ddl import init_schema
from src.schemas.taleo_split import (
    ProposalApplicant,
    ProposalDiagnostics,
    SplitProposal,
)
from src.services import split_request_service as srs
from src.storage.blob_store import BlobStore

PII_KEY = "integration-test-pii-key-do-not-use-in-prod"
PDF = b"%PDF-1.4\ncombined export bytes\n"

_ROW_SQL = "SELECT * FROM resume_split_requests WHERE id = $1"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(dsn=pg_dsn, min_size=1, max_size=5)
    await init_schema(p)
    async with p.acquire() as conn:
        await conn.execute("TRUNCATE jobs, outbox CASCADE")
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture(autouse=True)
def _pii_key() -> Iterator[None]:
    with patch(
        "src.services.pii.get_settings", return_value=SimpleNamespace(pii_key=PII_KEY)
    ):
        yield


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


async def _job(pool: asyncpg.Pool, *, blind: bool = False) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ('Eng', 'Build things.', $1) RETURNING id",
            blind,
        )
    return job_id


async def _create(
    pool: asyncpg.Pool, store: BlobStore, job_id: uuid.UUID, data: bytes = PDF
) -> uuid.UUID:
    async with pool.acquire() as conn:
        return await srs.create(
            conn,
            store,
            job_id=job_id,
            filename="export.pdf",
            data=data,
            consent_acknowledged=True,
            created_by="rec@example.com",
        )


async def _row(pool: asyncpg.Pool, split_id: uuid.UUID) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_ROW_SQL, split_id)
    assert row is not None
    return row


async def _age(pool: asyncpg.Pool, split_id: uuid.UUID, *, hours: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE resume_split_requests SET "
            "created_at = now() - make_interval(secs => $2), "
            "updated_at = now() - make_interval(secs => $2) WHERE id = $1",
            split_id,
            hours * 3600,
        )


async def _set_status(pool: asyncpg.Pool, split_id: uuid.UUID, status: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE resume_split_requests SET status = $2 WHERE id = $1",
            split_id,
            status,
        )


def _proposal(name: str = "") -> SplitProposal:
    return SplitProposal(
        applicants=[
            ProposalApplicant(
                candidate_name=name, resume_pages=[1, 2], cover_letter_pages=[3]
            ),
            ProposalApplicant(candidate_name=name, resume_pages=[4]),
        ]
    )


async def _to_proposed(
    pool: asyncpg.Pool,
    store: BlobStore,
    job_id: uuid.UUID,
    *,
    blind: bool = False,
    name: str = "",
) -> uuid.UUID:
    split_id = await _create(pool, store, job_id)
    async with pool.acquire() as conn:
        claim = await srs.claim(conn, split_id)
        assert claim is not None
        ok = await srs.store_proposal(
            conn,
            split_id,
            _proposal(name),
            diagnostics=ProposalDiagnostics(),
            method="llm",
            fallback_reason=None,
            blind_review=blind,
            page_count=4,
        )
        assert ok
    return split_id


# -- DDL ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_table_exists_with_every_planned_column(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'resume_split_requests'"
        )
    cols = {r["column_name"] for r in rows}
    assert cols >= {
        "id",
        "job_id",
        "status",
        "source_blob_key",
        "source_sha256",
        "source_filename",
        "page_count",
        "proposal",
        "proposal_version",
        "diagnostics",
        "method",
        "fallback_reason",
        "failure_reason",
        "consent_acknowledged",
        "created_by",
        "created_at",
        "updated_at",
        "accepted_by",
        "accepted_at",
        "ingest_result",
    }


@pytest.mark.asyncio
async def test_proposal_column_is_bytea_so_it_can_hold_pgp_ciphertext(
    pool: asyncpg.Pool,
) -> None:
    async with pool.acquire() as conn:
        dtype = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'resume_split_requests' AND column_name = 'proposal'"
        )
    assert dtype == "bytea"


@pytest.mark.asyncio
async def test_first_real_insert_via_create_succeeds(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    split_id = await _create(pool, store, job_id)
    row = await _row(pool, split_id)
    assert row["status"] == "queued"
    assert row["proposal"] is None
    assert row["proposal_version"] == 0
    assert row["consent_acknowledged"] is True
    assert row["created_by"] == "rec@example.com"
    assert row["created_at"] is not None and row["updated_at"] is not None
    assert row["source_filename"] == "export.pdf"
    assert len(row["source_sha256"]) == 64
    assert row["job_id"] == job_id


@pytest.mark.asyncio
async def test_create_writes_the_blob_at_the_recorded_server_key(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    row = await _row(pool, split_id)
    assert row["source_blob_key"] == f"split_sources/{split_id}.pdf"
    assert await store.get(row["source_blob_key"]) == PDF


@pytest.mark.asyncio
async def test_a_failed_blob_write_leaves_no_row(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)

    async def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    with patch.object(store, "put", _boom), pytest.raises(OSError):
        await _create(pool, store, job_id)
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM resume_split_requests")
    assert n == 0


@pytest.mark.asyncio
async def test_status_check_constraint_rejects_unknown_values(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    with pytest.raises(asyncpg.CheckViolationError):
        await _set_status(pool, split_id, "bogus")


@pytest.mark.asyncio
async def test_method_check_constraint_rejects_unknown_values(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE resume_split_requests SET method = 'magic' WHERE id = $1",
                split_id,
            )


@pytest.mark.asyncio
async def test_consent_flag_is_not_null(pool: asyncpg.Pool) -> None:
    job_id = await _job(pool)
    async with pool.acquire() as conn:
        with pytest.raises(asyncpg.NotNullViolationError):
            await conn.execute(
                "INSERT INTO resume_split_requests "
                "(job_id, source_blob_key, source_sha256, source_filename) "
                "VALUES ($1, 'split_sources/z.pdf', 'abc', 'z.pdf')",
                job_id,
            )


@pytest.mark.asyncio
async def test_deleting_the_job_cascades_to_its_split_requests(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    split_id = await _create(pool, store, job_id)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)
        n = await conn.fetchval(
            "SELECT count(*) FROM resume_split_requests WHERE id = $1", split_id
        )
    assert n == 0


# -- compare-and-set ---------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_wins_once_and_reports_the_jobs_blind_review_flag(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool, blind=True))
    async with pool.acquire() as conn:
        first = await srs.claim(conn, split_id)
        second = await srs.claim(conn, split_id)
    assert first is not None
    assert first.blind_review is True
    assert first.source_blob_key == f"split_sources/{split_id}.pdf"
    assert second is None
    assert (await _row(pool, split_id))["status"] == "segmenting"


@pytest.mark.asyncio
async def test_concurrent_claims_have_exactly_one_winner(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))

    async def _try() -> object:
        async with pool.acquire() as conn:
            return await srs.claim(conn, split_id)

    results = await asyncio.gather(*[_try() for _ in range(4)])
    assert sum(r is not None for r in results) == 1


@pytest.mark.asyncio
async def test_transition_only_applies_from_the_expected_status(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        wrong = await srs.transition(
            conn, split_id, expected="proposed", to="discarded"
        )
        right = await srs.transition(conn, split_id, expected="queued", to="segmenting")
    assert wrong is False
    assert right is True
    assert (await _row(pool, split_id))["status"] == "segmenting"


# -- proposal storage --------------------------------------------------------


@pytest.mark.asyncio
async def test_proposal_round_trips_and_is_ciphertext_at_rest(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    row = await _row(pool, split_id)
    assert row["status"] == "proposed"
    assert row["method"] == "llm"
    assert row["fallback_reason"] is None
    assert row["page_count"] == 4
    assert row["proposal_version"] == 1
    assert isinstance(row["proposal"], bytes)
    assert b"resume_pages" not in row["proposal"]
    async with pool.acquire() as conn:
        assert await srs.load_proposal(conn, split_id) == _proposal()


@pytest.mark.asyncio
async def test_diagnostics_are_a_jsonb_object_of_page_lists(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        await srs.claim(conn, split_id)
        await srs.store_proposal(
            conn,
            split_id,
            _proposal(),
            diagnostics=ProposalDiagnostics(missing=[9], merged=[2]),
            method="heuristic",
            fallback_reason="llm_error",
            blind_review=False,
            page_count=10,
        )
    row = await _row(pool, split_id)
    diag = row["diagnostics"]
    diag = json.loads(diag) if isinstance(diag, str) else diag
    assert diag["missing"] == [9]
    assert diag["merged"] == [2]
    assert row["method"] == "heuristic"
    assert row["fallback_reason"] == "llm_error"


@pytest.mark.asyncio
async def test_blind_review_job_never_persists_guessed_names(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=True)
    split_id = await _to_proposed(pool, store, job_id, blind=True, name="Pat Example")
    async with pool.acquire() as conn:
        loaded = await srs.load_proposal(conn, split_id)
        # decrypt the raw column too: the names must not be in the plaintext
        await conn.execute("SELECT set_config('app.pii_key', $1, false)", PII_KEY)
        raw = await conn.fetchval(
            "SELECT pgp_sym_decrypt(proposal, current_setting('app.pii_key')) "
            "FROM resume_split_requests WHERE id = $1",
            split_id,
        )
    assert loaded is not None
    assert [a.candidate_name for a in loaded.applicants] == ["", ""]
    assert "Pat Example" not in raw


@pytest.mark.asyncio
async def test_store_proposal_refuses_a_row_that_is_not_segmenting(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))  # still queued
    async with pool.acquire() as conn:
        ok = await srs.store_proposal(
            conn,
            split_id,
            _proposal(),
            diagnostics=ProposalDiagnostics(),
            method="llm",
            fallback_reason=None,
            blind_review=False,
            page_count=4,
        )
    assert ok is False
    row = await _row(pool, split_id)
    assert row["status"] == "queued"
    assert row["proposal"] is None


@pytest.mark.asyncio
async def test_load_proposal_is_none_for_a_row_without_one(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        assert await srs.load_proposal(conn, split_id) is None


# -- terminal states hold no blob and no proposal ----------------------------


async def _assert_released(
    pool: asyncpg.Pool, store: BlobStore, split_id: uuid.UUID, key: str
) -> None:
    row = await _row(pool, split_id)
    assert row["source_blob_key"] is None
    assert row["proposal"] is None
    assert not await store.exists(key)
    async with pool.acquire() as conn:
        offenders = await conn.fetch(doctor._SPLIT_TERMINAL_HOLDING_SQL)
    assert offenders == []


@pytest.mark.asyncio
async def test_discard_releases_blob_and_proposal(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    key = f"split_sources/{split_id}.pdf"
    async with pool.acquire() as conn:
        assert await srs.discard(conn, store, split_id) is True
    assert (await _row(pool, split_id))["status"] == "discarded"
    await _assert_released(pool, store, split_id, key)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "failed"])
async def test_discard_also_works_from_queued_and_failed(
    pool: asyncpg.Pool, store: BlobStore, status: str
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    key = f"split_sources/{split_id}.pdf"
    if status == "failed":
        async with pool.acquire() as conn:
            await srs.claim(conn, split_id)
            assert await srs.fail(conn, store, split_id, "unreadable_pdf")
    async with pool.acquire() as conn:
        assert await srs.discard(conn, store, split_id) is True
    await _assert_released(pool, store, split_id, key)


@pytest.mark.asyncio
async def test_discard_is_not_repeatable_and_never_touches_an_accepted_row(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        assert await srs.mark_accepted(
            conn, store, split_id, accepted_by="rec", ingest_result=[]
        )
        assert await srs.discard(conn, store, split_id) is False
    assert (await _row(pool, split_id))["status"] == "accepted"


@pytest.mark.asyncio
async def test_fail_records_a_static_reason_and_releases_the_blob(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    key = f"split_sources/{split_id}.pdf"
    async with pool.acquire() as conn:
        await srs.claim(conn, split_id)
        assert await srs.fail(conn, store, split_id, "encrypted_pdf") is True
    row = await _row(pool, split_id)
    assert row["status"] == "failed"
    assert row["failure_reason"] == "encrypted_pdf"
    await _assert_released(pool, store, split_id, key)


@pytest.mark.asyncio
async def test_fail_cannot_clobber_a_proposed_row(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        assert await srs.fail(conn, store, split_id, "unreadable_pdf") is False
    row = await _row(pool, split_id)
    assert row["status"] == "proposed"
    assert row["proposal"] is not None
    assert await store.exists(f"split_sources/{split_id}.pdf")


@pytest.mark.asyncio
async def test_mark_accepted_stores_the_result_and_releases_everything(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    key = f"split_sources/{split_id}.pdf"
    result = [{"original_filename": "applicant-001_resume.pdf", "outcome": "accepted"}]
    async with pool.acquire() as conn:
        ok = await srs.mark_accepted(
            conn, store, split_id, accepted_by="rec@example.com", ingest_result=result
        )
    assert ok is True
    row = await _row(pool, split_id)
    assert row["status"] == "accepted"
    assert row["accepted_by"] == "rec@example.com"
    assert row["accepted_at"] is not None
    stored = row["ingest_result"]
    stored = json.loads(stored) if isinstance(stored, str) else stored
    assert stored == result
    await _assert_released(pool, store, split_id, key)


@pytest.mark.asyncio
async def test_mark_accepted_second_call_loses_and_keeps_the_first_result(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        assert await srs.mark_accepted(
            conn, store, split_id, accepted_by="first", ingest_result=[{"n": 1}]
        )
        assert not await srs.mark_accepted(
            conn, store, split_id, accepted_by="second", ingest_result=[{"n": 2}]
        )
    assert (await _row(pool, split_id))["accepted_by"] == "first"


@pytest.mark.asyncio
async def test_mark_accepted_requires_a_proposed_row(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))  # queued
    async with pool.acquire() as conn:
        ok = await srs.mark_accepted(
            conn, store, split_id, accepted_by="rec", ingest_result=[]
        )
    assert ok is False
    assert await store.exists(f"split_sources/{split_id}.pdf")


# -- expiry, reaper, orphan sweep --------------------------------------------


@pytest.mark.asyncio
async def test_expire_discards_old_unaccepted_rows_and_frees_their_blobs(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    old_proposed = await _to_proposed(pool, store, job_id)
    old_queued = await _create(pool, store, job_id, b"%PDF-1.4 q")
    old_failed = await _create(pool, store, job_id, b"%PDF-1.4 f")
    async with pool.acquire() as conn:
        await srs.claim(conn, old_failed)
        await srs.fail(conn, store, old_failed, "unreadable_pdf")
    for sid in (old_proposed, old_queued, old_failed):
        await _age(pool, sid, hours=100)

    async with pool.acquire() as conn:
        n = await srs.expire(conn, store, ttl_hours=72)

    assert n == 3
    for sid in (old_proposed, old_queued, old_failed):
        await _assert_released(pool, store, sid, f"split_sources/{sid}.pdf")
        assert (await _row(pool, sid))["status"] == "discarded"


@pytest.mark.asyncio
async def test_expire_leaves_fresh_rows_alone(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    await _age(pool, split_id, hours=71)
    async with pool.acquire() as conn:
        assert await srs.expire(conn, store, ttl_hours=72) == 0
    row = await _row(pool, split_id)
    assert row["status"] == "proposed"
    assert row["proposal"] is not None
    assert await store.exists(f"split_sources/{split_id}.pdf")


@pytest.mark.asyncio
async def test_expire_never_touches_accepted_or_discarded_history(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    accepted = await _to_proposed(pool, store, job_id)
    async with pool.acquire() as conn:
        await srs.mark_accepted(
            conn, store, accepted, accepted_by="rec", ingest_result=[{"k": 1}]
        )
    await _age(pool, accepted, hours=1000)
    async with pool.acquire() as conn:
        assert await srs.expire(conn, store, ttl_hours=72) == 0
    row = await _row(pool, accepted)
    assert row["status"] == "accepted"
    assert row["accepted_by"] == "rec"


@pytest.mark.asyncio
async def test_expire_default_ttl_comes_from_settings(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    await _age(pool, split_id, hours=2)
    async with pool.acquire() as conn:
        with patch.object(
            srs, "get_settings", return_value=SimpleNamespace(split_proposal_ttl_h=1)
        ):
            assert await srs.expire(conn, store) == 1
    assert (await _row(pool, split_id))["status"] == "discarded"


@pytest.mark.asyncio
async def test_requeue_stale_resets_segmenting_and_returns_stale_ids_only(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    stale_queued = await _create(pool, store, job_id, b"%PDF-1.4 a")
    stale_segmenting = await _create(pool, store, job_id, b"%PDF-1.4 b")
    fresh = await _create(pool, store, job_id, b"%PDF-1.4 c")
    old_proposed = await _to_proposed(pool, store, job_id)
    async with pool.acquire() as conn:
        await srs.claim(conn, stale_segmenting)
    for sid in (stale_queued, stale_segmenting, old_proposed):
        await _age(pool, sid, hours=1)

    async with pool.acquire() as conn:
        ids = await srs.requeue_stale(conn, minutes=30)

    assert set(ids) == {stale_queued, stale_segmenting}
    assert (await _row(pool, stale_segmenting))["status"] == "queued"
    assert (await _row(pool, stale_queued))["status"] == "queued"
    assert (await _row(pool, fresh))["status"] == "queued"
    assert (await _row(pool, old_proposed))["status"] == "proposed"


@pytest.mark.asyncio
async def test_a_requeued_row_can_be_claimed_again(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        await srs.claim(conn, split_id)
    await _age(pool, split_id, hours=1)
    async with pool.acquire() as conn:
        await srs.requeue_stale(conn, minutes=30)
        assert await srs.claim(conn, split_id) is not None


@pytest.mark.asyncio
async def test_sweep_deletes_only_unreferenced_blobs_under_the_split_prefix(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    live = f"split_sources/{split_id}.pdf"
    await store.put("split_sources/orphan.pdf", b"x")
    await store.put("resumes/keepme.pdf", b"y")
    # Past the grace period, so the sweep treats it as a real orphan.
    for key in (live, "split_sources/orphan.pdf", "resumes/keepme.pdf"):
        _backdate(store, key, seconds=3600)
    async with pool.acquire() as conn:
        n = await srs.sweep_orphan_blobs(conn, store)
    assert n == 1
    assert await store.exists(live)
    assert not await store.exists("split_sources/orphan.pdf")
    assert await store.exists("resumes/keepme.pdf")


@pytest.mark.asyncio
async def test_sweep_frees_the_blob_of_a_row_that_is_already_terminal(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    # A discard that crashed between UPDATE and unlink leaves an orphan blob.
    split_id = await _create(pool, store, await _job(pool))
    key = f"split_sources/{split_id}.pdf"
    _backdate(store, key, seconds=3600)  # past the sweep's grace period
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE resume_split_requests SET status = 'discarded', "
            "source_blob_key = NULL WHERE id = $1",
            split_id,
        )
        assert await srs.sweep_orphan_blobs(conn, store) == 1
    assert not await store.exists(key)


# -- the doctor SQL against real rows ----------------------------------------


@pytest.mark.asyncio
async def test_doctor_sql_flags_a_terminal_row_that_kept_its_blob_key(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        assert await conn.fetch(doctor._SPLIT_TERMINAL_HOLDING_SQL) == []
    await _set_status(pool, split_id, "discarded")  # key NOT cleared
    async with pool.acquire() as conn:
        rows = await conn.fetch(doctor._SPLIT_TERMINAL_HOLDING_SQL)
    assert [r["id"] for r in rows] == [split_id]


@pytest.mark.asyncio
async def test_doctor_sql_flags_a_terminal_row_that_kept_its_proposal(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE resume_split_requests SET status = 'accepted', "
            "source_blob_key = NULL WHERE id = $1",
            split_id,
        )
        rows = await conn.fetch(doctor._SPLIT_TERMINAL_HOLDING_SQL)
    assert [r["id"] for r in rows] == [split_id]


@pytest.mark.asyncio
async def test_doctor_sql_ignores_live_rows_holding_data(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    await _to_proposed(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        assert await conn.fetch(doctor._SPLIT_TERMINAL_HOLDING_SQL) == []


# -- C+D review fixes (RED) ---------------------------------------------------


# item 6: source_filename is nullable, truncated on the way in, and NULLed on
# every terminal state (a filename can carry a candidate's name).


@pytest.mark.asyncio
async def test_source_filename_column_is_nullable(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        nullable = await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'resume_split_requests' "
            "AND column_name = 'source_filename'"
        )
    assert nullable == "YES"


@pytest.mark.asyncio
async def test_create_stores_a_400_char_filename_truncated_to_255(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    async with pool.acquire() as conn:
        split_id = await srs.create(
            conn,
            store,
            job_id=job_id,
            filename="y" * 400 + ".pdf",
            data=PDF,
            consent_acknowledged=True,
            created_by=None,
        )
    assert (await _row(pool, split_id))["source_filename"] == "y" * 255


async def _terminate(pool: asyncpg.Pool, store: BlobStore, how: str) -> uuid.UUID:
    job_id = await _job(pool)
    if how == "accepted":
        split_id = await _to_proposed(pool, store, job_id)
        async with pool.acquire() as conn:
            assert await srs.mark_accepted(
                conn, store, split_id, accepted_by="rec", ingest_result=[]
            )
    elif how == "discarded":
        split_id = await _to_proposed(pool, store, job_id)
        async with pool.acquire() as conn:
            assert await srs.discard(conn, store, split_id)
    elif how == "failed":
        split_id = await _create(pool, store, job_id)
        async with pool.acquire() as conn:
            await srs.claim(conn, split_id)
            assert await srs.fail(conn, store, split_id, "unreadable_pdf")
    else:  # expired
        split_id = await _to_proposed(pool, store, job_id)
        await _age(pool, split_id, hours=500)
        async with pool.acquire() as conn:
            assert await srs.expire(conn, store, ttl_hours=1) == 1
    return split_id


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["accepted", "discarded", "failed", "expired"])
async def test_every_terminal_state_nulls_the_source_filename(
    pool: asyncpg.Pool, store: BlobStore, how: str
) -> None:
    live = await _to_proposed(pool, store, await _job(pool))
    assert (await _row(pool, live))["source_filename"] == "export.pdf"
    split_id = await _terminate(pool, store, how)
    row = await _row(pool, split_id)
    assert row["status"] == ("discarded" if how == "expired" else how)
    assert row["source_filename"] is None


@pytest.mark.asyncio
async def test_a_live_row_keeps_its_source_filename(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _to_proposed(pool, store, await _job(pool))
    assert (await _row(pool, split_id))["source_filename"] == "export.pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["accepted", "discarded", "failed"])
async def test_doctor_sql_flags_a_terminal_row_that_kept_its_source_filename(
    pool: asyncpg.Pool, store: BlobStore, status: str
) -> None:
    split_id = await _create(pool, store, await _job(pool))
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE resume_split_requests SET status = $2, "
            "source_blob_key = NULL, proposal = NULL WHERE id = $1",
            split_id,
            status,
        )
        rows = await conn.fetch(doctor._SPLIT_TERMINAL_HOLDING_SQL)
    assert [r["id"] for r in rows] == [split_id]


@pytest.mark.asyncio
async def test_doctor_sql_is_clean_once_the_source_filename_is_nulled(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    split_id = await _terminate(pool, store, "discarded")
    async with pool.acquire() as conn:
        assert await conn.fetch(doctor._SPLIT_TERMINAL_HOLDING_SQL) == []
    assert (await _row(pool, split_id))["source_filename"] is None


# item 7: the stale threshold is a float number of minutes (derived from the
# LLM settings), so sub-minute precision must reach the SQL.


@pytest.mark.asyncio
async def test_requeue_stale_honours_a_fractional_minute_threshold(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    younger = await _create(pool, store, job_id, b"%PDF-1.4 y")
    older = await _create(pool, store, job_id, b"%PDF-1.4 o")
    await _age(pool, younger, hours=600 / 3600)  # 10.0 min idle
    await _age(pool, older, hours=660 / 3600)  # 11.0 min idle
    async with pool.acquire() as conn:
        ids = await srs.requeue_stale(conn, minutes=10.5)
    assert ids == [older]


# item 8: an unreferenced blob inside the grace period is a create in flight.


def _backdate(store: BlobStore, key: str, *, seconds: float) -> None:
    import os
    import time

    then = time.time() - seconds
    os.utime(store._resolve(key), (then, then))


@pytest.mark.asyncio
async def test_sweep_spares_a_fresh_orphan_and_deletes_an_hour_old_one(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    await store.put("split_sources/fresh.pdf", b"x")
    await store.put("split_sources/recent.pdf", b"x")
    await store.put("split_sources/stale.pdf", b"y")
    _backdate(store, "split_sources/recent.pdf", seconds=300)
    _backdate(store, "split_sources/stale.pdf", seconds=3600)
    async with pool.acquire() as conn:
        n = await srs.sweep_orphan_blobs(conn, store)
    assert n == 1
    assert await store.exists("split_sources/fresh.pdf")
    assert await store.exists("split_sources/recent.pdf")
    assert not await store.exists("split_sources/stale.pdf")
