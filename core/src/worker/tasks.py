"""arq task implementations — the JD half of the parse pipeline.

Ported from hris ``apps/worker/src/worker/tasks.py`` (see
``phase3-source-dossier.md`` §8), trimmed to ``parse_job``.

Tasks resolve their dependencies from ``ctx``, which ``src/worker/main.py::
startup`` builds:

  - ``ctx["pg_pool"]``  asyncpg.Pool   (NOT hris's ``ctx["pool"]``)
  - ``ctx["llm"]``      src.pipeline.llm.LLMClient
  - ``ctx["embedder"]`` src.pipeline.llm.CachedEmbedder
  - ``ctx["blob_store"]`` src.storage.blob_store.BlobStore
  - ``ctx["neo4j"]``    neo4j.AsyncDriver

Phase 3 stops at: parse -> write Postgres -> enqueue an ``outbox`` row. The
graph projection (``project_to_graph`` / ``_project_job`` / ``normalize_skill``)
is Phase 4, so the outbox rows written here sit undelivered until that drainer
lands — the outbox pattern working as intended, not a dropped requirement.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from uuid import UUID

from src.pipeline.llm import CachedEmbedder, LLMClient, LLMOutputInvalidError
from src.pipeline.skills import build_summary_text
from src.prompts import load_prompt
from src.schemas import JDExtracted
from src.services import job_service, outbox_service

log = logging.getLogger(__name__)

_MAX_REASON_CHARS = 1000

_JOB_META_SQL = "SELECT description_raw, status FROM jobs WHERE id = $1"


async def parse_job(ctx: dict[str, Any], job_id_str: str) -> str:
    """Pull the JD, run LLM extraction, embed the summary, write back, and
    enqueue the outbox event Phase 4's drainer will project.

    Returns one of: ``"parsed"``, ``"missing"``, ``"stale"``, ``"failed"``.

    Idempotent: re-running against a job that already left 'draft' is a no-op
    (``job_service.record_parsed`` only updates rows still in 'draft').
    """
    pool = ctx["pg_pool"]
    llm: LLMClient = ctx["llm"]
    embedder: CachedEmbedder = ctx["embedder"]
    job_id = UUID(job_id_str)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(_JOB_META_SQL, job_id)
        if row is None:
            log.warning("parse_job.missing job_id=%s", job_id_str)
            return "missing"
        if row["status"] != "draft":
            log.info(
                "parse_job.skipped job_id=%s status=%s reason=not-in-draft",
                job_id_str,
                row["status"],
            )
            return "stale"

        # Two distinct except branches on purpose: an invalid LLM payload is an
        # EXPECTED outcome with a small local model (logged at error, no stack),
        # whereas anything else is a genuine bug and gets a traceback. Both
        # record the failure on the row and return "failed" — and neither
        # enqueues an outbox row, because nothing was written to project.
        try:
            prompt = load_prompt("jd_extract_v1", jd_text=row["description_raw"])
            extracted = await llm.chat_json(
                prompt.messages, JDExtracted, max_tokens=2048, max_retries=1
            )
        except LLMOutputInvalidError as exc:
            log.error("parse_job.llm_invalid job_id=%s error=%s", job_id_str, exc)
            await job_service.record_parse_failure(
                conn,
                job_id=job_id,
                reason=f"llm output invalid: {exc}"[:_MAX_REASON_CHARS],
            )
            return "failed"
        except Exception as exc:  # noqa: BLE001 — recorded on the row, not swallowed
            log.exception("parse_job.unexpected job_id=%s", job_id_str)
            await job_service.record_parse_failure(
                conn,
                job_id=job_id,
                reason=f"{type(exc).__name__}: {exc}"[:_MAX_REASON_CHARS],
            )
            return "failed"

        summary_text = build_summary_text(extracted)
        [embedding] = await embedder.embed([summary_text])

        # The write-back and its outbox row commit together or not at all.
        async with conn.transaction():
            applied = await job_service.record_parsed(
                conn,
                job_id=job_id,
                extracted=extracted,
                parsed_at=dt.datetime.now(dt.UTC),
            )
            if not applied:
                log.info(
                    "parse_job.race job_id=%s note=%s",
                    job_id_str,
                    "row left draft state mid-parse; dropping result",
                )
                return "stale"

            await outbox_service.enqueue_outbox(
                conn,
                aggregate="job",
                aggregate_id=job_id,
                event_type="job.parsed",
                payload={
                    "embedding": embedding,
                    "extracted": extracted.model_dump(),
                    "prompt_version": prompt.version,
                },
            )

    log.info(
        "parse_job.ok job_id=%s required_skills=%d nice_to_have=%d",
        job_id_str,
        len(extracted.required_skills),
        len(extracted.nice_to_have_skills),
    )
    return "parsed"
