"""arq tasks for the ranking write path (Phase 4d).

``shortlist_job(ctx, job_id_str)``  — rank a job's résumé pool -> shortlist_entries.
``reverse_match_job(ctx, resume_id_str)`` — rank a résumé against parsed jobs
-> reverse_match_entries.

Mirrors ``worker/tasks.py::parse_job`` / ``resume_tasks.py::parse_resume``:
deps resolve off ``ctx`` (``pg_pool`` / ``neo4j`` / ``llm`` / ``embedder``), the
task returns a short status string, and the persist runs inside ONE
``conn.transaction()``.

ADR-009 REQUIREMENT 1 lands here: ``MatchingContext`` is built via
``matching_context_from_settings(get_settings(), ...)`` and ``MatchWeights`` via
``weights_from_settings(get_settings())`` — never the orchestrator's in-code /
``DEFAULT_WEIGHTS`` fallbacks. These are the real construction sites the 4c
settings bridge was proven for.

Status strings:

* ``"missing"``     — the job/résumé row is absent; the orchestrator is NEVER
  called.
* ``"not_parsed"``  — the row exists but isn't parsed (job
  ``description_parsed IS NULL`` / résumé ``status != 'parsed'``); the
  orchestrator is NEVER called.
* ``"persisted"``   — a non-empty result was ranked and written.
* ``"empty"``       — zero candidates ranked; STILL persisted (DELETE-first
  clears any stale prior run), but a distinct status so "ranked, zero results"
  is told apart from "ranked, wrote N rows".
* ``"already_running"`` — a concurrent duplicate run for the same entity
  already holds the Postgres advisory lock (``src.worker.job_lock``,
  ADR-010 §1 residual); the row is never even fetched and the orchestrator is
  never called.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from src.pipeline.matching.orchestrator import (
    generate_shortlist,
    match_resume_to_jobs,
    matching_context_from_settings,
)
from src.services.shortlist_service import persist_reverse_match, persist_shortlist
from src.settings import get_settings, weights_from_settings
from src.worker.job_lock import release_job_lock, try_job_lock

log = logging.getLogger(__name__)

_JOB_META_SQL = (
    "SELECT description_parsed, shortlist_top_percent FROM jobs WHERE id = $1"
)
_RESUME_STATUS_SQL = "SELECT status, withdrawn_at FROM resumes WHERE id = $1"
_PARSED_JOB_IDS_SQL = "SELECT id FROM jobs WHERE description_parsed IS NOT NULL"


async def shortlist_job(ctx: dict[str, Any], job_id_str: str) -> str:
    """Generate + persist the shortlist for one job."""
    pool = ctx["pg_pool"]
    job_id = UUID(job_id_str)
    settings = get_settings()

    async with pool.acquire() as conn:
        if not await try_job_lock(conn, "shortlist", job_id):
            log.info("shortlist_job.already_running job_id=%s", job_id_str)
            return "already_running"

        try:
            row = await conn.fetchrow(_JOB_META_SQL, job_id)
            if row is None:
                log.warning("shortlist_job.missing job_id=%s", job_id_str)
                return "missing"
            if row["description_parsed"] is None:
                log.info("shortlist_job.not_parsed job_id=%s", job_id_str)
                return "not_parsed"

            mc = matching_context_from_settings(
                settings,
                db=conn,
                neo4j=ctx["neo4j"],
                llm=ctx["llm"],
                embedder=ctx["embedder"],
            )
            result = await generate_shortlist(
                job_id,
                ctx=mc,
                weights=weights_from_settings(settings),
                coarse_k=settings.match_coarse_k,
                evidence_k=settings.match_evidence_k,
                top_percent=row["shortlist_top_percent"],
            )

            async with conn.transaction():
                await persist_shortlist(conn, result)
        finally:
            await release_job_lock(conn, "shortlist", job_id)

    persisted = bool(result.entries)
    log.info(
        "shortlist_job.done job_id=%s entries=%d",
        job_id_str,
        len(result.entries),
    )
    return "persisted" if persisted else "empty"


async def reverse_match_job(ctx: dict[str, Any], resume_id_str: str) -> str:
    """Generate + persist the reverse match (résumé -> jobs) for one résumé."""
    pool = ctx["pg_pool"]
    resume_id = UUID(resume_id_str)
    settings = get_settings()

    async with pool.acquire() as conn:
        if not await try_job_lock(conn, "reverse_match", resume_id):
            log.info("reverse_match_job.already_running resume_id=%s", resume_id_str)
            return "already_running"

        try:
            row = await conn.fetchrow(_RESUME_STATUS_SQL, resume_id)
            if row is None:
                log.warning("reverse_match_job.missing resume_id=%s", resume_id_str)
                return "missing"
            # ADR-026 (FU-8): a withdrawn résumé is excluded from ranking — a
            # DISTINCT status from "not_parsed" (checked FIRST so a withdrawn
            # row that also happens to be unparsed is reported as withdrawn),
            # the orchestrator is never called, and nothing is persisted.
            if row["withdrawn_at"] is not None:
                log.info("reverse_match_job.withdrawn resume_id=%s", resume_id_str)
                return "withdrawn"
            if row["status"] != "parsed":
                log.info("reverse_match_job.not_parsed resume_id=%s", resume_id_str)
                return "not_parsed"

            # Reverse match must never rank a résumé against an UNPARSED JD (no
            # required/nice-to-have skills to score against). Scope to parsed
            # jobs and pass an explicit set — never None (which means "no
            # filter" to match_resume_to_jobs).
            job_rows = await conn.fetch(_PARSED_JOB_IDS_SQL)
            allowed_job_ids: set[UUID] = {r["id"] for r in job_rows}

            mc = matching_context_from_settings(
                settings,
                db=conn,
                neo4j=ctx["neo4j"],
                llm=ctx["llm"],
                embedder=ctx["embedder"],
            )
            result = await match_resume_to_jobs(
                resume_id,
                ctx=mc,
                allowed_job_ids=allowed_job_ids,
                weights=weights_from_settings(settings),
                coarse_k=settings.match_coarse_k,
                evidence_k=settings.match_reverse_evidence_k,
            )

            async with conn.transaction():
                await persist_reverse_match(conn, result)
        finally:
            await release_job_lock(conn, "reverse_match", resume_id)

    persisted = bool(result.entries)
    log.info(
        "reverse_match_job.done resume_id=%s entries=%d",
        resume_id_str,
        len(result.entries),
    )
    return "persisted" if persisted else "empty"
