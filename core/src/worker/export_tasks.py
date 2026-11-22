"""arq tasks for the résumé export background job.

``build_resume_export`` claims a queued ``resume_export_requests`` row, reads the
job's live applicants and their stored files, builds the as-is + anonymized ZIP
and parks it in the ``BlobStore`` under ``resume_exports/`` until ``expires_at``.
The PDF work runs in ``asyncio.to_thread`` and the database connection is
RELEASED before it starts, so a slow build never pins a pooled connection.

``expire_resume_exports`` is the hourly cron: it expires ready rows past their
TTL, fails rows whose worker vanished, and sweeps orphan ZIP blobs.

Logging discipline: exception CLASS names only, never a message -- a PDF parser
or a blob error can quote a candidate's name. The failure path stores nothing at
all on the row for the same reason.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from typing import Any
from uuid import UUID

from src.services import pii
from src.services import resume_export_request_service as requests
from src.services import resume_export_service as export
from src.settings import get_settings
from src.storage.blob_store import BlobNotFound, BlobStore

log = logging.getLogger(__name__)

_ROWS_SQL = """
SELECT blob_key, cover_letter_blob_key, candidate_name, candidate_email,
       candidate_phone, parsed,
       cover_letter_text IS NOT NULL AS cover_typed
FROM resumes
WHERE job_id = $1 AND withdrawn_at IS NULL
ORDER BY uploaded_at, id
LIMIT $2
"""

_LIVE_COUNT_SQL = """
SELECT count(*) FROM resumes WHERE job_id = $1 AND withdrawn_at IS NULL
"""


async def _read(blob_store: BlobStore, key: str | None) -> bytes | None:
    """The stored bytes, or ``None`` when the file is gone (listed in the
    manifest as "file missing"; one lost blob never fails the export)."""
    if not key:
        return None
    try:
        return await blob_store.get(key)
    except BlobNotFound:
        return None


async def _applicant_rows(
    conn: Any, job_id: UUID, cap: int
) -> tuple[list[dict[str, Any]], int]:
    """The job's live applicants (at most ``cap``: the request refused a bigger
    job, but a job can grow before the build) with the identity columns
    decrypted, and how many live applicants the cap left out. The pii key is
    transaction-scoped, so everything happens inside one transaction."""
    async with conn.transaction():
        await pii.set_pii_key(conn)
        rows = await conn.fetch(_ROWS_SQL, job_id, cap)
        live = int(await conn.fetchval(_LIVE_COUNT_SQL, job_id) or 0)
        left_out = max(0, live - cap)
        held = [
            {
                "blob_key": r["blob_key"],
                "cover_key": r["cover_letter_blob_key"],
                "name": await pii.decrypt(conn, r["candidate_name"]),
                "email": await pii.decrypt(conn, r["candidate_email"]),
                "phone": await pii.decrypt(conn, r["candidate_phone"]),
                "parsed": r["parsed"],
                "cover_typed": bool(r["cover_typed"]),
            }
            for r in rows
        ]
        return held, left_out


def _parsed(raw: Any) -> dict[str, Any] | None:
    value = json.loads(raw) if isinstance(raw, str | bytes) else raw
    return value if isinstance(value, dict) else None


async def _load_applicants(
    blob_store: BlobStore, rows: list[dict[str, Any]]
) -> list[export.ExportApplicant]:
    applicants: list[export.ExportApplicant] = []
    for r in rows:
        resume_pdf = await _read(blob_store, r["blob_key"])
        cover_pdf = await _read(blob_store, r["cover_key"])
        applicants.append(
            export.ExportApplicant(
                resume_pdf=resume_pdf,
                cover_pdf=cover_pdf,
                terms=export.collect_terms(
                    r["name"],
                    _parsed(r["parsed"]),
                    email=r["email"],
                    phone=r["phone"],
                ),
                cover_missing=bool(r["cover_key"]) and cover_pdf is None,
                cover_typed=bool(r["cover_typed"]) and not r["cover_key"],
            )
        )
    return applicants


async def build_resume_export(ctx: dict[str, Any], export_id: str) -> str:
    """Build one export. Returns ``"ready"``, ``"failed"`` or ``"skipped"``
    (malformed or unknown id, or another worker already owns the row)."""
    try:
        eid = UUID(export_id)
    except ValueError:
        return "skipped"
    pool = ctx["pg_pool"]
    blob_store: BlobStore = ctx["blob_store"]
    # arq counts attempts from 1; a retry means the first attempt died holding
    # the row in "building".
    retry = int(ctx.get("job_try", 1)) > 1

    async with pool.acquire() as conn:
        claim = await requests.claim(conn, eid, retry=retry)
    if claim is None:
        return "skipped"

    key = f"{requests.EXPORT_PREFIX}{eid}.zip"
    stored = False
    try:
        async with pool.acquire() as conn:
            rows, left_out = await _applicant_rows(
                conn, claim.job_id, get_settings().resume_export_max_applicants
            )
        # The connection is back in the pool before any file or PDF work.
        applicants = await _load_applicants(blob_store, rows)
        result = await asyncio.to_thread(
            functools.partial(
                export.build_export,
                applicants,
                blind_review=claim.blind_review,
                not_exported=left_out,
            )
        )
        await blob_store.put(key, result.data, "application/zip")
        stored = True
        counts = {
            "applicants": result.applicants,
            "anonymized": result.anonymized,
            "not_anonymized": result.not_anonymized,
        }
        async with pool.acquire() as conn:
            marked = await requests.mark_ready(conn, eid, key, counts)
        if not marked:
            await blob_store.delete(key)
            return "skipped"
    except Exception as exc:  # noqa: BLE001 - classified; the row stores nothing
        log.error("export.build.failed", extra={"error": type(exc).__name__})
        try:
            if stored:
                await blob_store.delete(key)
            async with pool.acquire() as conn:
                await requests.mark_failed(conn, eid)
        except Exception as cleanup:  # noqa: BLE001 - the sweep finishes the job
            log.error("export.build.cleanup", extra={"error": type(cleanup).__name__})
        return "failed"
    log.info("export.build.ready", extra={"applicants": result.applicants})
    return "ready"


async def expire_resume_exports(ctx: dict[str, Any]) -> str:
    """Hourly: expire stale rows and sweep orphan ZIP blobs. Never raises."""
    pool = ctx.get("pg_pool")
    blob_store = ctx.get("blob_store")
    if pool is None or blob_store is None:
        return "unavailable"
    try:
        async with pool.acquire() as conn:
            expired = await requests.expire(conn, blob_store)
            swept = await requests.sweep_orphan_blobs(conn, blob_store)
    except Exception as exc:  # noqa: BLE001 - a cron tick must never crash
        log.error("export.expire.error", extra={"error": type(exc).__name__})
        return "error"
    log.info("export.expire.ok", extra={"expired": expired, "swept": swept})
    return f"expired={expired} swept={swept}"
