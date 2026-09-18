"""Integration tests — ``candidate_roster_service.reconcile_candidate_roster``
against a REAL Postgres with REAL pgcrypto.

**This file exists because its absence shipped a 503.**

`test_candidate_roster_reconciliation.py` (unit) covers the matching order,
the conflict refusal and the ambiguity policy thoroughly — and it mocks
``pii_service`` wholesale. That mock makes the name-decrypt a no-op, so the
unit suite is *structurally* blind to how the decrypt behaves against a real
database. On the first upload of a real 315-row roster to the running product,
the route returned **503**:

    asyncpg.exceptions.ExternalRoutineInvocationError:
        Illegal argument to function

``app.pii_key`` is installed with ``set_config(..., is_local => true)``, so it
is **transaction-scoped**. The reconciler decrypted names on a bare
connection, outside any transaction, so ``pgp_sym_decrypt`` received an empty
key. Every other keyed read in this repo already sets the key first (see
``resume_service._encrypt_pii``); this path did not.

The general rule, which is `CLAUDE.md`'s and was ignored here: if correctness
depends on how a real database or driver behaves, the unit suite cannot prove
it — it can only string-match the source. A mocked ``pii_service`` will agree
with any transaction state you like.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from unittest.mock import patch

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.services import candidate_roster_service
from src.services import pii as pii_service
from src.services.bulk_ingest_service import CandidateRosterRow

_ACTOR = "roster-reconciliation-test"
_TEST_PII_KEY = "roster-reconciliation-test-pii-key"


@pytest.fixture(autouse=True)
def patched_pii_key() -> Iterator[None]:
    """Point ``src.services.pii.get_settings`` at a fixed key, mirroring
    ``test_pii_encryption.py``'s fixture of the same name.

    ``PII_KEY`` is not supplied to the test environment, and
    ``current_setting('app.pii_key')`` is a STRICT read with no
    ``missing_ok`` — so without this every encrypt and decrypt below raises
    the very ``ExternalRoutineInvocationError`` this file exists to guard
    against, from the fixture rather than from the code under test.
    ``test_pii_encryption.test_encrypt_without_set_pii_key_raises_postgres_error``
    pins that behaviour deliberately; here it would only be noise.

    Autouse, and patched on ``src.services.pii`` itself, so it covers both
    this module's fixture encryption AND the ``set_pii_key`` call inside
    ``reconcile_candidate_roster``.
    """
    with patch(
        "src.services.pii.get_settings",
        return_value=SimpleNamespace(pii_key=_TEST_PII_KEY),
    ):
        yield


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
    try:
        yield pool
    finally:
        await pool.close()


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Business Analyst",
            "raw jd text",
        )
    return job_id


async def _insert_resume(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    *,
    name: str | None,
    email: str | None,
) -> uuid.UUID:
    """Insert a résumé with REALLY ENCRYPTED PII, the way the upload path does.

    The encryption goes through ``pii_service`` inside a transaction with the
    key set — so the ciphertext this test reconciles against is exactly what
    production stores, not a stand-in.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await pii_service.set_pii_key(conn)
            enc_name = await pii_service.encrypt(conn, name)
            enc_email = await pii_service.encrypt(conn, email)
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status,
                candidate_name, candidate_email, candidate_email_hash
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE,
                      'parsed', $4, $5, $6)
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
            enc_name,
            enc_email,
            pii_service.email_hash(email),
        )
    return resume_id


def _row(
    line_no: int,
    *,
    name: str | None = None,
    email: str | None = None,
    work_authorization: str = "unknown",
    internal_apsa: bool | None = None,
    internal_cupe: bool | None = None,
) -> CandidateRosterRow:
    return CandidateRosterRow(
        line_no=line_no,
        name=name,
        email=email,
        sfu_id=None,
        work_authorization=work_authorization,  # type: ignore[arg-type]
        work_authorization_source=None,
        internal_apsa=internal_apsa,
        internal_cupe=internal_cupe,
        submission_date=None,
    )


