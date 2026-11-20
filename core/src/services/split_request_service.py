"""Persistence and state machine behind the in-app Taleo combined-export split.

One ``resume_split_requests`` row per uploaded export. Every transition is a
compare-and-set ``UPDATE ... WHERE status = <expected>`` so two workers, a
double-clicked button or a redelivered job cannot both win. Terminal states
(accepted / discarded / failed) release the combined-PDF blob and NULL the
encrypted proposal in the same step; ``expire`` and ``sweep_orphan_blobs``
make that true for rows and blobs that fell through a crash.

Privacy: the proposal (page assignments, manager-corrected names) is stored
``pgp_sym_encrypt``-ed via :mod:`src.services.pii`; on a blind-review job guessed
names are never persisted at all. ``fallback_reason`` / ``failure_reason`` are
fixed enum strings, never free text, so a model or parser message (which can
quote a page) can never reach a row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from src.schemas.taleo_split import ProposalDiagnostics, SplitProposal
from src.services import DbConn, pii
from src.settings import get_settings
from src.storage.blob_store import BlobStore

SOURCE_PREFIX = "split_sources/"
_FILENAME_MAX = 255
# An unreferenced blob younger than this is a create in flight (the blob is
# written BEFORE its row), not an orphan.
ORPHAN_GRACE_S = 600.0
STATUSES = ("queued", "segmenting", "proposed", "accepted", "discarded", "failed")
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"segmenting", "failed", "discarded"}),
    "segmenting": frozenset({"proposed", "failed", "queued"}),
    "proposed": frozenset({"accepted", "discarded"}),
    "failed": frozenset({"discarded"}),
    "accepted": frozenset(),
    "discarded": frozenset(),
}
METHODS = ("llm", "heuristic")
FALLBACK_REASONS = frozenset(
    {"llm_unavailable", "llm_invalid_output", "llm_error", "requeued_after_stall"}
)
FAILURE_REASONS = frozenset(
    {
        "unreadable_pdf",
        "encrypted_pdf",
        "no_text_layer",
        "too_many_pages",
        "source_missing",
    }
)


@dataclass(frozen=True)
class SplitClaim:
    """What the worker needs after winning queued -> segmenting."""

    id: UUID
    job_id: UUID
    source_blob_key: str
    blind_review: bool


_INSERT_SQL = """
INSERT INTO resume_split_requests
    (id, job_id, status, source_blob_key, source_sha256, source_filename,
     consent_acknowledged, created_by)
VALUES ($1, $2, 'queued', $3, $4, $5, $6, $7)
"""

_TRANSITION_SQL = """
UPDATE resume_split_requests
SET status = $3, updated_at = now()
WHERE id = $1 AND status = $2
RETURNING id
"""

_CLAIM_SQL = """
UPDATE resume_split_requests r
SET status = 'segmenting', updated_at = now()
FROM jobs j
WHERE r.id = $1 AND r.status = 'queued' AND j.id = r.job_id
RETURNING r.id, r.job_id, r.source_blob_key, j.blind_review
"""

_STORE_PROPOSAL_SQL = """
UPDATE resume_split_requests
SET status = 'proposed', proposal = $2, proposal_version = proposal_version + 1,
    diagnostics = $3::jsonb, method = $4, fallback_reason = $5, page_count = $6,
    updated_at = now()
WHERE id = $1 AND status = 'segmenting'
RETURNING id
"""

_LOAD_PROPOSAL_SQL = "SELECT proposal FROM resume_split_requests WHERE id = $1"

# Terminal move that releases the blob key + proposal and hands back the OLD
# blob key (RETURNING a plain column would only see the NULLed new value).
_RELEASE_SQL = """
WITH old AS (
    SELECT id, source_blob_key FROM resume_split_requests
    WHERE id = $1 AND status = ANY($2::text[]) FOR UPDATE
)
UPDATE resume_split_requests r
SET status = $3, source_blob_key = NULL, proposal = NULL,
    source_filename = NULL, updated_at = now()
FROM old
WHERE r.id = old.id
RETURNING old.source_blob_key AS source_blob_key
"""

_FAIL_SQL = """
WITH old AS (
    SELECT id, source_blob_key FROM resume_split_requests
    WHERE id = $1 AND status IN ('queued', 'segmenting') FOR UPDATE
)
UPDATE resume_split_requests r
SET status = 'failed', failure_reason = $2, source_blob_key = NULL,
    source_filename = NULL,
    proposal = NULL, updated_at = now()
FROM old
WHERE r.id = old.id
RETURNING old.source_blob_key AS source_blob_key
"""

_ACCEPT_SQL = """
WITH old AS (
    SELECT id, source_blob_key FROM resume_split_requests
    WHERE id = $1 AND status = 'proposed' FOR UPDATE
)
UPDATE resume_split_requests r
SET status = 'accepted', source_blob_key = NULL, proposal = NULL,
    source_filename = NULL,
    accepted_by = $2, accepted_at = now(), ingest_result = $3::jsonb,
    updated_at = now()
