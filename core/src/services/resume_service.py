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
from typing import Any
from uuid import UUID

from src.errors import NotFoundError
from src.schemas.resumes import (
    CandidateInfo,
    CoverLetterParsed,
    ResumeListItem,
    ResumeOut,
    ResumeParsed,
)
from src.services import DbConn
from src.services import pii as pii_service
from src.services.pii import set_pii_key
from src.services.redaction import (
    blind_label_map,
    is_foreign_location,
    redact_text,
    redacted_filename,
)

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
    conn: DbConn, candidate: CandidateInfo
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
    conn: DbConn,
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


async def record_parse_failure(conn: DbConn, resume_id: UUID, reason: str) -> None:
    """Mark the résumé failed and surface why. Terminal — no retry."""
    await conn.execute(_RECORD_FAILURE_SQL, resume_id, reason[:_MAX_REASON_CHARS])
    logger.warning(
        "resume.parse_failed resume_id=%s reason=%s", resume_id, reason[:200]
    )


# ── read (Phase 5) ─────────────────────────────────────────────────────────

_LIST_SQL = (
    "SELECT id, original_filename, status, uploaded_at, parsed_at, "
    "pgp_sym_decrypt(candidate_name, current_setting('app.pii_key')) "
    "AS candidate_name, "
    # Feature 2: surface whether a cover letter is attached (blob or pasted
    # text) so the list can badge it. Cheap boolean, no PII.
    "(cover_letter_blob_key IS NOT NULL OR cover_letter_text IS NOT NULL) "
    "AS has_cover_letter "
    "FROM resumes WHERE job_id = $1 "
    "ORDER BY uploaded_at DESC LIMIT $2 OFFSET $3"
)

_GET_SQL = """
SELECT id, job_id, original_filename, mime_type, file_size_bytes, sha256,
    pgp_sym_decrypt(candidate_name, current_setting('app.pii_key')) AS c_name,
    pgp_sym_decrypt(candidate_email, current_setting('app.pii_key')) AS c_email,
    pgp_sym_decrypt(candidate_phone, current_setting('app.pii_key')) AS c_phone,
    pgp_sym_decrypt(cover_letter_text, current_setting('app.pii_key')) AS cl_text,
    cover_letter_parsed,
    candidate_email_hash, parsed, status, uploaded_by, uploaded_at,
    parsed_at, failure_reason, consent_acknowledged
FROM resumes WHERE id = $1
"""

_BLIND_CHECK_SQL = (
    "SELECT j.blind_review FROM jobs j "
    "JOIN resumes r ON r.job_id = j.id WHERE r.id = $1"
)


async def list_for_job(
    conn: DbConn, *, job_id: UUID, limit: int = 100, offset: int = 0
) -> list[ResumeListItem]:
    """List résumés for a job. Under blind review the candidate name is masked
    to ``None`` — NOT a pseudonym: a résumé list has no rank to build one from
    (unlike the shortlist, which always carries a rank)."""
    blind = await conn.fetchval("SELECT blind_review FROM jobs WHERE id = $1", job_id)
    async with conn.transaction():
        await set_pii_key(conn)
        rows = await conn.fetch(_LIST_SQL, job_id, limit, offset)
    return [
        ResumeListItem(
            id=r["id"],
            # A résumé named after its candidate leaks identity — mask the
            # filename under blind review, keeping only its extension.
            original_filename=(
                redacted_filename(r["original_filename"])
                if blind
                else r["original_filename"]
            ),
            status=r["status"],
            uploaded_at=r["uploaded_at"],
            parsed_at=r["parsed_at"],
            candidate_name=None if blind else r["candidate_name"],
            has_cover_letter=r["has_cover_letter"],
        )
        for r in rows
    ]