@pytest.mark.asyncio
async def test_reconciling_a_roster_decrypts_names_against_real_pgcrypto(
    pg_pool: asyncpg.Pool,
) -> None:
    """The regression test for the 503.

    A résumé whose email is absent forces the NAME fallback, which is the only
    path that decrypts. Against a bare connection this raised
    ``ExternalRoutineInvocationError``; it must now match and write.
    """
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id, name="Bree Nolan", email=None)

    async with pg_pool.acquire() as conn:
        report = await candidate_roster_service.reconcile_candidate_roster(
            conn,
            job_id,
            [_row(2, name="Nolan, Bree", work_authorization="eligible")],
            actor_kind="service",
            actor_user_id=None,
            actor_service=_ACTOR,
        )

    assert report.matched == 1, (
        "the surname-first CSV cell must match the free-form résumé name "
        "through a real decrypt"
    )
    async with pg_pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT work_authorization FROM resumes WHERE id = $1", resume_id
        )
    assert stored == "eligible"


@pytest.mark.asyncio
async def test_email_hash_match_needs_no_decrypt_and_writes_both_columns(
    pg_pool: asyncpg.Pool,
) -> None:
    """The common path: email resolves ~90% of real rows, and the internal
    flags land on the row alongside the declaration."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, name="Hemi Ngata", email="hemi.ngata@example.invalid"
    )

    async with pg_pool.acquire() as conn:
        report = await candidate_roster_service.reconcile_candidate_roster(
            conn,
            job_id,
            [
                _row(
                    2,
                    name="Ngata, Hemi",
                    email="hemi.ngata@example.invalid",
                    work_authorization="not_eligible",
                    internal_apsa=True,
                    internal_cupe=True,
                )
            ],
            actor_kind="service",
            actor_user_id=None,
            actor_service=_ACTOR,
        )

    assert report.matched == 1
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT work_authorization, internal_apsa, internal_cupe "
            "FROM resumes WHERE id = $1",
            resume_id,
        )
    assert row is not None
    assert row["work_authorization"] == "not_eligible"
    assert row["internal_apsa"] is True
    assert row["internal_cupe"] is True


@pytest.mark.asyncio
async def test_a_roster_with_no_matching_resumes_surfaces_both_directions(
    pg_pool: asyncpg.Pool,
) -> None:
    """ADR-017's "nothing is silently dropped", against real rows: a roster
    for a different requisition must report every CSV row unmatched AND every
    résumé unmatched, rather than quietly writing nothing."""
    job_id = await _insert_job(pg_pool)
    await _insert_resume(
        pg_pool, job_id, name="Someone Else", email="someone.else@example.invalid"
    )

    async with pg_pool.acquire() as conn:
        report = await candidate_roster_service.reconcile_candidate_roster(
            conn,
            job_id,
            [_row(2, name="Nobody, Here", email="nobody.here@example.invalid")],
            actor_kind="service",
            actor_user_id=None,
            actor_service=_ACTOR,
        )

    assert report.matched == 0
    assert report.unmatched_csv_rows == [2]
    assert len(report.unmatched_resumes) == 1


@pytest.mark.asyncio
async def test_the_audit_event_carries_no_decrypted_pii(
    pg_pool: asyncpg.Pool,
) -> None:
    """The reconciler decrypts names in bulk, so its own audit row is exactly
    where a name would leak. Assert against the real stored blob."""
    job_id = await _insert_job(pg_pool)
    await _insert_resume(
        pg_pool, job_id, name="Bree Nolan", email="bree.nolan@example.invalid"
    )

    async with pg_pool.acquire() as conn:
        await candidate_roster_service.reconcile_candidate_roster(
            conn,
            job_id,
            [
                _row(
                    2,
                    name="Nolan, Bree",
                    email="bree.nolan@example.invalid",
                    work_authorization="eligible",
                )
            ],
            actor_kind="service",
            actor_user_id=None,
            actor_service=_ACTOR,
        )
        rows = await conn.fetch(
            "SELECT details::text AS d FROM audit_log "
            "WHERE action = 'reconcile_candidate_roster'"
        )

    assert rows, "the reconciliation must write exactly one audit event"
    blob = " ".join(r["d"] or "" for r in rows).lower()
    assert "nolan" not in blob
    assert "bree" not in blob
    assert "example.invalid" not in blob


@pytest.mark.asyncio
async def test_two_real_resumes_sharing_an_email_hash_are_refused_not_collapsed(
    pg_pool: asyncpg.Pool,
) -> None:
    """Major 1 (2026-09-09 review finding), against a REAL schema: the
    collision the unit suite can only simulate with a mock ``conn.fetch`` is
    possible here for real, because ``resumes.candidate_email_hash`` carries
    only a plain partial index (``resumes_email_hash_idx``), not a unique
    constraint -- the table's only uniqueness is ``UNIQUE (job_id, sha256)``,
    on file content. Two real rows are inserted with the SAME email, so the
    SAME candidate_email_hash, in the same job; Postgres accepts both without
    complaint. The reconciler must refuse to guess between them rather than
    writing whichever one its ``by_email_hash`` dict comprehension happened
    to keep last."""
    job_id = await _insert_job(pg_pool)
    shared_email = "collision@example.invalid"
    resume_a = await _insert_resume(
        pg_pool, job_id, name="First Applicant", email=shared_email
    )
    resume_b = await _insert_resume(
        pg_pool, job_id, name="Second Applicant", email=shared_email
    )

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT candidate_email_hash FROM resumes WHERE id = $1", resume_a
        )
        row_b = await conn.fetchrow(
            "SELECT candidate_email_hash FROM resumes WHERE id = $1", resume_b
        )
    assert row is not None and row_b is not None
    assert row["candidate_email_hash"] == row_b["candidate_email_hash"], (
        "the fixture must actually produce a real collision at the schema "
        "level -- if this fails, the DDL grew a uniqueness constraint on "
        "candidate_email_hash and this test (and the review finding it "
        "pins) is moot"
    )

    async with pg_pool.acquire() as conn:
        report = await candidate_roster_service.reconcile_candidate_roster(
            conn,
            job_id,
            [
                _row(
                    2,
                    name="Someone, Else",
                    email=shared_email,
                    work_authorization="eligible",
                )
            ],
            actor_kind="service",
            actor_user_id=None,
            actor_service=_ACTOR,
        )

    assert report.matched == 0, (
        "a colliding email hash must resolve to NEITHER résumé, not "
        "whichever one the unordered SELECT happened to return last"
    )
    async with pg_pool.acquire() as conn:
        stored_a = await conn.fetchval(
            "SELECT work_authorization FROM resumes WHERE id = $1", resume_a
        )
        stored_b = await conn.fetchval(
            "SELECT work_authorization FROM resumes WHERE id = $1", resume_b
        )
    assert stored_a == "unknown", "resume A must not have been written"
    assert stored_b == "unknown", "resume B must not have been written"


@pytest.mark.asyncio
async def test_credential_suffix_matches_through_real_pii_decrypt(
    pg_pool: asyncpg.Pool,
) -> None:
    """Item 4 (2026-09-17), against real pgcrypto rather than a mocked
    ``pii_service``: the stored name carries a trailing credential suffix
    (", CSM"), the CSV row is "Last, First" with no email, and the two must
    still resolve to the same résumé through a real decrypt."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(
        pg_pool, job_id, name="Pat Example, CSM", email=None
    )

    async with pg_pool.acquire() as conn:
        report = await candidate_roster_service.reconcile_candidate_roster(
            conn,
            job_id,
            [_row(2, name="Example, Pat", work_authorization="eligible")],
            actor_kind="service",
            actor_user_id=None,
            actor_service=_ACTOR,
        )

    assert report.matched == 1, (
        "a credential suffix on the stored résumé name must not defeat the "
        "name match against a real decrypt"
    )
    async with pg_pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT work_authorization FROM resumes WHERE id = $1", resume_id
        )
    assert stored == "eligible"
