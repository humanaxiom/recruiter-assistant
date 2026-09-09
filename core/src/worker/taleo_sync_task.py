"""``sync_taleo_jobs`` — the only scheduled egress in this system (ADR-046).

Fetches SFU's public SIMOFRAS careers listing, upserts each requisition into
``jobs``, and archives any Taleo-sourced row that disappeared upstream.

**Gated on ``TALEO_ENABLED``, which defaults to ``false``.** A disabled
deployment does not construct a client, so it makes no outbound request at all
— that is the promise the flag makes, and it is what the test asserts.

Ported from hris ``apps/worker/src/worker/taleo_sync_task.py``, with three
deviations:

* **``structlog`` -> stdlib ``logging``**, and ``ctx["pool"]`` -> ``ctx["pg_pool"]``
  (this repo's key; a verbatim port ``KeyError``s at runtime).
* **No ``cron_runs`` wrapper.** hris wraps the run in ``_run_with_cron_log`` so
  ``/admin/health`` can show the last outcome. There is no such table here; the
  ``audit_log`` row is the record, and it is written on every run including the
  ones that change nothing.
* **AN EMPTY LISTING ABORTS BEFORE THE SWEEP** — see :func:`sync_taleo_jobs`.
  This is a behaviour change, not a port, and it is the important one.

The HTTP client is created per run rather than shared through ``ctx``. It is
the only network client in the worker, it is used by one disabled-by-default
task, and a per-run client means a deployment with the flag off holds no
outbound socket at all.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx

from src.pipeline.sources.taleo import TaleoClient
from src.services import job_source_service
from src.services.job_source_service import (
    ExternalJobUpsert,
    build_description_raw,
    normalise_employment_type,
)
from src.settings import get_settings

log = logging.getLogger(__name__)


async def sync_taleo_jobs(
    ctx: dict[str, Any], triggered_by_str: str | None = None
) -> dict[str, Any]:
    """Upsert SFU's public Taleo postings. Returns a summary dict.

    ``triggered_by_str`` is set when an admin triggered the run by hand;
    ``None`` means the cron. It rides into the audit row so a run can be
    attributed.

    **An empty listing aborts the run before the archive sweep**, and that is a
    deliberate departure from the hris source.

    Zero parsed rows has two possible causes and they are indistinguishable
    from here: SFU genuinely took every posting down, or the careers template
    changed and the parsers now match nothing. A 200 response carrying an error
    page produces the second with no exception at all. hris logs a warning and
    proceeds — which runs the sweep, which archives **every Taleo-sourced job
    in the database**.

    Only one of those readings is safe to act on, so the run stops and reports
    a failure. The cost of being wrong in this direction is one stale day; in
    the other it is the whole job list.
    """
    settings = get_settings()
    if not settings.taleo_enabled:
        # Return BEFORE constructing anything that could open a socket.
        return {"outcome": "skipped", "reason": "TALEO_ENABLED=false"}

    pool = ctx["pg_pool"]
    arq = ctx.get("arq")
    run_started = dt.datetime.now(dt.UTC)

    async with httpx.AsyncClient(timeout=settings.taleo_timeout_s) as http:
        client = TaleoClient(
            http,
            base_url=settings.taleo_base_url,
            org=settings.taleo_org,
            cws=settings.taleo_cws,
            request_delay_s=settings.taleo_request_delay_s,
            max_pages=settings.taleo_max_pages,
        )
        listings = await client.fetch_listings()

        if not listings:
            log.error(
                "taleo.sync.empty_listing note=%s",
                "zero rows parsed — layout drift or an error page; "
                "aborting before the archive sweep",
            )
            return {
                "outcome": "failed",
                "reason": (
                    "empty listing: zero requisitions parsed. Refusing to run "
                    "the archive sweep, which would retire every "
                    "Taleo-sourced job. Check the careers page and the "
                    "vendored parser fixtures."
                ),
            }

        inserted = updated_changed = updated_unchanged = fetch_failures = 0
        reparse: list[str] = []

        for listing in listings:
            try:
                req = await client.fetch_requisition(listing)
            except Exception as exc:  # noqa: BLE001 — counted and reported below
                # One posting failing must not stop the feed: a single 404 on
                # SFU's side would otherwise block every other requisition
                # indefinitely, and the sweep with it.
                fetch_failures += 1
                log.warning(
                    "taleo.requisition.fetch_failed external_id=%s error=%s",
                    listing.external_id,
                    f"{type(exc).__name__}: {exc}",
                )
                continue

            payload = ExternalJobUpsert(
                external_id=req.external_id,
                external_url=req.external_url,
                title=req.title,
                description_raw=build_description_raw(
                    req.description_raw, req.structured_fields, req.pdf_url
                ),
                department=req.department,
                location=req.location,
                employment_type=normalise_employment_type(req.employment_type),
            )
            async with pool.acquire() as db, db.transaction():
                result = await job_source_service.upsert_external_job(db, payload)

            if result.was_inserted:
                inserted += 1
                reparse.append(str(result.job_id))
            elif result.description_changed:
                updated_changed += 1
                reparse.append(str(result.job_id))
            else:
                updated_unchanged += 1

    # Sweep + audit outside the HTTP client's scope — no network left to do.
    async with pool.acquire() as db, db.transaction():
        archived = await job_source_service.mark_missing_as_archived(
            db, run_started_at=run_started
        )
    async with pool.acquire() as db:
        await job_source_service.write_sync_audit(
            db,
            inserted=inserted,
            updated=updated_changed + updated_unchanged,
            archived=len(archived),
            triggered_by=triggered_by_str or "cron",
        )

    # Only NEW or CHANGED JDs are re-parsed. Re-parsing an unchanged one burns
    # an LLM call per job per day and can move extracted requirements
    # underneath a shortlist someone is already reading (ROADMAP §5).
    if arq is not None:
        for job_id in reparse:
            await arq.enqueue_job("parse_job", job_id, _job_id=f"parse_job:{job_id}")

    summary = {
        "outcome": "ok",
        "inserted": inserted,
        "updated_changed": updated_changed,
        "updated_unchanged": updated_unchanged,
        "archived": len(archived),
        "fetch_failures": fetch_failures,
        "triggered_by": triggered_by_str or "cron",
    }
    log.info("taleo.sync.complete %s", summary)
    return summary
