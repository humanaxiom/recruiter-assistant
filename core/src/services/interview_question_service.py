"""Persistence for generated interview questions (``interview_questions``).

One row per ``(job_id, resume_id)``. ``claim`` is the single writer of
``pending`` and is an atomic upsert, so two clicks (or two replicas) cannot
both enqueue work for the same candidate. The worker then settles the row with
exactly one of ``record_ready`` / ``record_failed`` / ``record_refused``.

``reason`` is a FIXED vocabulary -- ``RefusalReason`` for a refusal,
``FailureReason`` for a failure -- validated here before any SQL runs, so a
free-text reason (which could carry a name) cannot reach the table.
"""

from __future__ import annotations

import enum
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from src.services import DbConn, pii
from src.services.interview_questions import (
    PROMPT_VERSION,
    Gap,
    InterviewQuestionItem,
    RefusalReason,
    gaps_hash,
)
from src.settings import Settings, get_settings

# The worker's retry defer step (``interview_tasks._RETRY_DEFER_SECONDS``) and
# the margin added on top of the worst-case run time.
_RETRY_DEFER_STEP_S = 30
_STALL_MARGIN_MINUTES = 5


class FailureReason(enum.StrEnum):
    ENTRY_GONE = "entry_gone"
    INVALID_OUTPUT = "invalid_output"
    NO_QUESTIONS = "no_questions"
    LLM_UNAVAILABLE = "llm_unavailable"


# A pending row is claimable again once it is older than the stall window (the
# worker died or the job was lost), and a settled row is always claimable (a
# regenerate). A fresh pending row is not: the WHERE makes the upsert a no-op
# and ``RETURNING`` yields no row. Under concurrency the losers block on the
# row lock, re-evaluate the WHERE against the winner's pending row and lose.
_CLAIM_SQL = """
INSERT INTO interview_questions
    (job_id, resume_id, status, created_by, claim_id)
VALUES ($1, $2, 'pending', $3, gen_random_uuid())
ON CONFLICT (job_id, resume_id) DO UPDATE SET
    status = 'pending',
    reason = NULL,
    questions = '[]'::jsonb,
    gaps_sha256 = NULL,
    prompt_version = NULL,
    model = NULL,
    created_by = EXCLUDED.created_by,
    requested_at = now(),
    generated_at = NULL,
    claim_id = gen_random_uuid()
WHERE interview_questions.status <> 'pending'
   OR interview_questions.requested_at < now() - make_interval(mins => $4)
RETURNING claim_id
"""

_READY_SQL = """
UPDATE interview_questions SET
    status = 'ready',
    reason = NULL,
    questions = $4::jsonb,
    gaps_sha256 = $5,
    prompt_version = $6,
    model = $7,
    generated_at = now(),
    blind_review = $8
WHERE job_id = $1 AND resume_id = $2 AND claim_id = $3 AND status = 'pending'
"""

_SETTLE_SQL = """
UPDATE interview_questions SET
    status = $4,
    reason = $5,
    questions = '[]'::jsonb,
    gaps_sha256 = NULL,
    generated_at = NULL
WHERE job_id = $1 AND resume_id = $2 AND claim_id = $3 AND status = 'pending'
"""

_HEARTBEAT_SQL = """
UPDATE interview_questions SET requested_at = now()
WHERE job_id = $1 AND resume_id = $2 AND claim_id = $3 AND status = 'pending'
"""

_GET_SQL = """
SELECT status, reason, questions, gaps_sha256, prompt_version, model,
       generated_at, requested_at, created_by, blind_review
FROM interview_questions
WHERE job_id = $1 AND resume_id = $2
"""


@dataclass(frozen=True)
class InterviewQuestionsRecord:
    status: str
    reason: str | None
    questions: list[dict[str, Any]]
    gaps_sha256: str | None
    prompt_version: str | None
    model: str | None
    generated_at: datetime | None
    requested_at: datetime | None = None
    blind_review: bool = False


def effective_stall_minutes(settings: Settings | None = None) -> int:
    """The window after which a ``pending`` row counts as stalled.

    The configured knob, but never below what one honest run can take: every
    LLM attempt (``llm_max_retries + 1`` HTTP tries at ``llm_timeout_s``) plus
    the worker's largest retry defer, plus a 5 minute margin. The worker also
    heartbeats at the start of each try, so this bounds ONE try's silence."""
    s = settings if settings is not None else get_settings()
    per_try_defer_s = _RETRY_DEFER_STEP_S * (s.resume_parse_max_tries - 1)
    floor = (
        math.ceil(((s.llm_max_retries + 1) * s.llm_timeout_s + per_try_defer_s) / 60)
        + _STALL_MARGIN_MINUTES
    )
    return max(s.interview_questions_stall_minutes, floor)