FROM old
WHERE r.id = old.id
RETURNING old.source_blob_key AS source_blob_key
"""

_REQUEUE_SQL = """
UPDATE resume_split_requests
SET status = 'queued', updated_at = now()
WHERE status IN ('queued', 'segmenting')
  AND updated_at < now() - make_interval(secs => $1::double precision * 60)
RETURNING id
"""

_EXPIRE_SQL = """
WITH old AS (
    SELECT id, source_blob_key FROM resume_split_requests
    WHERE status IN ('proposed', 'queued', 'failed')
      AND created_at < now() - make_interval(hours => $1::int)
    FOR UPDATE
)
UPDATE resume_split_requests r
SET status = 'discarded', source_blob_key = NULL, proposal = NULL,
    source_filename = NULL,
    updated_at = now()
FROM old
WHERE r.id = old.id
RETURNING old.source_blob_key AS source_blob_key
"""

_REFERENCED_SQL = """
SELECT source_blob_key FROM resume_split_requests
WHERE source_blob_key IS NOT NULL
"""


async def create(
    conn: DbConn,
    blob_store: BlobStore,
    *,
    job_id: UUID,
    filename: str,
    data: bytes,
    consent_acknowledged: bool,
    created_by: str | None,
) -> UUID:
    """Store the combined PDF under a server-generated key and insert a queued
    row. The blob is written first, so a failed write leaves no row."""
    if not consent_acknowledged:
        raise ValueError("consent must be acknowledged before a split is created")
    split_id = uuid4()
    key = f"{SOURCE_PREFIX}{split_id}.pdf"
    await blob_store.put(key, data, "application/pdf")
    try:
        await conn.execute(
            _INSERT_SQL,
            split_id,
            job_id,
            key,
            hashlib.sha256(data).hexdigest(),
            filename[:_FILENAME_MAX],
            consent_acknowledged,
            created_by,
        )
    except BaseException:
        await blob_store.delete(key)
        raise
    return split_id


async def transition(conn: DbConn, split_id: UUID, *, expected: str, to: str) -> bool:
    """Compare-and-set ``expected -> to``. True iff this call won."""
    if expected not in LEGAL_TRANSITIONS or to not in LEGAL_TRANSITIONS[expected]:
        raise ValueError(f"illegal split-request transition {expected!r} -> {to!r}")
    won = await conn.fetchval(_TRANSITION_SQL, split_id, expected, to)
    return won is not None


async def claim(conn: DbConn, split_id: UUID) -> SplitClaim | None:
    """CAS queued -> segmenting; ``None`` when another worker already did."""
    row = await conn.fetchrow(_CLAIM_SQL, split_id)
    if row is None:
        return None
    return SplitClaim(
        id=row["id"],
        job_id=row["job_id"],
        source_blob_key=row["source_blob_key"],
        blind_review=bool(row["blind_review"]),
    )


async def store_proposal(
    conn: DbConn,
    split_id: UUID,
    proposal: SplitProposal,
    *,
    diagnostics: ProposalDiagnostics,
    method: str,
    fallback_reason: str | None,
    blind_review: bool,
    page_count: int,
) -> bool:
    """CAS segmenting -> proposed with the proposal encrypted at rest."""
    if method not in METHODS:
        raise ValueError(f"unknown split method {method!r}")
    if method == "llm" and fallback_reason is not None:
        raise ValueError("an llm-method proposal carries no fallback reason")
    if method == "heuristic" and fallback_reason not in FALLBACK_REASONS:
        raise ValueError("a heuristic proposal needs a fixed fallback reason")

    to_store = proposal
    if blind_review:
        # Guessed names are never persisted on a blind-review job.
        to_store = proposal.model_copy(
            update={
                "applicants": [
                    a.model_copy(update={"candidate_name": ""})
                    for a in proposal.applicants
                ]
            }
        )
    async with conn.transaction():
        await pii.set_pii_key(conn)
        cipher = await pii.encrypt(conn, to_store.model_dump_json())
        won = await conn.fetchval(
            _STORE_PROPOSAL_SQL,
            split_id,
            cipher,
            json.dumps(diagnostics.model_dump()),
            method,
            fallback_reason,
            page_count,
        )
    return won is not None


_CORRECT_SQL = """
UPDATE resume_split_requests
SET proposal = $3, proposal_version = proposal_version + 1,
    diagnostics = $4::jsonb, updated_at = now()
