"""arq tasks for the in-app Taleo combined-export split.

``segment_taleo_export`` claims a queued ``resume_split_requests`` row, reads the
stored combined PDF, and proposes a segmentation: the local LLM first (stage
``llm_segment`` -> orphan-page repair), falling back automatically to the offline
contact-block heuristic on ANY model failure. Findings are recomputed
server-side from the per-page text (``validate_proposal``); the model's output is
never trusted to account for pages.

``expire_split_requests`` is the hourly cron: it discards unaccepted requests
past their TTL, sweeps orphan ``split_sources/`` blobs, and re-queues requests
that stalled mid-segmentation as heuristic-only re-runs (the model is what most
likely stalled them).

Logging discipline: never a proposal, page text, name or exception MESSAGE from
segmentation -- exception CLASS names only. A parser or model message can quote
a page of a candidate's document.

The CPU/PDF work (text extraction, validation, heuristic) runs in
``asyncio.to_thread`` so the worker's event loop keeps serving other tasks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from uuid import UUID

import fitz  # type: ignore[import-untyped]

from src.pipeline.llm import LLMOutputInvalidError, LLMUnavailableError
from src.schemas.taleo_split import SplitProposal
from src.services import split_request_service, taleo_split_service
from src.settings import get_settings
from src.storage.blob_store import BlobNotFound

log = logging.getLogger(__name__)

#: ctx keys ``segment_taleo_export`` reads. Pinned against ``worker.main.startup``
#: by a test so the task cannot ship reading a key the worker never sets.
_REQUIRED_CTX_KEYS = ("pg_pool", "blob_store", "llm")

#: The arq queue client the reaper enqueues re-runs on (NOT ``ctx["redis"]``,
#: which startup overwrites with a plain client).
_QUEUE_CTX_KEY = "arq"

#: A queued/segmenting request idle this long is stalled, not busy.
_STALE_MINUTES = 30


def _fallback_reason(exc: Exception) -> str:
    if isinstance(exc, LLMUnavailableError):
        return "llm_unavailable"
    if isinstance(exc, LLMOutputInvalidError):
        return "llm_invalid_output"
    return "llm_error"


def _is_encrypted(pdf: bytes) -> bool:
    """True if the PDF opens but needs a password."""
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:  # noqa: BLE001 - classification only; never surfaced
        return False
    try:
        return bool(doc.needs_pass)
    finally:
        doc.close()


async def segment_taleo_export(
    ctx: dict[str, Any], split_id: str, heuristic_only: bool = False
) -> str:
    """Segment one combined export. Returns ``"proposed"``, ``"failed"`` or
    ``"skipped"`` (another worker owns it, or the row moved on)."""
    pool = ctx["pg_pool"]
    blob_store = ctx["blob_store"]
    llm = ctx["llm"]
    sid = UUID(split_id)
    srs = split_request_service
    svc = taleo_split_service

    async with pool.acquire() as conn:
        claim = await srs.claim(conn, sid)
    if claim is None:
        return "skipped"

    async def _fail(reason: str) -> str:
        async with pool.acquire() as conn:
            await srs.fail(conn, blob_store, sid, reason)
        log.info("split.segment.failed", extra={"split_id": split_id, "reason": reason})
        return "failed"

    try:
        data = await blob_store.get(claim.source_blob_key)
    except BlobNotFound:
        return await _fail("source_missing")

    try:
        texts: list[str] = await asyncio.to_thread(svc.extract_page_texts, data)
    except Exception as exc:  # noqa: BLE001 - classified into a static reason
        log.warning("split.segment.extract_error", extra={"error": type(exc).__name__})
        if await asyncio.to_thread(_is_encrypted, data):
            return await _fail("encrypted_pdf")
        return await _fail("unreadable_pdf")
    if not texts:
        return await _fail("unreadable_pdf")
    if await asyncio.to_thread(_is_encrypted, data):
        return await _fail("encrypted_pdf")
    if len(texts) > get_settings().split_max_pages:
        return await _fail("too_many_pages")
    if not any(t.strip() for t in texts):
        return await _fail("no_text_layer")

    proposal: SplitProposal
    fallback_reason: str | None
    if heuristic_only:
        proposal = await asyncio.to_thread(svc.heuristic_proposal, texts)
        method, fallback_reason = "heuristic", "requeued_after_stall"
    else:
        try:
            raw = await svc.llm_segment(llm, texts)
            repaired, _report = await asyncio.to_thread(
                svc.repair_orphan_pages,
                raw.applicants,
                len(texts),
                svc.orphan_page_emails(texts),
            )
            # ignored_pages is manager-only: the model's is always dropped.
            proposal = SplitProposal(applicants=repaired, ignored_pages=[])
            method, fallback_reason = "llm", None
        except Exception as exc:  # noqa: BLE001 - any model failure -> heuristic
            log.warning(
                "split.segment.llm_fallback", extra={"error": type(exc).__name__}
            )
            proposal = await asyncio.to_thread(svc.heuristic_proposal, texts)
            method, fallback_reason = "heuristic", _fallback_reason(exc)

    diagnostics = await asyncio.to_thread(svc.validate_proposal, proposal, texts)
    async with pool.acquire() as conn:
        stored = await srs.store_proposal(
            conn,
            sid,
            proposal,
            diagnostics=diagnostics,
            method=method,
            fallback_reason=fallback_reason,
            blind_review=claim.blind_review,
            page_count=len(texts),
        )
    if not stored:
        return "skipped"
    log.info(
        "split.segment.proposed",
        extra={"split_id": split_id, "method": method, "pages": len(texts)},
    )
    return "proposed"


async def expire_split_requests(ctx: dict[str, Any]) -> str:
    """Hourly: expire stale proposals, sweep orphan blobs, re-queue stalled
    segmentations heuristic-only. Never raises."""
    pool = ctx.get("pg_pool")
    blob_store = ctx.get("blob_store")
    arq = ctx.get(_QUEUE_CTX_KEY)
    if pool is None or blob_store is None or arq is None:
        return "unavailable"
    srs = split_request_service
    try:
        async with pool.acquire() as conn:
            expired = await srs.expire(conn, blob_store)
            swept = await srs.sweep_orphan_blobs(conn, blob_store)
            stale = await srs.requeue_stale(conn, minutes=_STALE_MINUTES)
        for sid in stale:
            await arq.enqueue_job(
                "segment_taleo_export",
                str(sid),
                heuristic_only=True,
                # Time-suffixed: arq drops a repeat of a still-remembered _job_id.
                _job_id=f"split-requeue-{sid}-{int(time.time())}",
            )
    except Exception as exc:  # noqa: BLE001 - a cron tick must never crash
        log.error("split.expire.error", extra={"error": type(exc).__name__})
        return "error"
    log.info(
        "split.expire.ok",
        extra={"expired": expired, "swept": swept, "requeued": len(stale)},
    )
    return f"expired={expired} swept={swept} requeued={len(stale)}"
