"""arq task for the interview-question generator (S2).

``generate_interview_questions(ctx, job_id, resume_id, claim_id)`` settles the
``interview_questions`` row the route claimed as ``pending``. It takes ids only
(no free text through Redis), rebuilds the entry through
``shortlist_service.get_one`` -- the blind-redacted, unscoped DTO, never raw
SQL on ``resumes.parsed`` -- and returns the status it recorded.

* A vanished entry (including a WITHDRAWN résumé, which ``get_one`` hides) is
  ``failed``/``entry_gone`` and the model is never called.
* S1's ``InterviewRefusal`` is ``refused`` with its reason; no model call.
* Candidate name/email/phone are decrypted ONLY to scrub partial quotes (a
  non-blind job's evidence quote can carry them) before the prompt is built and
  to drive ``filter_questions`` afterwards. They never enter a message.
* ``LLMUnavailableError`` raises ``arq.Retry`` until the final try (``job_try``
  reaching ``resume_parse_max_tries``), recording nothing; the final try settles
  ``failed``/``llm_unavailable``. A parent row deleted mid-run (FK violation) is
  a ``failed`` status, not a crash.
  Schema-invalid output is ``failed``/``invalid_output``; an empty or fully
  filtered result is ``failed``/``no_questions``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg
from arq import Retry

from src.errors import NotFoundError
from src.pipeline.llm import LLMClient, LLMOutputInvalidError, LLMUnavailableError
from src.services import interview_question_service as iq_service
from src.services import shortlist_service
from src.services.interview_question_service import FailureReason
from src.services.interview_questions import (
    InterviewRefusal,
    filter_questions,
    generate,
    redact_gaps,
    select_gaps,
)
from src.settings import get_settings

log = logging.getLogger(__name__)

_ENTRY_ID_SQL = "SELECT id FROM shortlist_entries WHERE job_id = $1 AND resume_id = $2"
_RETRY_DEFER_SECONDS = 30
_ROW_SQL = "SELECT 1 FROM interview_questions WHERE job_id = $1 AND resume_id = $2"


async def generate_interview_questions(
    ctx: dict[str, Any], job_id: str, resume_id: str, claim_id: str | None = None
) -> str:
    if claim_id is None:
        # a job queued before claims existed: nothing to settle against
        log.warning(
            "interview_questions job=%s resume=%s has no claim_id; skipped",
            job_id,
            resume_id,
        )
        return "failed"
    pool = ctx["pg_pool"]
    llm: LLMClient = ctx["llm"]
    job_uuid, resume_uuid = UUID(job_id), UUID(resume_id)
    claim_uuid = UUID(claim_id)

    async with pool.acquire() as conn:
        # Every try refreshes the claim's requested_at first, so a slow model
        # or a long retry chain is not mistaken for a dead worker. A False
        # means superseded: skip the model entirely.
        beat = await iq_service.heartbeat(
            conn, job_uuid, resume_uuid, claim_id=claim_uuid
        )
        if not beat:
            if await _row_gone(conn, job_uuid, resume_uuid):
                return "failed"
            _warn_if_superseded(False, job_id, resume_id)
            return "ready"  # what this run meant to record; nothing was written
        entry_id = await conn.fetchval(_ENTRY_ID_SQL, job_uuid, resume_uuid)
        if entry_id is None:
            return await _failed(
                conn, job_uuid, resume_uuid, claim_uuid, FailureReason.ENTRY_GONE
            )
        try:
            entry = await shortlist_service.get_one(conn, entry_id)
        except NotFoundError:
            return await _failed(
                conn, job_uuid, resume_uuid, claim_uuid, FailureReason.ENTRY_GONE
            )

        try:
            gaps = select_gaps(entry)
        except InterviewRefusal as refusal:
            try:
                written = await iq_service.record_refused(
                    conn, job_uuid, resume_uuid, refusal.reason, claim_id=claim_uuid
                )
            except asyncpg.ForeignKeyViolationError:
                return "failed"
            if not written and await _row_gone(conn, job_uuid, resume_uuid):
                return "failed"
            _warn_if_superseded(written, job_id, resume_id)
            return "refused"

        name, email, phone = await iq_service.load_identifiers(conn, resume_uuid)

    # The connection is released before the (slow) model call.
    scrubbed = redact_gaps(gaps, name=name, email=email, phone=phone)
    try:
        ingest = await generate(llm, scrubbed)
    except LLMUnavailableError as exc:
        job_try = ctx.get("job_try", 1)
        if job_try >= get_settings().resume_parse_max_tries:
            return await _settle_failed(
                pool,
                job_uuid,
                resume_uuid,
                claim_uuid,
                FailureReason.LLM_UNAVAILABLE.value,
            )
        raise Retry(defer=_RETRY_DEFER_SECONDS * job_try) from exc
    except LLMOutputInvalidError:
        return await _settle_failed(
            pool, job_uuid, resume_uuid, claim_uuid, "invalid_output"
        )

    kept, dropped = filter_questions(
        ingest, scrubbed, name=name, email=email, phone=phone
    )
    log.info(
        "interview_questions job=%s resume=%s kept=%d dropped=%d",
        job_id,
        resume_id,
        len(kept),
        dropped,
    )
    if not kept:
        return await _settle_failed(
            pool, job_uuid, resume_uuid, claim_uuid, "no_questions"
        )
    try:
        async with pool.acquire() as conn:
            written = await iq_service.record_ready(
                conn,
                job_uuid,
                resume_uuid,
                claim_id=claim_uuid,
                questions=kept,
                gaps=scrubbed,
                model=get_settings().llm_model_generation,
                blind_review=entry.blinded,
            )
            if not written and await _row_gone(conn, job_uuid, resume_uuid):
                return "failed"
    except asyncpg.ForeignKeyViolationError:
        return "failed"
    _warn_if_superseded(written, job_id, resume_id)
    return "ready"


async def _row_gone(conn: Any, job_id: UUID, resume_id: UUID) -> bool:
    """A no-op settle with NO row left means the parent job or résumé was
    deleted mid-run (the cascade took the row): that is a failed run, not a
    superseded one."""
    return await conn.fetchval(_ROW_SQL, job_id, resume_id) is None


def _warn_if_superseded(written: bool, job_id: str, resume_id: str) -> None:
    if not written:
        log.warning(
            "interview_questions job=%s resume=%s settle superseded: the claim "
            "is no longer the row's pending claim; nothing written",
            job_id,
            resume_id,
        )


async def _failed(
    conn: Any, job_id: UUID, resume_id: UUID, claim_id: UUID, reason: FailureReason
) -> str:
    try:
        written = await iq_service.record_failed(
            conn, job_id, resume_id, reason.value, claim_id=claim_id
        )
    except asyncpg.ForeignKeyViolationError:
        return "failed"
    _warn_if_superseded(written, str(job_id), str(resume_id))
    return "failed"


async def _settle_failed(
    pool: Any, job_id: UUID, resume_id: UUID, claim_id: UUID, reason: str
) -> str:
    try:
        async with pool.acquire() as conn:
            written = await iq_service.record_failed(
                conn, job_id, resume_id, reason, claim_id=claim_id
            )
    except asyncpg.ForeignKeyViolationError:
        return "failed"
    _warn_if_superseded(written, str(job_id), str(resume_id))
    return "failed"