WHERE id = $1 AND status = 'proposed' AND proposal_version = $2
RETURNING proposal_version
"""


async def store_correction(
    conn: DbConn,
    split_id: UUID,
    proposal: SplitProposal,
    *,
    expected_version: int,
    diagnostics: ProposalDiagnostics,
    blind_review: bool,
) -> int | None:
    """CAS proposed -> proposed at ``expected_version``: replace the encrypted
    proposal, bump the version and return it (``None`` when another writer won)."""
    to_store = proposal
    if blind_review:
        to_store = proposal.model_copy(
            update={
                "applicants": [
                    a.model_copy(update={"candidate_name": ""})
                    for a in proposal.applicants
                ]
            }
        )
    async with conn.transaction():
        await pii.set_pii_key(conn)
        cipher = await pii.encrypt(conn, to_store.model_dump_json())
        version = await conn.fetchval(
            _CORRECT_SQL,
            split_id,
            expected_version,
            cipher,
            json.dumps(diagnostics.model_dump()),
        )
    return None if version is None else int(version)


async def load_proposal(conn: DbConn, split_id: UUID) -> SplitProposal | None:
    """Decrypt and parse the stored proposal; ``None`` if there is none."""
    async with conn.transaction():
        await pii.set_pii_key(conn)
        cipher = await conn.fetchval(_LOAD_PROPOSAL_SQL, split_id)
        if cipher is None:
            return None
        plain = await pii.decrypt(conn, cipher)
    if plain is None:
        return None
    return SplitProposal.model_validate_json(plain)


async def _delete_blob(blob_store: BlobStore, key: str | None) -> None:
    if key:
        await blob_store.delete(key)


async def fail(
    conn: DbConn, blob_store: BlobStore, split_id: UUID, reason: str
) -> bool:
    """CAS queued|segmenting -> failed, releasing the blob and proposal."""
    if reason not in FAILURE_REASONS:
        raise ValueError("failure reason must be one of FAILURE_REASONS")
    row = await conn.fetchrow(_FAIL_SQL, split_id, reason)
    if row is None:
        return False
    await _delete_blob(blob_store, row["source_blob_key"])
    return True


async def discard(conn: DbConn, blob_store: BlobStore, split_id: UUID) -> bool:
    """CAS queued|proposed|failed -> discarded, releasing blob and proposal."""
    row = await conn.fetchrow(
        _RELEASE_SQL, split_id, ["queued", "proposed", "failed"], "discarded"
    )
    if row is None:
        return False
    await _delete_blob(blob_store, row["source_blob_key"])
    return True


async def mark_accepted(
    conn: DbConn,
    blob_store: BlobStore,
    split_id: UUID,
    *,
    accepted_by: str | None,
    ingest_result: list[dict[str, Any]],
    release_blob: bool = True,
) -> bool:
    """CAS proposed -> accepted, storing the ingest result for idempotent
    replays and releasing the proposal. ``release_blob=False`` leaves the blob
    for the caller to delete AFTER its transaction commits (a rollback must not
    have already destroyed the source of a proposal that is still 'proposed')."""
    row = await conn.fetchrow(
        _ACCEPT_SQL, split_id, accepted_by, json.dumps(ingest_result)
    )
    if row is None:
        return False
    if release_blob:
        await _delete_blob(blob_store, row["source_blob_key"])
    return True


async def requeue_stale(conn: DbConn, *, minutes: float) -> list[UUID]:
    """Reset queued/segmenting rows idle for ``minutes`` to queued and return
    their ids; the caller re-runs them heuristic-only."""
    rows = await conn.fetch(_REQUEUE_SQL, minutes)
    return [r["id"] for r in rows]


async def expire(
    conn: DbConn, blob_store: BlobStore, *, ttl_hours: int | None = None
) -> int:
    """Discard unaccepted rows older than the TTL and free their blobs."""
    ttl = ttl_hours if ttl_hours is not None else get_settings().split_proposal_ttl_h
    rows = await conn.fetch(_EXPIRE_SQL, ttl)
    for row in rows:
        await _delete_blob(blob_store, row["source_blob_key"])
    return len(rows)


async def sweep_orphan_blobs(conn: DbConn, blob_store: BlobStore) -> int:
    """Delete ``split_sources/*`` blobs no row references (a crash between a
    terminal UPDATE and the unlink, or an aborted create)."""
    listed = await blob_store.list_keys(SOURCE_PREFIX)
    keys = [k for k in listed if k.startswith(SOURCE_PREFIX)]
    if not keys:
        return 0
    referenced = {r["source_blob_key"] for r in await conn.fetch(_REFERENCED_SQL)}
    orphans: list[str] = []
    for k in keys:
        if k in referenced:
            continue
        age = await blob_store.age_seconds(k)
        if age is None or age < ORPHAN_GRACE_S:
            continue
        orphans.append(k)
    for key in orphans:
        await blob_store.delete(key)
    return len(orphans)


async def list_pending(conn: DbConn, job_id: UUID) -> list[dict[str, Any]]:
    """A job's splits still awaiting action (queued, segmenting, proposed,
    failed), newest first. Metadata only: never the proposal or any name."""
    rows = await conn.fetch(
        "SELECT id, status, created_at, page_count, method, source_filename "
        "FROM resume_split_requests "
        "WHERE job_id = $1 AND status IN "
        "('queued', 'segmenting', 'proposed', 'failed') "
        "ORDER BY created_at DESC, id",
        job_id,
    )
    return [dict(r) for r in rows]