async def get_one(conn: DbConn, resume_id: UUID, *, reveal: bool = False) -> ResumeOut:
    """Fetch + decrypt one résumé. Opens its own transaction and sets the PII
    key. ``reveal=True`` bypasses blind-review masking (the audited un-blind
    path). Under blind review, identity is redacted BEFORE the DTO is built
    (ADR-006 §4) — no decrypted PII ever reaches ``ResumeOut``."""
    async with conn.transaction():
        await set_pii_key(conn)
        row = await conn.fetchrow(_GET_SQL, resume_id)
    if row is None:
        raise NotFoundError(f"resume {resume_id} not found", resume_id=str(resume_id))

    parsed: ResumeParsed | None = None
    raw_parsed = row["parsed"]
    if raw_parsed is not None:
        if isinstance(raw_parsed, str):
            raw_parsed = json.loads(raw_parsed)
        parsed = ResumeParsed.model_validate(raw_parsed)

    cover_parsed = _parse_cover_letter(row["cover_letter_parsed"])

    blind = (not reveal) and await conn.fetchval(_BLIND_CHECK_SQL, resume_id)
    if blind:
        location = parsed.candidate.location if parsed else None
        shown_location = None if is_foreign_location(location) else location
        return ResumeOut(
            id=row["id"],
            job_id=row["job_id"],
            # A résumé named after its candidate ("Jane_Smith_Resume.pdf")
            # leaks identity verbatim — mask it, keeping only the extension.
            original_filename=redacted_filename(row["original_filename"]),
            mime_type=row["mime_type"],
            file_size_bytes=row["file_size_bytes"],
            sha256=row["sha256"],
            candidate=CandidateInfo(
                name=None, email=None, phone=None, location=shown_location
            ),
            candidate_email_hash=row["candidate_email_hash"],
            parsed=_blind_parsed(parsed, row["c_name"], row["c_email"], row["c_phone"]),
            status=row["status"],
            uploaded_by=row["uploaded_by"],
            uploaded_at=row["uploaded_at"],
            parsed_at=row["parsed_at"],
            failure_reason=row["failure_reason"],
            consent_acknowledged=row["consent_acknowledged"],
            blinded=True,
            # The cover letter carries PII (name/address) — withhold it under
            # blind review (an audited reveal surfaces it, like the name).
            cover_letter_text=None,
            cover_letter_parsed=None,
        )

    return ResumeOut(
        id=row["id"],
        job_id=row["job_id"],
        original_filename=row["original_filename"],
        mime_type=row["mime_type"],
        file_size_bytes=row["file_size_bytes"],
        sha256=row["sha256"],
        candidate=CandidateInfo(
            name=row["c_name"],
            email=row["c_email"],
            phone=row["c_phone"],
            location=parsed.candidate.location if parsed else None,
        ),
        candidate_email_hash=row["candidate_email_hash"],
        parsed=parsed,
        status=row["status"],
        uploaded_by=row["uploaded_by"],
        uploaded_at=row["uploaded_at"],
        parsed_at=row["parsed_at"],
        failure_reason=row["failure_reason"],
        consent_acknowledged=row["consent_acknowledged"],
        cover_letter_text=row["cl_text"],
        cover_letter_parsed=cover_parsed,
    )


def _parse_cover_letter(raw: Any) -> CoverLetterParsed | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = json.loads(raw)
    return CoverLetterParsed.model_validate(raw)


def _blind_parsed(
    parsed: ResumeParsed | None,
    name: str | None,
    email: str | None,
    phone: str | None,
) -> ResumeParsed | None:
    """Mask the candidate for blind review: scrub name/contact from the text
    (summary, chunk text, experience bullets), replace employer + school names
    with stable labels in both the structured fields and the prose, drop
    graduation years, and mask **foreign** locations. Job titles, degree, field,
    employment dates, skills, and Canadian locations are left intact."""
    if parsed is None:
        return None

    labels = blind_label_map(
        employers=[e.company for e in parsed.experience],
        institutions=[ed.institution for ed in parsed.education],
    )
    location = parsed.candidate.location
    shown_location = None if is_foreign_location(location) else location

    def _r(text: str) -> str:
        return redact_text(
            text,
            name=name,
            email=email,
            phone=phone,
            term_map=labels,
            location=location,
            redact_locations=True,
        )

    return parsed.model_copy(
        update={
            "candidate": CandidateInfo(
                name=None, email=None, phone=None, location=shown_location
            ),
            "summary": _r(parsed.summary),
            "chunks": [
                c.model_copy(update={"text": _r(c.text)}) for c in parsed.chunks
            ],
            # Cover-letter chunk text carries the candidate's OWN letterhead
            # (name/email/phone in the first ~200 chars) — redact it exactly
            # like the résumé chunks so blind ResumeOut can't leak identity.
            "cover_letter_chunks": [
                c.model_copy(update={"text": _r(c.text)})
                for c in parsed.cover_letter_chunks
            ],
            "experience": [
                e.model_copy(
                    update={
                        "company": labels.get(e.company.strip(), e.company),
                        "bullets": [
                            b.model_copy(update={"text": _r(b.text)}) for b in e.bullets
                        ],
                    }
                )
                for e in parsed.experience
            ],
            "education": [
                ed.model_copy(
                    update={
                        "institution": labels.get(
                            ed.institution.strip(), ed.institution
                        ),
                        "year": None,  # graduation year is an age proxy
                    }
                )
                for ed in parsed.education
            ],
        }
    )