async def claim(
    conn: DbConn, job_id: UUID, resume_id: UUID, created_by: str | None
) -> UUID | None:
    """Atomically mark ``(job_id, resume_id)`` pending and mint a fresh claim
    id. ``None`` when a fresh pending row already exists (the caller answers
    409 and enqueues nothing)."""
    claim_id = await conn.fetchval(
        _CLAIM_SQL, job_id, resume_id, created_by, effective_stall_minutes()
    )
    return None if claim_id is None else UUID(str(claim_id))


async def heartbeat(
    conn: DbConn, job_id: UUID, resume_id: UUID, *, claim_id: UUID
) -> bool:
    """Refresh ``requested_at`` of the live claim. ``False`` when superseded."""
    tag = await conn.execute(_HEARTBEAT_SQL, job_id, resume_id, claim_id)
    return bool(tag == "UPDATE 1")


async def record_ready(
    conn: DbConn,
    job_id: UUID,
    resume_id: UUID,
    *,
    claim_id: UUID,
    questions: Sequence[InterviewQuestionItem],
    gaps: Sequence[Gap],
    model: str,
    blind_review: bool = False,
) -> bool:
    """Store the questions with their requirement text, so a stored row stays
    readable after the shortlist's gaps move, stamped with the gap hash the
    reader compares against to show a stale banner. ``False`` (a no-op) when
    ``claim_id`` is no longer the row's pending claim."""
    requirement = {g.index: g.requirement for g in gaps}
    payload = json.dumps(
        [
            {
                "requirement_index": q.requirement_index,
                "requirement": requirement.get(q.requirement_index, ""),
                "question": q.question,
                "probes_quote": q.probes_quote,
            }
            for q in questions
        ],
        ensure_ascii=False,
    )
    tag = await conn.execute(
        _READY_SQL,
        job_id,
        resume_id,
        claim_id,
        payload,
        gaps_hash(gaps),
        PROMPT_VERSION,
        model,
        blind_review,
    )
    return bool(tag == "UPDATE 1")


async def record_failed(
    conn: DbConn, job_id: UUID, resume_id: UUID, reason: str, *, claim_id: UUID
) -> bool:
    """Settle as ``failed``. ``reason`` must be a ``FailureReason`` value."""
    checked = FailureReason(reason)  # ValueError before any SQL
    tag = await conn.execute(
        _SETTLE_SQL, job_id, resume_id, claim_id, "failed", checked.value
    )
    return bool(tag == "UPDATE 1")


async def record_refused(
    conn: DbConn,
    job_id: UUID,
    resume_id: UUID,
    reason: RefusalReason | str,
    *,
    claim_id: UUID,
) -> bool:
    """Settle as ``refused``. ``reason`` must be a ``RefusalReason`` value."""
    checked = RefusalReason(reason)  # ValueError before any SQL
    tag = await conn.execute(
        _SETTLE_SQL, job_id, resume_id, claim_id, "refused", checked.value
    )
    return bool(tag == "UPDATE 1")


async def get(
    conn: DbConn, job_id: UUID, resume_id: UUID
) -> InterviewQuestionsRecord | None:
    row = await conn.fetchrow(_GET_SQL, job_id, resume_id)
    if row is None:
        return None
    raw = row["questions"]
    decoded = json.loads(raw) if isinstance(raw, str | bytes) else raw
    return InterviewQuestionsRecord(
        status=row["status"],
        reason=row["reason"],
        questions=list(decoded) if decoded else [],
        gaps_sha256=row["gaps_sha256"],
        prompt_version=row["prompt_version"],
        model=row["model"],
        generated_at=row["generated_at"],
        requested_at=row["requested_at"],
        blind_review=bool(row["blind_review"]),
    )


_IDENTIFIERS_SQL = (
    "SELECT candidate_name, candidate_email, candidate_phone FROM resumes WHERE id = $1"
)


async def load_identifiers(
    conn: DbConn, resume_id: UUID
) -> tuple[str | None, str | None, str | None]:
    """The candidate's decrypted ``(name, email, phone)``, used ONLY to scrub
    quotes and requirement text and to filter questions. Never enters a prompt
    or a response. ``(None, None, None)`` when the résumé row is gone."""
    row = await conn.fetchrow(_IDENTIFIERS_SQL, resume_id)
    if row is None:
        return None, None, None
    async with conn.transaction():
        await pii.set_pii_key(conn)
        name = await pii.decrypt(conn, row["candidate_name"])
        email = await pii.decrypt(conn, row["candidate_email"])
        phone = await pii.decrypt(conn, row["candidate_phone"])
    return name, email, phone
