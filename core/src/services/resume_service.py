"""Résumé write-back — the worker's half of the résumé lifecycle.

Phase 3 ports ONLY the worker write-back trio from hris
``apps/api/src/api/services/resume_service.py``: ``encrypt_pii_via_session``,
``record_parsed`` and ``record_parse_failure``. Upload/validation
(``store_uploaded_blob``, ``detect_mime``, ``FileRejected``) is Phase 6 and the
read side (``list_for_job``, ``get_one``, ``reveal_identity``, ``_blind_parsed``)
is Phase 5 (redaction) — none of it is on the parse path.

``record_parsed`` carries the same optimistic-concurrency guard as the job
variant, widened to the two non-terminal résumé states: the UPDATE applies only
while the row is still ``status IN ('uploaded', 'parsing')``. A résumé that was
deleted, already parsed, or already failed under us yields 0 rows, we return
``False``, and the caller (``parse_resume``) returns ``"stale"`` WITHOUT
enqueueing an outbox row.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from uuid import UUID

import asyncpg
from asyncpg import Record

from src.schemas.resumes import CandidateInfo, CoverLetterParsed, ResumeParsed
from src.services import pii as pii_service

logger = logging.getLogger(__name__)

_MAX_REASON_CHARS = 1000

# (candidate_name_enc, candidate_email_enc, candidate_phone_enc, email_hash)
EncryptedPii = tuple[bytes | None, bytes | None, bytes | None, str | None]

_RECORD_PARSED_SQL = """
UPDATE resumes SET
    parsed = $2::jsonb,
    candidate_name = $3,
    candidate_email = $4,
    candidate_phone = $5,
    candidate_email_hash = $6,
    cover_letter_parsed = $7::jsonb,
    status = 'parsed',
    parsed_at = $8,
    failure_reason = NULL
WHERE id = $1 AND status IN ('uploaded', 'parsing')
"""

_RECORD_FAILURE_SQL = """
UPDATE resumes SET
    status = 'failed',
    failure_reason = $2
WHERE id = $1 AND status IN ('uploaded', 'parsing')
"""


async def encrypt_pii_via_session(
    conn: asyncpg.Connection[Record], candidate: CandidateInfo
) -> EncryptedPii:
    """Encrypt the candidate's PII and derive the lookup hash.

    ``set_pii_key`` runs EXACTLY ONCE and STRICTLY FIRST: ``set_config`` is
    transaction-scoped, so it has to land before any ``current_setting()`` read
    in the same transaction — every ``encrypt`` below is such a read. The caller
    MUST already be inside ``async with conn.transaction():``.

    The email hash is computed in Python and stays PLAINTEXT — it is the
    subject-access lookup key, not a secret.
    """
    await pii_service.set_pii_key(conn)
    return (
        await pii_service.encrypt(conn, candidate.name),
        await pii_service.encrypt(conn, candidate.email),
        await pii_service.encrypt(conn, candidate.phone),
        pii_service.email_hash(candidate.email),
    )


async def record_parsed(
    conn: asyncpg.Connection[Record],
    resume_id: UUID,
    parsed: ResumeParsed,
    pii: EncryptedPii,
    cover_letter_parsed: CoverLetterParsed | None,
    parsed_at: dt.datetime,
) -> bool:
    """Write the LLM extraction + the encrypted PII back onto the résumé row.

    ``parsed`` already carries ``chunks`` and ``cover_letter_chunks``;
    ``cover_letter_parsed`` is the separate cover-letter extraction, or ``None``
    when there is no cover letter (or its parse failed — a non-fatal path).

    Returns ``True`` when the UPDATE applied, ``False`` when the row was no
    longer in ('uploaded', 'parsing') — see the module docstring.
    """
    name_enc, email_enc, phone_enc, email_hash = pii
    result = await conn.execute(
        _RECORD_PARSED_SQL,
        resume_id,
        json.dumps(parsed.model_dump()),
        name_enc,
        email_enc,
        phone_enc,
        email_hash,
        json.dumps(cover_letter_parsed.model_dump()) if cover_letter_parsed else None,
        parsed_at,
    )
    applied = result.endswith(" 1")
    if not applied:
        logger.info("resume.record_parsed.stale resume_id=%s", resume_id)
    return applied


async def record_parse_failure(
    conn: asyncpg.Connection[Record], resume_id: UUID, reason: str
) -> None:
    """Mark the résumé failed and surface why. Terminal — no retry."""
    await conn.execute(_RECORD_FAILURE_SQL, resume_id, reason[:_MAX_REASON_CHARS])
    logger.warning(
        "resume.parse_failed resume_id=%s reason=%s", resume_id, reason[:200]
    )
