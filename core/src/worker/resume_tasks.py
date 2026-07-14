"""arq tasks for the résumé pipeline.

``parse_resume(ctx, resume_id)``::

    blob    <- BlobStore.get(blob_key)
    text    <- extract_text(blob, mime)
    chunks  <- chunk_resume(text)                     # c_NNN
    core    <- LLM(resume_core_v1  -> ResumeCore)     # narrative half
    skills  <- vocab scan MERGED with LLM(resume_skills_v2)
    cover   <- LLM(cover_letter_v1 -> CoverLetterParsed)   # cl_NNN, non-fatal
    embed   <- summary text (PII-FREE) + every chunk
    UPDATE resumes ... status='parsed' + INSERT outbox('resume.parsed')  [one tx]

Ported from hris ``apps/worker/src/worker/resume_tasks.py``
(``phase3-source-dossier.md`` §7). Four deliberate deviations:

* **MinIO is gone.** hris's ``_fetch_blob(minio, bucket, key)`` +
  ``asyncio.to_thread`` wrapper is deleted outright: ``BlobStore.get(key)`` is
  already a coroutine and takes a single relative key (no bucket). ``S3Error``
  becomes ``(BlobNotFound, InvalidBlobKey)``.
* **``ctx["pg_pool"]``, not ``ctx["pool"]``** — the key ``src/worker/main.py``
  actually sets. A verbatim copy-paste of the hris body ``KeyError``s here.
* **The cover-letter paste branch decrypts through ``pii.decrypt``** rather
  than re-SELECTing ``cover_letter_text`` with an inline ``pgp_sym_decrypt``:
  the ciphertext is already in the ``meta`` row we fetched, so the extra round
  trip bought nothing. ``set_pii_key`` still runs first, in the same
  transaction — the GUC is transaction-scoped.
* **Graph projection is Phase 4.** ``project_resume`` /
  ``_resume_projection_tx`` / ``project_to_graph`` are not ported; this task
  stops at the outbox row.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from collections import Counter
from typing import Any
from uuid import UUID

from asyncpg import Record
from neo4j import AsyncDriver
from pydantic import ValidationError

from src.pipeline import skills_graph
from src.pipeline.llm import (
    CachedEmbedder,
    LLMClient,
    LLMOutputInvalidError,
    validation_error_digest,
)
from src.pipeline.parsing import (
    MIME_DOCX,
    MIME_PDF,
    MIME_RTF,
    MIME_TXT,
    EncryptedPdfError,
    InputTooLargeError,
    UnsupportedMimeError,
    chunk_resume,
    extract_text,
)
from src.pipeline.skills import canonicalize_skill_names, match_skills_in_text
from src.prompts import load_prompt
from src.schemas import (
    CandidateInfo,
    CoverLetterParsed,
    ResumeChunk,
    ResumeCore,
    ResumeParsed,
    ResumeSkill,
    ResumeSkillDetail,
    ResumeSkillDetails,
)
from src.services import DbConn, outbox_service, pii, resume_service
from src.storage.blob_store import BlobNotFound, BlobStore, InvalidBlobKey

log = logging.getLogger(__name__)

# ResumeParsed.skills is capped at 80; cap the merge at the same number so the
# model_validate below never has to silently truncate.
_MAX_SKILLS = 80

# Embed in batches instead of one giant POST. A 200-chunk résumé in a single
# request is a multi-MB body that Ollama has to hold whole, and one slow/failed
# request loses the entire document's embeddings; 64 keeps each call bounded.
_EMBED_BATCH_SIZE = 64

_RESUME_META_SQL = (
    "SELECT blob_key, mime_type, status, job_id, "
    "cover_letter_blob_key, cover_letter_text FROM resumes WHERE id = $1"
)

# A résumé is parseable only from these two states; anything else means another
# worker (or a delete) got there first.
_PARSEABLE_STATUSES = ("uploaded", "parsing")

# ADR-007 §7 / decision 1 (EXTRACTION_PLAN 4b row): the outbox NEVER carries
# candidate identity or raw chunk text — Neo4j gets no chunk text, ever, not
# even a 200-char preview (a résumé header chunk's first 200 chars ARE the
# candidate's name/email/phone). `summary` is dropped too (F2, round 3): a
# small model sometimes opens it with the candidate's own name. A single
# named constant so `parse_resume`'s enqueue call and every test/fixture that
# needs to know "what does the outbox actually carry" share ONE source of
# truth instead of hand-duplicating the exclude clause (the drift class the
# 4a corpus's audit rounds kept finding).
_OUTBOX_PARSED_EXCLUDE: dict[str, Any] = {
    "candidate": True,
    "summary": True,
    "chunks": {"__all__": {"text"}},
    "cover_letter_chunks": {"__all__": {"text"}},
}


# ---------------- skills ----------------


async def _extract_skills_merged(
    llm: LLMClient, chunks: list[ResumeChunk], resume_id_str: str
) -> list[ResumeSkill]:
    """Résumé skills = a deterministic vocabulary scan (reliable, never fails)
    MERGED with a best-effort skills LLM call that also yields optional
    per-skill ``years`` / ``last_used_year``.

    Never raises: the LLM half is non-fatal (the lenient ``ResumeSkillDetails``
    drops malformed/looping output), so the deterministic scan is the floor and
    a résumé still parses with names-only when the model fails. Returns
    canonical, deduped rows capped at the ``ResumeParsed.skills`` limit;
    years/last_used_year are carried only where the LLM stated them.

    F3b (security re-audit round 2): ``skills_graph.reject_reason_for_skill_name``
    runs HERE, on each LLM detail's RAW ``d.name``, BEFORE it is ever handed to
    ``canonicalize_skill_names`` below. ``canonicalize_skill_names`` (and its
    ``_basic_normalise``) strips ``@`` and lowercases the string, which is
    exactly what let an email-shaped "skill" name sail past
    ``skills_graph._resolve_one``'s email check in round 1 — that check runs
    downstream, in Phase 4b's graph projection, on the ALREADY-canonicalised
    name, where the ``@`` (and the capitalisation the new person-name-shape
    check needs) is long gone. Checking the raw name here closes that gap; the
    deterministic scan (``det``, below) is never checked — it can only ever
    contain a vocabulary TERM found verbatim in the text, never LLM free text.
    """
    det = match_skills_in_text("\n".join(c.text for c in chunks))
    llm_details: list[ResumeSkillDetail] = []
    try:
        prompt = load_prompt("resume_skills_v2", chunks=chunks)
        out = await llm.chat_json(
            prompt.messages, ResumeSkillDetails, max_tokens=1536, max_retries=1
        )
        llm_details = out.skills
    except LLMOutputInvalidError as exc:
        log.warning(
            "parse_resume.skills_llm_failed resume_id=%s error=%s", resume_id_str, exc
        )

    kept_details: list[ResumeSkillDetail] = []
    pii_shaped_dropped = 0
    for d in llm_details:
        if skills_graph.reject_reason_for_skill_name(d.name) is not None:
            pii_shaped_dropped += 1
            continue
        kept_details.append(d)
    if pii_shaped_dropped:
        # R3 discipline: count only, never the name(s).
        log.warning(
            "parse_resume.skill_name_pii_shaped_rejected resume_id=%s count=%d",
            resume_id_str,
            pii_shaped_dropped,
        )
    llm_details = kept_details

    # Years/last_used_year keyed by CANONICAL name (the first stated value wins,
    # then fill any gap from a later duplicate) so they survive the dedupe below.
    detail_by_canonical: dict[str, ResumeSkill] = {}
    for d in llm_details:
        canon = canonicalize_skill_names([d.name])
        if not canon:
            continue
        c = canon[0]
        prev = detail_by_canonical.get(c)
        if prev is None:
            detail_by_canonical[c] = ResumeSkill(
                name=c, years=d.years, last_used_year=d.last_used_year
            )
        else:
            detail_by_canonical[c] = ResumeSkill(
                name=c,
                years=prev.years if prev.years is not None else d.years,
                last_used_year=(
                    prev.last_used_year
                    if prev.last_used_year is not None
                    else d.last_used_year
                ),
            )

    # ``dict.fromkeys`` de-dupes while preserving first-seen order. The real
    # ``canonicalize_skill_names`` already de-dupes, so this is a no-op against
    # it — but the dedupe is a postcondition THIS function promises (see the
    # docstring), and it must not outsource it: a duplicate canonical name
    # survives into ``ResumeParsed.skills`` and becomes two HAS_SKILL edges for
    # the same skill when Phase 4 projects the résumé. Cap AFTER the dedupe, so
    # the limit counts 80 DISTINCT skills rather than 80 rows.
    canonical = canonicalize_skill_names([*det, *[d.name for d in llm_details]])
    ordered = list(dict.fromkeys(canonical))[:_MAX_SKILLS]
    skills = [detail_by_canonical.get(c, ResumeSkill(name=c)) for c in ordered]
    return _drop_smeared_years(skills, resume_id_str)


# A small local instruct model often ignores the "don't copy a career total onto
# every skill" instruction and stamps the SAME year-count on many skills (e.g.
# "25" on 40 skills, from "over 25 years of software development"). A résumé
# almost never legitimately states the identical duration for this many DISTINCT
# skills, so that pattern is a copied total, not per-skill data — a
# deterministic backstop the prompt can't reliably enforce. When >= this many
# skills share one exact years value, drop years on those (treat as unknown).
_SMEARED_YEARS_MIN = 6


def _drop_smeared_years(
    skills: list[ResumeSkill], resume_id_str: str
) -> list[ResumeSkill]:
    counts = Counter(s.years for s in skills if s.years is not None)
    smeared = {years for years, n in counts.items() if n >= _SMEARED_YEARS_MIN}
    if not smeared:
        return skills
    log.info(
        "parse_resume.skills_years_smeared_dropped resume_id=%s values=%s",
        resume_id_str,
        sorted(smeared),
    )
    return [
        s.model_copy(update={"years": None}) if s.years in smeared else s
        for s in skills
    ]


# ---------------- skill-name PII scrub (F3, security re-audit, layer 2) ----
#
# `src.pipeline.skills_graph._resolve_one` (layer 1) shape-rejects a skill
# name that LOOKS like contact information — but it has no idea what the
# CANDIDATE'S OWN name/email/phone actually is, so a skill name that IS the
# candidate's identity verbatim (e.g. "Casey Rivera", emitted by a small
# model off a header-shaped chunk) sails through layer 1 unshaped: it is
# short, has no "@", and isn't phone-digit-heavy. This function runs HERE,
# at parse time, where `CandidateInfo` is still in scope (the outbox payload
# has it stripped by design — decision 1/N1), and scrubs every skill name
# with the SAME `_redact_candidate_pii` used for the embedder's input, BEFORE
# `ResumeParsed.model_validate` — so the scrub lands in `resumes.parsed`
# (Postgres, system of record) and the outbox alike, not just the embed
# call.


def _redact_skill_names_pii(
    skills: list[ResumeSkill], candidate: CandidateInfo, resume_id_str: str
) -> list[ResumeSkill]:
    """Scrub the candidate's own identity out of every skill NAME. A name
    that reduces to blank after scrubbing (i.e. WAS the candidate's identity,
    verbatim) is dropped outright rather than kept as an empty string.

    R3 discipline: the drop is logged as a COUNT only — never the name.

    S12 (round-5 security re-audit): ``_redact_candidate_pii`` above is a
    STRUCTURED, pattern-based scrub — it needs at least one of
    ``candidate``'s own fields to build a pattern from. When
    ``_redaction_available(candidate)`` is False (a completely empty
    ``CandidateInfo`` — the ``core`` LLM call produced nothing usable), that
    scrub has nothing to match against and falls through to
    ``_generic_contact_scrub``, which by its own docstring cannot catch a
    bare NAME. Left unhandled, a name-shaped "skill" the LLM copied off a
    header block (e.g. an uncommon full name the offline personal-name
    lexicon does not cover, so ``skills_graph``'s NORMAL three-way
    conjunction lets it through too) would reach ``resumes.parsed``/the
    outbox verbatim — the candidate's OWN identity, landing as a ``:Skill``
    node in Neo4j.

    Fail CLOSED here exactly as ``parse_resume``'s header-chunk skip (S7)
    already does for the embedder: with no candidate identity at all to
    reason about, drop ANY skill name that is name-SHAPED and misses the
    220-term vocabulary, unconditionally — ``reject_reason_for_skill_name(
    ..., strict_lexicon=True)`` collapses Decision C's three-way conjunction
    back to the older, stricter two-way rule for exactly this reason (see
    that function's docstring). Privacy wins when we have no identity
    context to reason with; a real, uncommon-but-legitimate skill name lost
    this way is the accepted cost, same class as S7's header-chunk skip.
    """
    strict = not _redaction_available(candidate)
    out: list[ResumeSkill] = []
    dropped = 0
    for skill in skills:
        if strict and (
            skills_graph.reject_reason_for_skill_name(skill.name, strict_lexicon=True)
            is not None
        ):
            dropped += 1
            continue
        redacted_name = _redact_candidate_pii(skill.name, candidate)
        if not redacted_name:
            dropped += 1
            continue
        if redacted_name != skill.name:
            out.append(skill.model_copy(update={"name": redacted_name}))
        else:
            out.append(skill)
    if dropped:
        log.warning(
            "parse_resume.skill_name_pii_dropped resume_id=%s count=%d",
            resume_id_str,
            dropped,
        )
    return out


# ---------------- cover letter ----------------

_COVER_MIME_BY_EXT = {
    "pdf": MIME_PDF,
    "docx": MIME_DOCX,
    "rtf": MIME_RTF,
    "txt": MIME_TXT,
}


async def _parse_cover_letter(
    conn: DbConn,
    llm: LLMClient,
    blob_store: BlobStore,
    meta: Record,
    resume_id: UUID,
) -> tuple[list[ResumeChunk], CoverLetterParsed | None]:
    """Extract + chunk (``cl_NNN``) + LLM-parse a résumé's cover letter, if any.

    NON-FATAL by design: a résumé's parse must never fail because its cover
    letter's extraction or LLM call did. Any extraction failure returns
    ``([], None)``; an LLM failure returns ``(cl_chunks, CoverLetterParsed())``
    — the chunks are kept (Phase 4's evidence stage can still cite them), the
    extraction is just empty.

    The text comes from either the blob (file upload) or the pgp-encrypted
    ``cover_letter_text`` column (paste), decrypted under the session PII key.
    """
    blob_key = meta["cover_letter_blob_key"]
    has_enc_text = meta["cover_letter_text"] is not None
    if not blob_key and not has_enc_text:
        return [], None

    try:
        if blob_key:
            cover_blob = await blob_store.get(blob_key)
            ext = blob_key.rsplit(".", 1)[-1].lower()
            extracted = await asyncio.to_thread(
                extract_text, cover_blob, _COVER_MIME_BY_EXT.get(ext, MIME_PDF)
            )
        else:
            # SET LOCAL app.pii_key is transaction-scoped, so the key must land
            # before the decrypt reads current_setting() — same transaction,
            # set_pii_key strictly first.
            async with conn.transaction():
                await pii.set_pii_key(conn)
                plain = await pii.decrypt(conn, meta["cover_letter_text"])
            extracted = await asyncio.to_thread(
                extract_text, (plain or "").encode(), MIME_TXT
            )
        cl_chunks = await asyncio.to_thread(chunk_resume, extracted, prefix="cl")
    except (
        UnsupportedMimeError,
        EncryptedPdfError,
        InputTooLargeError,
        BlobNotFound,
        InvalidBlobKey,
    ) as exc:
        log.warning(
            "parse_resume.cover_extract_failed resume_id=%s error=%s", resume_id, exc
        )
        return [], None

    if not cl_chunks:
        return [], None

    try:
        prompt = load_prompt("cover_letter_v1", chunks=cl_chunks)
        cl_parsed = await llm.chat_json(
            prompt.messages, CoverLetterParsed, max_tokens=1024, max_retries=1
        )
    except LLMOutputInvalidError as exc:
        log.warning(
            "parse_resume.cover_llm_failed resume_id=%s error=%s", resume_id, exc
        )
        return cl_chunks, CoverLetterParsed()

    return cl_chunks, cl_parsed


# ---------------- embeddings ----------------


def _whitespace_flexible_pattern(identifier: str) -> str | None:
    """Build a case-insensitive regex that matches ``identifier`` allowing ANY
    run of whitespace (space/tab/newline, any count) wherever the normalized
    identifier has whitespace. Split on ``\\s+`` into tokens, ``re.escape`` each
    (candidate values are untrusted LLM output — no ReDoS/injection), and join
    with ``r"\\s+"``. A single-token identifier degrades to a plain
    ``re.escape`` literal — no regression. Returns ``None`` for an all-whitespace
    identifier (nothing to match).

    Tokens stay ORDER- and BOUNDARY-bound: only whitespace between the
    candidate's OWN tokens is tolerated, so an unrelated name sharing one token
    ("Jane Smith" vs the candidate's "Jane Doe") is never connected.
    """
    tokens = [re.escape(tok) for tok in identifier.split()]
    if not tokens:
        return None
    return r"\s+".join(tokens)


def _redaction_available(candidate: CandidateInfo) -> bool:
    """S7 (security re-audit round 3): the STRUCTURED scrub below
    (``_redact_candidate_pii``'s per-candidate patterns) has something to
    remove only when at least one candidate identifier is actually present.
    An EMPTY ``CandidateInfo`` (every field ``None`` — the LLM's ``core``
    call produced nothing usable) is NOT "nothing to redact"; it is
    "redaction unavailable", and the caller (``parse_resume``) must fail
    CLOSED on that distinction rather than embedding the raw text verbatim.
    See ``_redact_candidate_pii``'s docstring and ``parse_resume``'s
    header-chunk handling for the two-part fix this backs."""
    return bool(
        candidate.name or candidate.email or candidate.phone or candidate.location
    )


# S7 (security re-audit round 3): a GENERIC, structure-only email/phone scrub
# that runs UNCONDITIONALLY — regardless of whether `CandidateInfo` carries
# any value at all. `_redact_candidate_pii` previously returned its input
# VERBATIM whenever `candidate` was empty (`patterns` stayed `[]`), which is
# a fail-OPEN: "I have no candidate context" silently became "there is
# nothing to redact", and a résumé header chunk's raw contact block sailed
# straight into the embedder. This cannot catch a bare NAME (there is no
# reliable shape for an arbitrary name — see `parse_resume`'s header-chunk
# skip for how that specific, highest-risk gap is closed instead), but it
# closes the email/phone half of the gap unconditionally, with no candidate
# context required at all.
_GENERIC_EMAIL_RE = re.compile(
    r"[^\s@]+\s*(?:@|\(at\)|\[at\])\s*[^\s@]+\.[^\s@]+", re.IGNORECASE
)
_GENERIC_PHONE_RUN_RE = re.compile(r"[+()\-.\s\d]{7,}")


def _generic_contact_scrub(text: str) -> str:
    """Structure-only (candidate-free) email/phone scrub — see the module
    comment above for why this must run even when ``CandidateInfo`` is
    completely empty. A no-op input is returned truly verbatim (whitespace
    is only collapsed when a redaction actually fired), matching
    ``_redact_candidate_pii``'s existing discipline."""
    out = _GENERIC_EMAIL_RE.sub(" ", text)

    def _redact_phone_run(m: re.Match[str]) -> str:
        run = m.group()
        digits = sum(ch.isdigit() for ch in run)
        return " " if digits >= 7 else run

    out = _GENERIC_PHONE_RUN_RE.sub(_redact_phone_run, out)
    if out == text:
        return text
    return re.sub(r"\s+", " ", out).strip()


def _redact_candidate_pii(text: str, candidate: CandidateInfo) -> str:
    """Strip the candidate's own name/email/phone/location out of a piece of
    text BEFORE it is embedded — embeddings are PII-equivalent (PIPEDA/FIPPA)
    and both ``chunk_embs`` and ``summary_emb`` ride the unencrypted outbox
    into Neo4j in Phase 4. A header chunk is the candidate's verbatim contact
    block; a small model sometimes opens ``summary`` with the candidate's own
    name — either way the resulting vector encodes identity unless the input
    string is scrubbed first.

    Deterministic and surgical: each NON-EMPTY structured identifier is removed
    as a case-insensitive, WHITESPACE-FLEXIBLE pattern (tokens ``re.escape``'d
    and joined with ``r"\\s+"`` — see ``_whitespace_flexible_pattern``), so a
    name split across a PDF line break, a phone reflowed with extra whitespace,
    or a tab-separated two-column header still matches the LLM's normalized
    value. An unrelated reference sharing only ONE token ("Jane Smith" next to
    the candidate's "Jane Doe") is untouched — tokens stay order- and
    boundary-bound. The email LOCAL-PART is additionally scrubbed as its own
    pattern (a bare ``jane.doe`` in a column-wrapped header); the domain is not
    PII and is left. Whitespace left by a removal is collapsed.

    The scrub deliberately errs toward OVER-redaction of the embedded text
    (a common-word ``location`` substring may be removed from a larger word —
    ADR-007 §7 N2); this is not a leak and favors privacy over retrieval
    precision. When the candidate carries no STRUCTURED identifiers, a
    generic (candidate-free) email/phone scrub still runs — see
    ``_generic_contact_scrub`` and ``_redaction_available`` (S7, security
    re-audit round 3): "no structured identifiers" must never silently mean
    "nothing to redact" (that was the fail-OPEN bug an empty ``candidate``
    block exploited). This function only scrubs the EMBEDDER'S input — the
    ``resumes.parsed`` chunk text/summary stay full/cleartext at rest
    (ADR-007 §6, system of record).
    """
    values = [candidate.name, candidate.email, candidate.phone, candidate.location]
    # Also scrub an email's local-part alone — a truncated/column-wrapped header
    # can show `jane.doe` without the `@domain`. The domain is not PII, so only
    # the substring before `@` is added; it is left intact. Gate this on a
    # DISTINCTIVE local-part (one carrying a non-letter separator/digit, e.g.
    # `jane.doe` / `jdoe1`); a bare single-word local-part (`ada`) is left to the
    # full-email pattern only, so scrubbing it does not eat an unrelated
    # first-name/word elsewhere in the body (ADR-007 §7 N2 — favor precision here
    # since the whole email is already removed).
    if candidate.email and "@" in candidate.email:
        local = candidate.email.split("@", 1)[0]
        if re.search(r"[^A-Za-z]", local):
            values.append(local)
    patterns: list[str] = []
    for value in values:
        if not value:
            continue
        pattern = _whitespace_flexible_pattern(value)
        if pattern is not None:
            patterns.append(pattern)
    if not patterns:
        # S7: fail CLOSED, not open — a totally EMPTY candidate block is NOT
        # "nothing to redact"; a generic (candidate-free) email/phone scrub
        # runs instead of returning `text` verbatim. Scoped to exactly this
        # branch (not every call): when at least one structured identifier
        # IS present, the existing precision contract holds unchanged — a
        # field the candidate does NOT have (e.g. `phone=None`) stays
        # untouched, so an unrelated reference's phone number elsewhere in
        # the body is never swept up just because the candidate's email
        # happened to be known.
        return _generic_contact_scrub(text)
    redacted = text
    for pattern in patterns:
        redacted = re.sub(pattern, " ", redacted, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", redacted).strip()


def _is_header_like_chunk(index: int, chunk: ResumeChunk) -> bool:
    """S7 (security re-audit round 3): a résumé's very FIRST chunk is, in
    practice, always its contact block — ``chunk_resume``
    (``src/pipeline/parsing/chunk.py``) buckets everything before the first
    RECOGNISED section heading under ``"other"``, so the real header text is
    never actually labelled ``"header"`` in production; only test fixtures
    use that literal label as a stand-in. Both signals are checked here so a
    future chunker change that DOES start emitting an explicit ``header``
    section is covered too, with no change needed at this call site."""
    return index == 0 or chunk.section == "header"


async def _embed_batched(
    embedder: CachedEmbedder, texts: list[str]
) -> list[list[float]]:
    """Embed ``texts`` in bounded batches, preserving input order.

    One document must never produce a single enormous POST: a 200-chunk résumé
    (the ``ResumeParsed.chunks`` cap) at 4 000 chars/chunk is an ~800 KB body
    that the local model has to hold whole, and one failure loses every vector
    for the document. Batching also lets the CachedEmbedder's Redis read-through
    serve partial hits without re-embedding the rest.
    """
    out: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH_SIZE):
        out.extend(await embedder.embed(texts[start : start + _EMBED_BATCH_SIZE]))
    return out


# ---------------- parse_resume ----------------


async def parse_resume(  # noqa: PLR0911 — each error path gets a distinct return
    ctx: dict[str, Any], resume_id_str: str
) -> str:
    """Parse one résumé end to end and enqueue its projection event.

    Returns one of: ``"parsed"``, ``"missing"``, ``"stale"``, ``"failed"``.
    """
    pool = ctx["pg_pool"]
    llm: LLMClient = ctx["llm"]
    embedder: CachedEmbedder = ctx["embedder"]
    blob_store: BlobStore = ctx["blob_store"]

    resume_id = UUID(resume_id_str)

    async with pool.acquire() as conn:
        meta = await conn.fetchrow(_RESUME_META_SQL, resume_id)
        if meta is None:
            log.warning("parse_resume.missing resume_id=%s", resume_id_str)
            return "missing"
        if meta["status"] not in _PARSEABLE_STATUSES:
            log.info(
                "parse_resume.skipped resume_id=%s status=%s",
                resume_id_str,
                meta["status"],
            )
            return "stale"

        try:
            blob = await blob_store.get(meta["blob_key"])
        except (BlobNotFound, InvalidBlobKey) as exc:
            await resume_service.record_parse_failure(
                conn, resume_id=resume_id, reason=f"blob fetch failed: {exc}"
            )
            return "failed"

        # Extract + chunk are CPU-bound — run them off the event loop.
        try:
            extracted = await asyncio.to_thread(extract_text, blob, meta["mime_type"])
        except (UnsupportedMimeError, EncryptedPdfError, InputTooLargeError) as exc:
            await resume_service.record_parse_failure(
                conn, resume_id=resume_id, reason=f"text extraction failed: {exc}"
            )
            return "failed"

        chunks = await asyncio.to_thread(chunk_resume, extracted)
        if not chunks:
            # Fail BEFORE any LLM call — an empty chunk set is unusable input
            # and a wasted inference pass is expensive on local hardware.
            await resume_service.record_parse_failure(
                conn,
                resume_id=resume_id,
                reason="no chunks produced from extracted text",
            )
            return "failed"

        # LLM extraction is SPLIT (hris ADR 0011) and must stay split:
        #   1. a bounded "core" call for the narrative (candidate, summary,
        #      experience, education) — small local models produce this
        #      reliably;
        #   2. skills come from a deterministic vocabulary scan (always) MERGED
        #      with a best-effort skills-only call.
        # A single combined call made small models loop/over-generate on the
        # open-ended skills list and truncate the whole résumé into invalid JSON.
        core_prompt = load_prompt("resume_core_v1", chunks=chunks)
        try:
            core = await llm.chat_json(
                core_prompt.messages, ResumeCore, max_tokens=3072, max_retries=1
            )
        except LLMOutputInvalidError as exc:
            await resume_service.record_parse_failure(
                conn, resume_id=resume_id, reason=f"llm output invalid: {exc}"
            )
            return "failed"

        merged = await _extract_skills_merged(llm, chunks, resume_id_str)
        # F3 (security re-audit), layer 2: scrub the candidate's own identity
        # out of any skill NAME the LLM emitted, before it enters
        # `resumes.parsed`/the outbox. Must run before `cleaned_parsed` is
        # built below — layer 1 (skills_graph, Phase 4b projection) cannot
        # catch this, it has no candidate context.
        merged = _redact_skill_names_pii(merged, core.candidate, resume_id_str)

        # Non-fatal. Its chunks ride in the parsed jsonb (cl_NNN id space); the
        # LLM extraction goes to the cover_letter_parsed column.
        cl_chunks, cl_parsed = await _parse_cover_letter(
            conn, llm, blob_store, meta, resume_id
        )
        # Hand-assembled: a résumé parse spans 2-3 load_prompt calls, so there
        # is no single RenderedPrompt.version to reach for.
        prompt_version = "resume_core_v1+resume_skills_v2" + (
            "+cover_letter_v1" if cl_chunks else ""
        )

        # Build from dicts so ResumeParsed's lossy row-dropping validator runs.
        # The lossy validator only drops bad LIST ROWS — a violated LIST CAP
        # (e.g. >200 chunks) or a bad scalar still raises. Uncaught, that
        # ValidationError escapes the task entirely: record_parse_failure never
        # runs, the row is stranded uploaded/parsing with a NULL failure_reason,
        # and arq re-runs this whole expensive LLM pipeline on every retry.
        try:
            cleaned_parsed = ResumeParsed.model_validate(
                {
                    "candidate": core.candidate.model_dump(),
                    "summary": core.summary,
                    "total_years_experience": core.total_years_experience,
                    "skills": [s.model_dump() for s in merged],
                    "experience": [e.model_dump() for e in core.experience],
                    "education": [e.model_dump() for e in core.education],
                    "chunks": [c.model_dump() for c in chunks],
                    "cover_letter_chunks": [c.model_dump() for c in cl_chunks],
                }
            )
        except ValidationError as exc:
            # PII-FREE digest only: str(ValidationError) embeds input_value —
            # here that is the candidate's own name/phone/email, and
            # failure_reason is a CLEARTEXT column.
            await resume_service.record_parse_failure(
                conn,
                resume_id=resume_id,
                reason=f"parsed schema invalid: {validation_error_digest(exc)}",
            )
            return "failed"

        # The embedding text NEVER carries name/email/phone/location — embeddings
        # are PII-equivalent under PIPEDA/FIPPA. See _build_summary_text.
        #
        # LLMClient.embed raises LLMOutputInvalidError on a count mismatch AND on
        # the 768-d expected_dim check — a PERMANENT per-document error (the model
        # is mis-pointed or misbehaving; re-running the same bytes won't fix it),
        # exactly like a core chat_json failure. Funnel it through
        # record_parse_failure -> "failed" so it doesn't escape parse_resume
        # uncaught (stranded row + arq retry storm). A TRANSIENT
        # LLMUnavailableError (Ollama down) is deliberately NOT caught here: it
        # propagates so arq retries the genuine outage.
        try:
            # Scrub the candidate's structured identifiers out of every string
            # handed to the embedder — the STORED chunk text / summary stay full
            # (see _redact_candidate_pii / _build_summary_text docstrings).
            candidate = cleaned_parsed.candidate
            summary_text = _redact_candidate_pii(
                _build_summary_text(cleaned_parsed), candidate
            )
            [summary_emb] = await embedder.embed([summary_text])

            # S7 (security re-audit round 3): when there is NO structured
            # candidate identifier to redact against, `_redact_candidate_pii`
            # can only fall back to its generic (email/phone-shaped) scrub —
            # it has no way to strip an arbitrary bare NAME. Refuse to embed
            # the header-like chunk(s) entirely in that case, rather than
            # silently handing the embedder raw contact-block text. Redaction
            # IS available (the normal case), every chunk embeds as before.
            if _redaction_available(candidate):
                chunks_for_embedding = chunks
            else:
                chunks_for_embedding = [
                    c for i, c in enumerate(chunks) if not _is_header_like_chunk(i, c)
                ]
                skipped = len(chunks) - len(chunks_for_embedding)
                if skipped:
                    log.warning(
                        "parse_resume.header_chunk_embedding_skipped_"
                        "redaction_unavailable resume_id=%s count=%d",
                        resume_id_str,
                        skipped,
                    )
            chunk_embs_list = await _embed_batched(
                embedder,
                [
                    _redact_candidate_pii(c.text, candidate)
                    for c in chunks_for_embedding
                ],
            )
        except LLMOutputInvalidError as exc:
            await resume_service.record_parse_failure(
                conn, resume_id=resume_id, reason=f"embedding failed: {exc}"
            )
            return "failed"
        chunk_embs = {
            chunks_for_embedding[i].id: chunk_embs_list[i]
            for i in range(len(chunks_for_embedding))
        }

        # Encrypt PII + write back + enqueue the outbox row, all atomic.
        async with conn.transaction():
            encrypted_pii = await resume_service.encrypt_pii_via_session(
                conn, cleaned_parsed.candidate
            )
            applied = await resume_service.record_parsed(
                conn,
                resume_id=resume_id,
                parsed=cleaned_parsed,
                pii=encrypted_pii,
                cover_letter_parsed=cl_parsed,
                parsed_at=dt.datetime.now(dt.UTC),
            )
            if not applied:
                # The row moved out of uploaded/parsing under us. Drop the
                # result on the floor: an outbox event for a write that never
                # landed would project stale state into the graph in Phase 4.
                log.info(
                    "parse_resume.race resume_id=%s note=%s",
                    resume_id_str,
                    "row left uploaded/parsing mid-parse; dropping result",
                )
                return "stale"

            await outbox_service.enqueue_outbox(
                conn,
                aggregate="resume",
                aggregate_id=resume_id,
                event_type="resume.parsed",
                payload={
                    # NO `candidate` block AND no raw chunk TEXT. Phase 4 projects
                    # this payload into Neo4j and needs skills/experience/
                    # embeddings, NOT identity — `resumes` (pgcrypto-encrypted) is
                    # the system of record for PII, and the outbox is an
                    # unencrypted jsonb table. Beyond the structured `candidate`
                    # block, header chunks carry the candidate's name/email/phone
                    # VERBATIM in `chunks[].text` / `cover_letter_chunks[].text`,
                    # so those are dropped too (ids/section/page stay — Phase 4
                    # keys embeddings by chunk id and reads any chunk-text preview
                    # from `resumes.parsed`, the system of record, which KEEPS the
                    # full text).
                    "parsed": cleaned_parsed.model_dump(exclude=_OUTBOX_PARSED_EXCLUDE),
                    "summary_emb": summary_emb,
                    "chunk_embs": chunk_embs,
                    "prompt_version": prompt_version,
                    # job_id rides along so Phase 4's stage-1 vector search can
                    # scope candidates to "resumes uploaded against THIS job"
                    # rather than every résumé in the system — without it a
                    # recruiter sees other jobs' candidates in their shortlist.
                    "job_id": str(meta["job_id"]),
                },
            )

    log.info(
        "parse_resume.ok resume_id=%s chunks=%d skills=%d",
        resume_id_str,
        len(chunks),
        len(cleaned_parsed.skills),
    )
    return "parsed"


def _build_summary_text(parsed: ResumeParsed) -> str:
    """The text that gets EMBEDDED. Excludes name/email/phone/location.

    MERGE-BLOCKING INVARIANT: embeddings are treated as PII-equivalent
    (PIPEDA/FIPPA) — this string is fed to ``nomic-embed-text`` and the vector
    is stored in a Neo4j index that Phase 4 queries. This function must read
    ONLY ``summary`` / ``skills`` / ``experience`` / ``education`` and must
    NEVER touch the ``CandidateInfo`` block. Two tests guard it: a runtime
    sentinel check and a static ``inspect.getsource`` check.
    """
    parts: list[str] = []
    if parsed.summary:
        parts.append(parsed.summary)
    if parsed.skills:
        parts.append("Skills: " + ", ".join(s.name for s in parsed.skills[:30]))
    if parsed.experience:
        roles = [f"{e.title} at {e.company}" for e in parsed.experience[:5]]
        parts.append("Recent roles: " + "; ".join(roles))
    if parsed.education:
        edu = [
            f"{e.degree}, {e.institution}" + (f" ({e.year})" if e.year else "")
            for e in parsed.education[:3]
        ]
        parts.append("Education: " + "; ".join(edu))
    # Never "" — an empty string is a degenerate embedding-cache key (the cache
    # keys on sha256(f"{model}\n{text}"), so every contentless résumé would
    # collide on one vector).
    return ". ".join(parts) or "candidate"


# ---------------- resume.parsed graph projection (Phase 4b) ----------------
#
# Ported behaviourally from hris ``apps/worker/src/worker/resume_tasks.py::
# project_resume`` / ``_resume_projection_tx``, with the human-locked
# deviations from ``docs/EXTRACTION_PLAN.md`` (4b row):
#
# * Decision 1 (CRIT) — NO chunk text, ever, not even a preview. hris writes
#   ``preview=chunk["text"][:200]``; the outbox payload has no ``text`` key at
#   all (ADR-007 §7 / R2), so a verbatim port would either KeyError or (worse,
#   if `.get()`'d) write an empty string that could later be "fixed" back into
#   a real leak. Neo4j never needs it — the reveal path reads
#   ``resumes.parsed`` (Postgres), the system of record.
# * Decision 3 — skill names are resolved via ``skills_graph.resolve_
#   canonical_names`` on a plain session, BEFORE ``session.execute_write`` is
#   ever called. The callback handed to ``execute_write`` receives the
#   RESOLVED ``{raw_name: canonical}`` mapping only — never ``llm``/
#   ``embedder`` — so it architecturally cannot make a model call while
#   holding the write transaction.
# * R8 (MED) — pinned label set: only ``Resume``/``ResumeChunk``/``Skill`` are
#   ever written here. Never ``Company``/``Institution``, even though the
#   Neo4j bootstrap already declares constraints for those labels.


async def project_resume(
    driver: AsyncDriver,
    *,
    resume_id: Any,
    payload: dict[str, Any],
    llm: Any,
    embedder: Any,
) -> None:
    """Project one ``resume.parsed`` outbox payload into Neo4j.

    Idempotent: the write transaction DETACH-DELETEs old chunks and drops old
    HAS_SKILL edges before re-creating them, so a re-parse never accumulates
    cruft. No Postgres dependency at all — decision 1 means this function
    never needs to read a chunk-text preview from anywhere.
    """
    parsed = payload["parsed"]
    summary_emb = payload["summary_emb"]
    chunk_embs = payload["chunk_embs"]
    # job_id is required for per-job shortlist scoping; tolerate its absence
    # (older/malformed payloads) by passing None through — the projection
    # simply skips the job_id assignment for that resume.
    job_id = payload.get("job_id")

    skill_names = [s["name"] for s in parsed.get("skills", [])]

    async with driver.session() as session:
        # Decision 3: every embed()/chat_json() round trip happens HERE, on a
        # plain auto-commit session, before any write transaction opens.
        resolved_skills = await skills_graph.resolve_canonical_names(
            session, skill_names, llm=llm, embedder=embedder
        )
        await session.execute_write(
            _resume_projection_tx,
            str(resume_id),
            parsed,
            summary_emb,
            chunk_embs,
            resolved_skills,
            job_id=job_id,
        )


async def _resume_projection_tx(
    tx: Any,
    resume_id: str,
    parsed: dict[str, Any],
    summary_emb: list[float],
    chunk_embs: dict[str, list[float]],
    resolved_skills: dict[str, str | None],
    *,
    job_id: str | None,
) -> None:
    """The write-transaction callback. Architecturally cannot call an LLM or
    embedder — neither parameter exists on this signature (see
    ``test_resume_projection_tx_signature_has_no_llm_or_embedder_parameter``).
    """
    # Resume node + summary embedding + job_id (used by stage-1 scoping so
    # each job's shortlist only considers its own resume pool).
    await tx.run(
        """
        MERGE (r:Resume {id: $rid})
        SET r.total_years_experience = $tye,
            r.summary_embedding = $emb,
            r.status = 'parsed',
            r.updated_at = datetime(),
            r.job_id = coalesce($jid, r.job_id)
        """,
        rid=resume_id,
        tye=parsed.get("total_years_experience", 0),
        emb=summary_emb,
        jid=job_id,
    )

    # Drop old chunks + skill edges BEFORE recreating them — a re-parse must
    # not accumulate stale HAS_CHUNK/HAS_SKILL edges.
    await tx.run(
        "MATCH (:Resume {id: $rid})-[:HAS_CHUNK]->(c:ResumeChunk) DETACH DELETE c",
        rid=resume_id,
    )
    await tx.run("MATCH (:Resume {id: $rid})-[h:HAS_SKILL]->() DELETE h", rid=resume_id)

    # Chunks: Decision 1 — id/section/page/embedding ONLY. The outbox payload
    # never carries a `text` key (ADR-007 §7 / R2); even if it did, no text or
    # preview is ever written to this node.
    for chunk in parsed.get("chunks", []):
        emb = chunk_embs.get(chunk["id"])
        if emb is None:
            continue
        await tx.run(
            """
            MATCH (r:Resume {id: $rid})
            CREATE (c:ResumeChunk {
                id: $cid, resume_id: $rid, section: $section, page: $page,
                embedding: $emb
            })
            CREATE (r)-[:HAS_CHUNK]->(c)
            """,
            rid=resume_id,
            cid=chunk["id"],
            section=chunk.get("section", "other"),
            page=chunk.get("page", 0),
            emb=emb,
        )

    # Skills: use the ALREADY-RESOLVED canonical name (decision 3) — no
    # embed()/chat_json() call happens in this transaction.
    for skill in parsed.get("skills", []):
        raw_name = skill["name"]
        # F6 (security re-audit): a name ABSENT from resolved_skills means
        # resolve_canonical_names was never asked to resolve it at all — a
        # caller bug. Falling back to the raw name (hris's
        # ``resolved_skills.get(name, name)``) matches no Skill node in
        # Cypher and the HAS_SKILL edge silently vanishes (R5's failure
        # class). Fail loud instead.
        if raw_name not in resolved_skills:
            raise skills_graph.UnresolvedSkillNameError(
                "resume skill name has no resolution entry"
            )
        canonical = resolved_skills[raw_name]
        if canonical is None:
            # F3 (security re-audit): shape-rejected as PII at the
            # resolution boundary — drop this skill/edge silently, never
            # project it. Not a caller bug, unlike the branch above.
            continue
        await skills_graph._ensure_categories(tx, canonical)
        evidence = skill.get("evidence_chunk_ids") or []
        await tx.run(
            """
            MATCH (r:Resume {id: $rid}), (s:Skill {canonical_name: $cn})
            MERGE (r)-[h:HAS_SKILL]->(s)
            SET h.years = $years,
                h.last_used_year = $luy,
                h.evidence_chunk_id = $first_ev
            """,
            rid=resume_id,
            cn=canonical,
            years=skill.get("years"),
            luy=skill.get("last_used_year"),
            first_ev=evidence[0] if evidence else None,
        )

    # R8: no Company/Institution/experience/education writes from this
    # module — the matching pipeline that would consume them is out of scope
    # for Phase 4b.
