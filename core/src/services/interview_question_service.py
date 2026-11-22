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
from src.settings import get_settings


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
INSERT INTO interview_questions (job_id, resume_id, status, created_by)
VALUES ($1, $2, 'pending', $3)
ON CONFLICT (job_id, resume_id) DO UPDATE SET
    status = 'pending',
    reason = NULL,
    questions = '[]'::jsonb,
    gaps_sha256 = NULL,
    prompt_version = NULL,
    model = NULL,
    created_by = EXCLUDED.created_by,
    requested_at = now(),
    generated_at = NULL
WHERE interview_questions.status <> 'pending'
   OR interview_questions.requested_at < now() - make_interval(mins => $4)
RETURNING true
"""

_READY_SQL = """
INSERT INTO interview_questions
    (job_id, resume_id, status, questions, gaps_sha256, prompt_version, model,
     generated_at, blind_review)
VALUES ($1, $2, 'ready', $3::jsonb, $4, $5, $6, now(), $7)
ON CONFLICT (job_id, resume_id) DO UPDATE SET
    status = 'ready',
    reason = NULL,
    questions = EXCLUDED.questions,
    gaps_sha256 = EXCLUDED.gaps_sha256,
    prompt_version = EXCLUDED.prompt_version,
    model = EXCLUDED.model,
    generated_at = now(),
    blind_review = EXCLUDED.blind_review
"""

_SETTLE_SQL = """
INSERT INTO interview_questions (job_id, resume_id, status, reason)
VALUES ($1, $2, $3, $4)
ON CONFLICT (job_id, resume_id) DO UPDATE SET
    status = EXCLUDED.status,
    reason = EXCLUDED.reason,
    questions = '[]'::jsonb,
    gaps_sha256 = NULL,
    generated_at = NULL
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


async def claim(
    conn: DbConn, job_id: UUID, resume_id: UUID, created_by: str | None
) -> bool:
    """Atomically mark ``(job_id, resume_id)`` pending. ``False`` when a fresh
    pending row already exists (the caller answers 409 and enqueues nothing)."""
    stall = get_settings().interview_questions_stall_minutes
    won = await conn.fetchval(_CLAIM_SQL, job_id, resume_id, created_by, stall)
    return bool(won)


async def record_ready(
    conn: DbConn,
    job_id: UUID,
    resume_id: UUID,
    *,
    questions: Sequence[InterviewQuestionItem],
    gaps: Sequence[Gap],
    model: str,
    blind_review: bool = False,
) -> None:
    """Store the questions with their requirement text, so a stored row stays
    readable after the shortlist's gaps move, stamped with the gap hash the
    reader compares against to show a stale banner."""
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
    await conn.execute(
        _READY_SQL,
        job_id,
        resume_id,
        payload,
        gaps_hash(gaps),
        PROMPT_VERSION,
        model,
        blind_review,
    )


async def record_failed(
    conn: DbConn, job_id: UUID, resume_id: UUID, reason: str
) -> None:
    """Settle as ``failed``. ``reason`` must be a ``FailureReason`` value."""
    checked = FailureReason(reason)  # ValueError before any SQL
    await conn.execute(_SETTLE_SQL, job_id, resume_id, "failed", checked.value)


async def record_refused(
    conn: DbConn, job_id: UUID, resume_id: UUID, reason: RefusalReason | str
) -> None:
    """Settle as ``refused``. ``reason`` must be a ``RefusalReason`` value."""
    checked = RefusalReason(reason)  # ValueError before any SQL
    await conn.execute(_SETTLE_SQL, job_id, resume_id, "refused", checked.value)


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
