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
import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from src.errors import NotFoundError
from src.pipeline.parsing import (
    MIME_DOCX,
    MIME_PDF,
    MIME_RTF,
    MIME_TXT,
    UnsupportedMimeError,
)
from src.schemas.resumes import (
    CandidateInfo,
    CoverLetterParsed,
    ResumeListItem,
    ResumeOut,
    ResumeParsed,
    ResumeUploadResult,
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
from src.storage.blob_store import BlobStore

logger = logging.getLogger(__name__)

_MAX_REASON_CHARS = 1000

# Matches src/pipeline/parsing/extract.py's per-document cap — the upload
# boundary must not accept anything the extractor would reject anyway.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

_EXT_MIME: dict[str, str] = {
    "pdf": MIME_PDF,
    "docx": MIME_DOCX,
    "rtf": MIME_RTF,
    "txt": MIME_TXT,
}
_MIME_EXT: dict[str, str] = {v: k for k, v in _EXT_MIME.items()}

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


# ── upload / validation (Phase 6) ────────────────────────────────────────────


def detect_mime(filename: str, data: bytes) -> str:
    """Sniff the real mime type from MAGIC BYTES and cross-check it against
    the filename's extension. Raises ``UnsupportedMimeError`` (the SAME
    exception ``extract_text`` raises, so callers have one type to catch) on
    an unsupported extension OR a extension/content mismatch (a
    ``.pdf``-named file whose bytes are actually a DOCX zip)."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    expected = _EXT_MIME.get(ext)
    if expected is None:
        raise UnsupportedMimeError(f"unsupported file extension: {filename!r}")
    actual = _sniff_mime(data)
    if actual != expected:
        raise UnsupportedMimeError(
            f"file {filename!r} has extension {ext!r} but its content sniffs "
            f"as {actual!r}, not {expected!r}"
        )
    return expected


def _sniff_mime(data: bytes) -> str:
    if data.startswith(b"%PDF"):
        return MIME_PDF
    if data.startswith(b"PK\x03\x04"):
        return MIME_DOCX
    if data.lstrip().startswith(rb"{\rtf"):
        return MIME_RTF
    return MIME_TXT


_DEDUP_SQL = "SELECT id FROM resumes WHERE job_id = $1 AND sha256 = $2"

_INSERT_UPLOADED_SQL = """
INSERT INTO resumes (
    id, job_id, blob_key, original_filename, mime_type, file_size_bytes,
    sha256, consent_acknowledged, uploaded_by, cover_letter_text,
    cover_letter_blob_key
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""


async def upload_resumes(
    conn: DbConn,
    blob_store: BlobStore,
    *,
    job_id: UUID,
    files: list[tuple[str, bytes]],
    consent_acknowledged: bool,
    uploaded_by: str | None = None,
    cover_letter_file: tuple[str, bytes] | None = None,
    cover_letter_text: str | None = None,
) -> list[ResumeUploadResult]:
    """Validate + persist a batch of résumé files.

    ``consent_acknowledged=False`` raises ``ValueError`` immediately, before
    touching the blob store or the connection at all (one flag for the whole
    batch, not per file). Every other rejection (oversize, empty, unsupported
    mime, sha256 duplicate) yields a per-file ``ResumeUploadResult`` row and
    does not abort the rest of the batch.

    The blob key is ALWAYS server-generated (``resumes/{uuid4()}.{ext}``) —
    the same ``uuid4()`` also backs the inserted row's primary key, so a
    caller can enqueue the parse task without a second DB round trip. A
    pasted ``cover_letter_text`` is encrypted (via ``pii_service.set_pii_key``
    + ``pii_service.encrypt``, called through the ``pii_service`` module
    object so it stays monkeypatchable) inside the SAME transaction as the
    résumé row insert(s) — a failure there (e.g. an unset PII_KEY) propagates
    uncaught, never swallowed into a "rejected" row.
    """
    if not consent_acknowledged:
        raise ValueError(
            "consent_acknowledged must be true before any résumé upload I/O"
        )
    # A file-attached cover letter is stored as a blob (like the résumé itself)
    # and its key set on the row; the worker's `_parse_cover_letter` extracts +
    # LLM-parses it at parse time from `cover_letter_blob_key` — the same
    # deferred path a pasted `cover_letter_text` takes, and non-fatal to the
    # résumé parse. Validated + size-capped here so a bad cover letter fails
    # loud at upload rather than silently later. Symmetric with the résumé
    # files: reject an oversize or unsupported-mime cover letter.
    cover_blob_key: str | None = None
    if cover_letter_file is not None:
        cl_name, cl_bytes = cover_letter_file
        if len(cl_bytes) == 0:
            raise ValueError("cover letter file is empty")
        if len(cl_bytes) > _MAX_UPLOAD_BYTES:
            raise ValueError(
                f"cover letter file is {len(cl_bytes)} bytes; the cap is "
                f"{_MAX_UPLOAD_BYTES}"
            )
        try:
            cl_mime = detect_mime(cl_name, cl_bytes)
        except UnsupportedMimeError as exc:
            raise ValueError(f"unsupported cover letter file: {exc}") from exc
        cover_blob_key = f"cover_letters/{uuid4()}.{_MIME_EXT[cl_mime]}"
        await blob_store.put(cover_blob_key, cl_bytes, content_type=cl_mime)

    results: list[ResumeUploadResult] = []

    async with conn.transaction():
        encrypted_cover: bytes | None = None
        if cover_letter_text is not None:
            await pii_service.set_pii_key(conn)
            encrypted_cover = await pii_service.encrypt(conn, cover_letter_text)

        for filename, data in files:
            if len(data) == 0:
                results.append(
                    ResumeUploadResult(
                        original_filename=filename,
                        outcome="rejected",
                        reason="file is empty",
                    )
                )
                continue
            if len(data) > _MAX_UPLOAD_BYTES:
                results.append(
                    ResumeUploadResult(
                        original_filename=filename,
                        outcome="rejected",
                        reason=(
                            f"file is {len(data)} bytes; the cap is "
                            f"{_MAX_UPLOAD_BYTES}"
                        ),
                    )
                )
                continue

            try:
                mime = detect_mime(filename, data)
            except UnsupportedMimeError as exc:
                results.append(
                    ResumeUploadResult(
                        original_filename=filename,
                        outcome="rejected",
                        reason=str(exc),
                    )
                )
                continue

            sha = hashlib.sha256(data).hexdigest()
            dup_id = await conn.fetchval(_DEDUP_SQL, job_id, sha)
            if dup_id is not None:
                results.append(
                    ResumeUploadResult(
                        original_filename=filename,
                        outcome="duplicate",
                        resume_id=dup_id,
                    )
                )
                continue

            resume_id = uuid4()
            blob_key = f"resumes/{resume_id}.{_MIME_EXT[mime]}"
            await blob_store.put(blob_key, data, content_type=mime)
            await conn.execute(
                _INSERT_UPLOADED_SQL,
                resume_id,
                job_id,
                blob_key,
                filename,
                mime,
                len(data),
                sha,
                consent_acknowledged,
                uploaded_by,
                encrypted_cover,
                cover_blob_key,
            )
            results.append(
                ResumeUploadResult(
                    original_filename=filename,
                    outcome="accepted",
                    resume_id=resume_id,
                )
            )

    return results


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
