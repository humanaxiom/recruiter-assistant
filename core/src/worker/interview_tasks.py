"""arq task for the interview-question generator (S2).

``generate_interview_questions(ctx, job_id, resume_id)`` settles the
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
* ``LLMUnavailableError`` propagates so arq retries; nothing is recorded.
  Schema-invalid output is ``failed``/``invalid_output``; an empty or fully
  filtered result is ``failed``/``no_questions``.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from src.errors import NotFoundError
from src.pipeline.llm import LLMClient, LLMOutputInvalidError
from src.services import interview_question_service as iq_service
from src.services import pii, shortlist_service
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
_IDENTIFIERS_SQL = (
    "SELECT candidate_name, candidate_email, candidate_phone FROM resumes WHERE id = $1"
)


async def generate_interview_questions(
    ctx: dict[str, Any], job_id: str, resume_id: str
) -> str:
    pool = ctx["pg_pool"]
    llm: LLMClient = ctx["llm"]
    job_uuid, resume_uuid = UUID(job_id), UUID(resume_id)

    async with pool.acquire() as conn:
        entry_id = await conn.fetchval(_ENTRY_ID_SQL, job_uuid, resume_uuid)
        if entry_id is None:
            return await _failed(conn, job_uuid, resume_uuid, FailureReason.ENTRY_GONE)
        try:
            entry = await shortlist_service.get_one(conn, entry_id)
        except NotFoundError:
            return await _failed(conn, job_uuid, resume_uuid, FailureReason.ENTRY_GONE)

        try:
            gaps = select_gaps(entry)
        except InterviewRefusal as refusal:
            await iq_service.record_refused(conn, job_uuid, resume_uuid, refusal.reason)
            return "refused"

        row = await conn.fetchrow(_IDENTIFIERS_SQL, resume_uuid)
        name = email = phone = None
        if row is not None:
            async with conn.transaction():
                await pii.set_pii_key(conn)
                name = await pii.decrypt(conn, row["candidate_name"])
                email = await pii.decrypt(conn, row["candidate_email"])
                phone = await pii.decrypt(conn, row["candidate_phone"])

    # The connection is released before the (slow) model call.
    scrubbed = redact_gaps(gaps, name=name, email=email, phone=phone)
    try:
        ingest = await generate(llm, scrubbed)
    except LLMOutputInvalidError:
        return await _settle_failed(pool, job_uuid, resume_uuid, "invalid_output")

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
        return await _settle_failed(pool, job_uuid, resume_uuid, "no_questions")
    async with pool.acquire() as conn:
        await iq_service.record_ready(
            conn,
            job_uuid,
            resume_uuid,
            questions=kept,
            gaps=scrubbed,
            model=get_settings().llm_model_generation,
        )
    return "ready"


async def _failed(
    conn: Any, job_id: UUID, resume_id: UUID, reason: FailureReason
) -> str:
    await iq_service.record_failed(conn, job_id, resume_id, reason.value)
    return "failed"


async def _settle_failed(pool: Any, job_id: UUID, resume_id: UUID, reason: str) -> str:
    async with pool.acquire() as conn:
        await iq_service.record_failed(conn, job_id, resume_id, reason)
    return "failed"
