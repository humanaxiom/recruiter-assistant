"""Persistence and state machine behind the résumé export background job.

One ``resume_export_requests`` row per requested ZIP. Every transition is a
compare-and-set ``UPDATE ... WHERE status = <expected>`` so a redelivered job or
a double click cannot build twice. A built ZIP holds every applicant's identity:
it lives in the ``BlobStore`` under :data:`EXPORT_PREFIX` only until
``expires_at``, and ``blob_key`` is NULLed in the same step that releases the
blob when the row turns ``expired`` or ``failed``.

Privacy: ``counts`` carries numbers only. The failure path stores nothing at
all -- an exception message from a PDF parser can quote a candidate's page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from src.services import DbConn
from src.settings import get_settings
from src.storage.blob_store import BlobStore

EXPORT_PREFIX = "resume_exports/"
# An unreferenced blob younger than this is a build in flight (the blob is
# written BEFORE the row is marked ready), not an orphan.
ORPHAN_GRACE_S = 3600.0
# A queued/building row older than this lost its worker (arq's job_timeout is
# an hour): fail it so the requester can ask again.
STALE_MINUTES = 120

_STATUS_FIELDS = "id, job_id, requested_by, status, counts, created_at, expires_at"


@dataclass(frozen=True)
class ExportClaim:
    id: UUID
    job_id: UUID
    blind_review: bool


def counts_of(raw: Any) -> dict[str, Any]:
    """The ``counts`` JSONB as a dict (asyncpg hands back text without a codec)."""
    value = json.loads(raw) if isinstance(raw, str | bytes) else raw
    return dict(value) if isinstance(value, dict) else {}


async def job_blind_review(conn: DbConn, job_id: UUID) -> bool | None:
    """``None`` when the job does not exist."""
    row = await conn.fetchrow("SELECT blind_review FROM jobs WHERE id = $1", job_id)
    return None if row is None else bool(row["blind_review"])


async def count_live_applicants(conn: DbConn, job_id: UUID) -> int:
    n = await conn.fetchval(
        "SELECT count(*) FROM resumes WHERE job_id = $1 AND withdrawn_at IS NULL",
        job_id,
    )
    return int(n or 0)


async def create(conn: DbConn, job_id: UUID, requested_by: str) -> UUID:
    export_id: UUID = await conn.fetchval(
        "INSERT INTO resume_export_requests (job_id, requested_by) "
        "VALUES ($1, $2) RETURNING id",
        job_id,
        requested_by,
    )
    return export_id


async def get(conn: DbConn, export_id: UUID) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        f"SELECT {_STATUS_FIELDS} FROM resume_export_requests WHERE id = $1",
        export_id,
    )
    return None if row is None else dict(row)


async def latest_for(
    conn: DbConn, job_id: UUID, requested_by: str
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        f"SELECT {_STATUS_FIELDS} FROM resume_export_requests "
        "WHERE job_id = $1 AND requested_by = $2 "
        "ORDER BY created_at DESC, id LIMIT 1",
        job_id,
        requested_by,
    )
    return None if row is None else dict(row)


async def get_for_download(
    conn: DbConn, export_id: UUID, requested_by: str
) -> dict[str, Any] | None:
    """The requester's own row (nobody else's, admins included), with whether it
    is downloadable right now: ``ready``, unexpired, holding a blob."""
    row = await conn.fetchrow(
        "SELECT r.id, r.job_id, r.status, r.blob_key, r.counts, "
        "       (r.status = 'ready' AND r.blob_key IS NOT NULL "
        "        AND (r.expires_at IS NULL OR r.expires_at > now())) AS downloadable, "
        "       j.blind_review "
        "FROM resume_export_requests r JOIN jobs j ON j.id = r.job_id "
        "WHERE r.id = $1 AND r.requested_by = $2",
        export_id,
        requested_by,
    )
    return None if row is None else dict(row)


async def claim(conn: DbConn, export_id: UUID) -> ExportClaim | None:
    """CAS queued -> building; ``None`` when unknown or another worker won."""
    row = await conn.fetchrow(
        "WITH c AS ("
        "  UPDATE resume_export_requests SET status = 'building' "
        "  WHERE id = $1 AND status = 'queued' RETURNING id, job_id"
        ") SELECT c.id, c.job_id, j.blind_review FROM c JOIN jobs j ON j.id = c.job_id",
        export_id,
    )
    if row is None:
        return None
    return ExportClaim(
        id=row["id"], job_id=row["job_id"], blind_review=bool(row["blind_review"])
    )


async def mark_ready(
    conn: DbConn, export_id: UUID, blob_key: str, counts: dict[str, int]
) -> bool:
    """CAS building -> ready, stamping the TTL from now."""
    ttl = get_settings().resume_export_ttl_minutes
    result: str = await conn.execute(
        "UPDATE resume_export_requests SET status = 'ready', blob_key = $2, "
        "counts = $3::jsonb, expires_at = now() + make_interval(mins => $4) "
        "WHERE id = $1 AND status = 'building'",
        export_id,
        blob_key,
        json.dumps(counts),
        ttl,
    )
    return result.endswith(" 1")


async def mark_failed(conn: DbConn, export_id: UUID) -> bool:
    """CAS queued|building -> failed. Stores nothing: no reason, no message."""
    result: str = await conn.execute(
        "UPDATE resume_export_requests SET status = 'failed', blob_key = NULL "
        "WHERE id = $1 AND status IN ('queued', 'building')",
        export_id,
    )
    return result.endswith(" 1")


async def expire(conn: DbConn, blob_store: BlobStore) -> int:
    """Expire ready rows past ``expires_at``, fail rows whose worker vanished,
    and delete every blob a dead row held. Returns how many rows changed."""
    expired = await conn.fetch(
        "UPDATE resume_export_requests r SET status = 'expired', blob_key = NULL "
        "FROM (SELECT id, blob_key FROM resume_export_requests "
        "      WHERE status = 'ready' AND expires_at <= now() FOR UPDATE) old "
        "WHERE r.id = old.id RETURNING old.blob_key AS blob_key"
    )
    stale = await conn.fetch(
        "UPDATE resume_export_requests SET status = 'failed', blob_key = NULL "
        "WHERE status IN ('queued', 'building') "
        "AND created_at < now() - make_interval(mins => $1) RETURNING id",
        STALE_MINUTES,
    )
    leaked = await conn.fetch(
        "UPDATE resume_export_requests r SET blob_key = NULL "
        "FROM (SELECT id, blob_key FROM resume_export_requests "
        "      WHERE status IN ('expired', 'failed') AND blob_key IS NOT NULL "
        "      FOR UPDATE) dead "
        "WHERE r.id = dead.id RETURNING dead.blob_key AS blob_key"
    )
    for row in (*expired, *leaked):
        if row["blob_key"]:
            await blob_store.delete(row["blob_key"])
    return len(expired) + len(stale) + len(leaked)


async def sweep_orphan_blobs(conn: DbConn, blob_store: BlobStore) -> int:
    """Delete ``resume_exports/*`` blobs no row references and that are older
    than the grace period (a crash between the put and the ready UPDATE)."""
    listed = await blob_store.list_keys(EXPORT_PREFIX)
    keys = [k for k in listed if k.startswith(EXPORT_PREFIX)]
    if not keys:
        return 0
    referenced = {
        r["blob_key"]
        for r in await conn.fetch(
            "SELECT blob_key FROM resume_export_requests WHERE blob_key IS NOT NULL"
        )
    }
    swept = 0
    for key in keys:
        if key in referenced:
            continue
        age = await blob_store.age_seconds(key)
        if age is None or age < ORPHAN_GRACE_S:
            continue
        await blob_store.delete(key)
        swept += 1
    return swept
