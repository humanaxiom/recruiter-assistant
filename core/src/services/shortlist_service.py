"""Shortlist + reverse-match write path (Phase 4d).

Write-only persistence for the 4c orchestrator's results, raw asyncpg + hand-
written SQL (SQLAlchemy is dropped), mirroring ``job_service`` /
``resume_service`` / ``outbox_service``: every function takes an open
connection and leaves transaction scoping to the caller (the worker wraps each
persist in ONE ``conn.transaction()`` so the DELETE + re-INSERT-per-run commit
atomically).

Both functions are DELETE-first (clearing any stale prior run BEFORE inserting,
even when the new result is empty — a rerun that now yields nothing must still
clear the previous shortlist). The two paths are deliberate MIRROR IMAGES of
each other, dictated by the two tables' shapes (verified against
``src/models/ddl.py``):

* ``shortlist_entries`` has NO dedicated ``score_structured`` /
  ``score_evidence`` columns and ``evidence JSONB NOT NULL`` ->
  ``persist_shortlist`` FOLDS ``score_structured`` / ``score_evidence`` into the
  ``score_breakdown`` jsonb (losing them would be a data-loss bug) and coerces
  ``evidence=None`` to the JSON literal ``{}`` (never SQL NULL).
* ``reverse_match_entries`` HAS dedicated ``score_structured`` /
  ``score_evidence`` columns and a NULLABLE ``evidence JSONB`` ->
  ``persist_reverse_match`` writes those as their OWN SQL args (NOT folded into
  the breakdown jsonb) and passes ``evidence=None`` through as SQL NULL.

``pipeline_meta`` jsonb carries the full ``MatchWeights`` + ``git_sha`` on both
paths — it is serialised straight from the orchestrator's ``PipelineMeta``,
which already stamps both (Phase 4c wired ``git_sha`` through ``Settings`` ->
``MatchingContext`` -> ``PipelineMeta``).

No new PII redaction here (ADR-007 §6): stage-3 evidence quotes are written
verbatim, exactly as extracted from ``resumes.parsed`` chunk text. Any future
redaction is Phase 5's display-only concern and must not touch this at-rest
write path silently.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any
from uuid import UUID

from src.errors import NotFoundError
from src.pipeline.matching.orchestrator import JobMatchResult, ShortlistResult
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    EvidenceObject,
    PipelineMeta,
    ScoreBreakdown,
    ShortlistEntry,
)
from src.schemas.resumes import ResumeParsed
from src.services import DbConn
from src.services.pii import set_pii_key
from src.services.redaction import (
    blind_label_map,
    pseudonym,
    redact_text,
    redacted_filename,
)

_DELETE_SHORTLIST_SQL = "DELETE FROM shortlist_entries WHERE job_id = $1"

_INSERT_SHORTLIST_SQL = """
INSERT INTO shortlist_entries (
    job_id, resume_id, rank, score_final, score_breakdown, evidence, pipeline_meta
) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb)
"""

_DELETE_REVERSE_SQL = "DELETE FROM reverse_match_entries WHERE resume_id = $1"

_INSERT_REVERSE_SQL = """
INSERT INTO reverse_match_entries (
    resume_id, job_id, rank, score_final, score_structured, score_evidence,
    score_breakdown, evidence, requirement_count, must_have_count, pipeline_meta
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11::jsonb)
"""


def _meta_json(meta: PipelineMeta | None) -> str:
    """Serialise the reproducibility stamp for the ``pipeline_meta`` jsonb
    column (``NOT NULL`` on both tables). ``model_dump_json`` handles the
    nested ``MatchWeights`` and the ``generated_at`` datetime; the rare
    ``None`` case (a job/résumé that vanished mid-run before the orchestrator
    stamped meta) still writes a valid, non-null object carrying the default
    weights so the ``NOT NULL`` constraint holds."""
    if meta is None:
        return json.dumps({"weights": DEFAULT_WEIGHTS.model_dump(mode="json")})
    return meta.model_dump_json()


async def persist_shortlist(conn: DbConn, result: ShortlistResult) -> int:
    """Replace the shortlist for ``result.job_id`` (DELETE-first, then one
    INSERT per ranked entry) and return the number of rows inserted."""
    await conn.execute(_DELETE_SHORTLIST_SQL, result.job_id)
    meta_json = _meta_json(result.pipeline_meta)
    for entry in result.entries:
        # shortlist_entries has no dedicated structured/evidence columns —
        # fold them into the breakdown jsonb so they are not lost.
        breakdown = entry.breakdown.model_dump()
        breakdown["score_structured"] = entry.score_structured
        breakdown["score_evidence"] = entry.score_evidence
        # evidence JSONB NOT NULL: None -> the empty-object literal, never NULL.
        evidence_json = (
            json.dumps(entry.evidence.model_dump())
            if entry.evidence is not None
            else "{}"
        )
        await conn.execute(
            _INSERT_SHORTLIST_SQL,
            result.job_id,
            entry.resume_id,
            entry.rank,
            entry.score_final,
            json.dumps(breakdown),
            evidence_json,
            meta_json,
        )
    return len(result.entries)


async def persist_reverse_match(conn: DbConn, result: JobMatchResult) -> int:
    """Replace the reverse-match result for ``result.resume_id`` (DELETE-first,
    then one INSERT per ranked entry) and return the number of rows inserted."""
    await conn.execute(_DELETE_REVERSE_SQL, result.resume_id)
    meta_json = _meta_json(result.pipeline_meta)
    for entry in result.entries:
        # reverse_match_entries HAS dedicated structured/evidence columns — keep
        # them out of the breakdown jsonb (mirror-image of the shortlist path).
        breakdown_json = json.dumps(entry.breakdown.model_dump())
        # evidence JSONB is nullable here: None passes through as SQL NULL.
        evidence_json = (
            json.dumps(entry.evidence.model_dump())
            if entry.evidence is not None
            else None
        )
        await conn.execute(
            _INSERT_REVERSE_SQL,
            result.resume_id,
            entry.job_id,
            entry.rank,
            entry.score_final,
            entry.score_structured,
            entry.score_evidence,
            breakdown_json,
            evidence_json,
            entry.requirement_count,
            entry.must_have_count,
            meta_json,
        )
    return len(result.entries)


# ── read (Phase 5) ─────────────────────────────────────────────────────────

# The review-workflow joins (shortlist_decisions / stage_transitions / users)
# are CUT — those tables do not exist in this project. The read layer surfaces
# only the ranking row + blind-review masking.
_ENTRY_COLS = (
    "id, job_id, resume_id, rank, score_final, "
    "score_breakdown, evidence, generated_at"
)
_LIST_QUERY = (
    f"SELECT {_ENTRY_COLS} FROM shortlist_entries "
    "WHERE job_id = $1 ORDER BY rank ASC"
)
_GET_QUERY = f"SELECT {_ENTRY_COLS} FROM shortlist_entries WHERE id = $1"

# Blind-review queries: same columns (aliased `se`, since the JOIN to resumes
# makes a bare `id` ambiguous) plus the candidate's decrypted name/contact and
# the résumé's parsed json — appended ONLY so we can redact them out of the
# evidence server-side. Those `_c_*` columns are popped before the response is
# built and never reach the client. Caller need not pre-`set_pii_key`; the
# read function opens its own transaction and sets it.
_BLIND_COLS = (
    "se.id, se.job_id, se.resume_id, se.rank, se.score_final, "
    "se.score_breakdown, se.evidence, se.generated_at, "
    "pgp_sym_decrypt(r.candidate_name, current_setting('app.pii_key')) "
    "AS _c_name, "
    "pgp_sym_decrypt(r.candidate_email, current_setting('app.pii_key')) "
    "AS _c_email, "
    "pgp_sym_decrypt(r.candidate_phone, current_setting('app.pii_key')) "
    "AS _c_phone, "
    "r.parsed AS _c_parsed"
)
_BLIND_LIST_QUERY = (
    f"SELECT {_BLIND_COLS} FROM shortlist_entries se "
    "JOIN resumes r ON r.id = se.resume_id "
    "WHERE se.job_id = $1 ORDER BY se.rank ASC"
)
_BLIND_GET_QUERY = (
    f"SELECT {_BLIND_COLS} FROM shortlist_entries se "
    "JOIN resumes r ON r.id = se.resume_id WHERE se.id = $1"
)
_BLIND_CHECK_QUERY = (
    "SELECT j.blind_review FROM jobs j "
    "JOIN shortlist_entries se ON se.job_id = j.id WHERE se.id = $1"
)


async def list_for_job(conn: DbConn, *, job_id: UUID) -> list[ShortlistEntry]:
    """Ranked shortlist for a job. Under blind review, identity is masked and a
    rank-based ``display_label`` is applied before any DTO is built."""
    if await conn.fetchval("SELECT blind_review FROM jobs WHERE id = $1", job_id):
        async with conn.transaction():
            await set_pii_key(conn)
            rows = await conn.fetch(_BLIND_LIST_QUERY, job_id)
        return [_row_to_blind_entry(r) for r in rows]
    rows = await conn.fetch(_LIST_QUERY, job_id)
    return [_row_to_entry(r) for r in rows]


async def get_one(conn: DbConn, entry_id: UUID) -> ShortlistEntry:
    """One shortlist entry by id, blind-masked when its job is blind."""
    if await conn.fetchval(_BLIND_CHECK_QUERY, entry_id):
        async with conn.transaction():
            await set_pii_key(conn)
            row = await conn.fetchrow(_BLIND_GET_QUERY, entry_id)
        if row is None:
            raise NotFoundError(
                f"shortlist entry {entry_id} not found", entry_id=str(entry_id)
            )
        return _row_to_blind_entry(row)
    row = await conn.fetchrow(_GET_QUERY, entry_id)
    if row is None:
        raise NotFoundError(
            f"shortlist entry {entry_id} not found", entry_id=str(entry_id)
        )
    return _row_to_entry(row)


def _parse_entry_jsonb(raw: dict[str, Any]) -> None:
    """In-place: coerce the jsonb score_breakdown/evidence columns into their
    pydantic models.

    ``persist_shortlist`` (4d) FOLDS ``score_structured``/``score_evidence``
    into the ``score_breakdown`` jsonb (the table has no dedicated columns).
    ``ScoreBreakdown`` is ``extra="forbid"``, so those two folded keys MUST be
    stripped before validating or every 4d row raises ValidationError."""
    sb = raw["score_breakdown"]
    if isinstance(sb, str):
        sb = json.loads(sb)
    sb = dict(sb)
    sb.pop("score_structured", None)
    sb.pop("score_evidence", None)
    raw["score_breakdown"] = ScoreBreakdown.model_validate(sb)
    ev = raw["evidence"]
    if isinstance(ev, str):
        ev = json.loads(ev)
    raw["evidence"] = EvidenceObject.model_validate(ev) if ev is not None else None


def _row_to_entry(row: Any) -> ShortlistEntry:
    raw = dict(row)
    _parse_entry_jsonb(raw)
    return ShortlistEntry.model_validate(raw)


def _redact_evidence(
    ev: EvidenceObject | None,
    *,
    name: str | None,
    email: str | None,
    phone: str | None,
    term_map: dict[str, str] | None = None,
    location: str | None = None,
    redact_locations: bool = False,
) -> EvidenceObject | None:
    """Scrub identity out of the LLM evidence. Closes the hris gap: BOTH the
    requirement evidence + overall_summary AND the cover-letter evidence +
    overall_motivation are redacted (a cover-letter quote can carry the
    candidate's own name)."""
    if ev is None:
        return None

    def _r(text: str) -> str:
        return redact_text(
            text,
            name=name,
            email=email,
            phone=phone,
            term_map=term_map,
            location=location,
            redact_locations=redact_locations,
        )

    reqs = [r.model_copy(update={"evidence": _r(r.evidence)}) for r in ev.requirements]
    cover = [
        c.model_copy(update={"evidence": _r(c.evidence)})
        for c in ev.cover_letter_evidence
    ]
    return ev.model_copy(
        update={
            "requirements": reqs,
            "overall_summary": _r(ev.overall_summary),
            "cover_letter_evidence": cover,
            "overall_motivation": _r(ev.overall_motivation),
        }
    )


def labels_from_parsed(raw_parsed: Any) -> dict[str, str]:
    """Employer/school label map from a résumé's stored parse JSON, so evidence
    quotes that name an employer/school get the same labels as the résumé
    detail. Empty when the résumé isn't parsed. Shared with the export path."""
    if raw_parsed is None:
        return {}
    if isinstance(raw_parsed, str):
        raw_parsed = json.loads(raw_parsed)
    parsed = ResumeParsed.model_validate(raw_parsed)
    return blind_label_map(
        employers=[e.company for e in parsed.experience],
        institutions=[ed.institution for ed in parsed.education],
    )


def location_from_parsed(raw_parsed: Any) -> str | None:
    """The candidate's structured location from a stored parse, so evidence +
    export text scrub the same city as the résumé detail. Shared with export."""
    if raw_parsed is None:
        return None
    if isinstance(raw_parsed, str):
        raw_parsed = json.loads(raw_parsed)
    return ResumeParsed.model_validate(raw_parsed).candidate.location


def _row_to_blind_entry(row: Any) -> ShortlistEntry:
    raw = dict(row)
    name = raw.pop("_c_name", None)
    email = raw.pop("_c_email", None)
    phone = raw.pop("_c_phone", None)
    parsed_raw = raw.pop("_c_parsed", None)
    labels = labels_from_parsed(parsed_raw)
    location = location_from_parsed(parsed_raw)
    _parse_entry_jsonb(raw)
    # Redaction happens BEFORE the DTO is built (ADR-006 §4): no decrypted PII
    # ever reaches ShortlistEntry.
    raw["evidence"] = _redact_evidence(
        raw["evidence"],
        name=name,
        email=email,
        phone=phone,
        term_map=labels,
        location=location,
        redact_locations=True,
    )
    raw["blinded"] = True
    raw["display_label"] = pseudonym(int(raw["rank"]))
    return ShortlistEntry.model_validate(raw)


# ── export (Phase 5) ───────────────────────────────────────────────────────

_EXPORT_QUERY = """
SELECT
    s.rank,
    s.resume_id,
    s.score_final,
    s.score_breakdown,
    s.evidence,
    s.pipeline_meta,
    s.generated_at,
    j.title AS job_title,
    j.department AS job_department,
    r.original_filename,
    pgp_sym_decrypt(r.candidate_name, current_setting('app.pii_key'))
        AS candidate_name,
    pgp_sym_decrypt(r.candidate_email, current_setting('app.pii_key'))
        AS candidate_email,
    pgp_sym_decrypt(r.candidate_phone, current_setting('app.pii_key'))
        AS candidate_phone,
    r.parsed AS candidate_parsed
FROM shortlist_entries s
JOIN jobs j ON j.id = s.job_id
JOIN resumes r ON r.id = s.resume_id
WHERE s.job_id = $1
ORDER BY s.rank ASC
"""


def _as_obj(v: object) -> dict[str, Any]:
    """jsonb columns arrive as a dict (JSON codec) or a JSON string depending on
    the codec; normalise either to a plain dict."""
    if isinstance(v, str):
        loaded = json.loads(v)
        return loaded if isinstance(loaded, dict) else {}
    if isinstance(v, dict):
        return v
    return {}


def _redact_evidence_dict(
    ev: object,
    *,
    name: str | None,
    term_map: dict[str, str],
    location: str | None = None,
    redact_locations: bool = False,
) -> dict[str, Any]:
    """Scrub name/contact + label employer/school (and, when requested, foreign
    geography) out of an export row's evidence dict — requirement quotes +
    overall_summary AND cover-letter quotes + overall_motivation (gap closed)."""
    obj = _as_obj(ev)
    if not obj:
        return obj

    def _r(text: str) -> str:
        return redact_text(
            text,
            name=name,
            term_map=term_map,
            location=location,
            redact_locations=redact_locations,
        )

    for key in ("requirements", "cover_letter_evidence"):
        items = obj.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("evidence"), str):
                    item["evidence"] = _r(item["evidence"])
    for key in ("overall_summary", "overall_motivation"):
        if isinstance(obj.get(key), str):
            obj[key] = _r(obj[key])
    return obj


def _apply_reveal(rows: list[dict[str, Any]], *, reveal: bool) -> None:
    """When ``reveal`` is False, strip decrypted identity and substitute the
    rank-based pseudonym, mask the identifying résumé filename, and scrub
    name/contact + employer/school labels out of the evidence quote text (using
    the REAL name still present here BEFORE it is swapped). Mutates rows in
    place."""
    if reveal:
        return
    for r in rows:
        labels = labels_from_parsed(r.get("candidate_parsed"))
        location = location_from_parsed(r.get("candidate_parsed"))
        r["evidence"] = _redact_evidence_dict(
            r.get("evidence"),
            name=r.get("candidate_name"),
            term_map=labels,
            location=location,
            redact_locations=True,
        )
        r["candidate_name"] = pseudonym(int(r["rank"]))
        r["candidate_email"] = ""
        r["candidate_phone"] = ""
        # A résumé named after its candidate leaks identity through the export
        # (the resume_file column) — mask it, keeping only its extension.
        r["original_filename"] = redacted_filename(r.get("original_filename"))


async def export_rows(
    conn: DbConn, *, job_id: UUID, reveal: bool
) -> list[dict[str, Any]]:
    """Flat rows for CSV / JSON export, already reveal-applied. The caller MUST
    have run ``pii_service.set_pii_key(conn)`` inside an open transaction — the
    raw-SQL ``pgp_sym_decrypt`` fails loud otherwise (it is NOT called here)."""
    rows = await conn.fetch(_EXPORT_QUERY, job_id)
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["score_breakdown"] = _as_obj(d.get("score_breakdown"))
        d["evidence"] = _as_obj(d.get("evidence"))
        d["pipeline_meta"] = _as_obj(d.get("pipeline_meta"))
        out.append(d)
    _apply_reveal(out, reveal=reveal)
    # candidate_parsed was only needed to derive labels/location for redaction;
    # it carries the raw name and must never reach the exported payload.
    for d in out:
        d.pop("candidate_parsed", None)
    return out


# ── export formatters (pure) ───────────────────────────────────────────────


def _skill_summary(breakdown: dict[str, Any]) -> dict[str, Any]:
    """Flatten score_breakdown.skill_contributions into recruiter-facing
    columns: which required skills matched vs. are missing, and which of the
    MISSING ones are must-haves."""
    contribs = breakdown.get("skill_contributions")
    matched: list[str] = []
    missing: list[str] = []
    must_missing: list[str] = []
    must_total = 0
    for c in contribs if isinstance(contribs, list) else []:
        if not isinstance(c, dict):
            continue
        cname = str(c.get("skill") or "")
        is_must = bool(c.get("is_must_have"))
        must_total += int(is_must)
        if c.get("reason") == "missing":
            missing.append(cname)
            if is_must:
                must_missing.append(cname)
        else:
            matched.append(cname)
    return {
        "skills_total": len(matched) + len(missing),
        "skills_matched_count": len(matched),
        "skills_matched": "; ".join(matched),
        "skills_missing": "; ".join(missing),
        "must_have_count": must_total,
        "must_have_missing": "; ".join(must_missing),
    }


def _evidence_coverage(evidence: dict[str, Any]) -> dict[str, Any]:
    """Per-requirement coverage counts + the evidence-completeness fraction,
    computed with the same formula the scorer uses:
    (#met-with-confidence>=0.7 + 0.5*#partial) / #requirements."""
    reqs = evidence.get("requirements")
    met = partial = missing = strong_met = 0
    for r in reqs if isinstance(reqs, list) else []:
        if not isinstance(r, dict):
            continue
        status = r.get("status")
        conf = r.get("confidence")
        if status == "met":
            met += 1
            if isinstance(conf, (int, float)) and conf >= 0.7:
                strong_met += 1
        elif status == "partial":
            partial += 1
        elif status == "missing":
            missing += 1
    total = met + partial + missing
    completeness = round((strong_met + 0.5 * partial) / total, 4) if total else 0.0
    summary = str(evidence.get("overall_summary") or "").replace("\n", " ")
    return {
        "requirement_count": total,
        "requirements_met": met,
        "requirements_partial": partial,
        "requirements_missing": missing,
        "evidence_completeness": completeness,
        "evidence_summary": summary,
    }


_CSV_FIELDS = [
    "rank",
    "candidate_name",
    "candidate_email",
    "candidate_phone",
    "resume_file",
    "job_title",
    "job_department",
    "score_final",
    # Skill gap — the confidence headline, kept beside the score.
    "must_have_missing",
    "skills_missing",
    "skills_matched_count",
    "skills_total",
    "must_have_count",
    # Sub-scores that reconcile FINAL.
    "score_structured",
    "score_evidence_completeness",
    "score_motivation",
    # Structured axes (each 0..1).
    "skill",
    "experience",
    "education",
    "seniority",
    "vector",
    # LLM evidence coverage.
    "requirement_count",
    "requirements_met",
    "requirements_partial",
    "requirements_missing",
    "evidence_summary",
    "skills_matched",
    # Traceability. (The review-workflow columns — current_stage /
    # current_decision / decision_* — are CUT: no review pipeline here.)
    "generated_at",
    "pipeline_version",
    "resume_id",
]


def shortlist_csv(rows: list[dict[str, Any]]) -> str:
    """One row per candidate — the recruiter working file (Excel)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        sb = _as_obj(r["score_breakdown"])
        ev = _as_obj(r["evidence"])
        meta = _as_obj(r["pipeline_meta"])
        skills = _skill_summary(sb)
        coverage = _evidence_coverage(ev)
        writer.writerow(
            {
                "rank": r["rank"],
                "candidate_name": r["candidate_name"] or "",
                "candidate_email": r["candidate_email"] or "",
                "candidate_phone": r["candidate_phone"] or "",
                "resume_file": r["original_filename"] or "",
                "job_title": r["job_title"] or "",
                "job_department": r["job_department"] or "",
                "score_final": round(float(r["score_final"]), 4),
                "must_have_missing": skills["must_have_missing"],
                "skills_missing": skills["skills_missing"],
                "skills_matched_count": skills["skills_matched_count"],
                "skills_total": skills["skills_total"],
                "must_have_count": skills["must_have_count"],
                "score_structured": sb.get("structured"),
                "score_evidence_completeness": coverage["evidence_completeness"],
                "score_motivation": sb.get("motivation"),
                "skill": sb.get("skill"),
                "experience": sb.get("experience"),
                "education": sb.get("education"),
                "seniority": sb.get("seniority"),
                "vector": sb.get("vector"),
                "requirement_count": coverage["requirement_count"],
                "requirements_met": coverage["requirements_met"],
                "requirements_partial": coverage["requirements_partial"],
                "requirements_missing": coverage["requirements_missing"],
                "evidence_summary": coverage["evidence_summary"],
                "skills_matched": skills["skills_matched"],
                "generated_at": r["generated_at"].isoformat(),
                "pipeline_version": str(meta.get("git_sha") or "")[:12],
                "resume_id": str(r["resume_id"]),
            }
        )
    return buf.getvalue()


_EVIDENCE_CSV_FIELDS = [
    "rank",
    "candidate_name",
    "resume_file",
    "job_title",
    "requirement",
    "status",
    "confidence",
    "quote",
    "evidence_chunk_ids",
]


def shortlist_evidence_csv(rows: list[dict[str, Any]]) -> str:
    """One row per (candidate, requirement): the per-quote audit detail.
    Zero-requirement candidates still get a single placeholder row."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EVIDENCE_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        base = {
            "rank": r["rank"],
            "candidate_name": r["candidate_name"] or "",
            "resume_file": r["original_filename"] or "",
            "job_title": r["job_title"] or "",
        }
        reqs = _as_obj(r["evidence"]).get("requirements")
        req_list = (
            [req for req in reqs if isinstance(req, dict)]
            if isinstance(reqs, list)
            else []
        )
        if not req_list:
            writer.writerow({**base, "requirement": "(no evidence generated)"})
            continue
        for req in req_list:
            chunks = req.get("evidence_chunk_ids")
            writer.writerow(
                {
                    **base,
                    "requirement": str(req.get("requirement") or ""),
                    "status": str(req.get("status") or ""),
                    "confidence": req.get("confidence"),
                    "quote": str(req.get("evidence") or "").replace("\n", " "),
                    "evidence_chunk_ids": (
                        "; ".join(str(c) for c in chunks)
                        if isinstance(chunks, list)
                        else ""
                    ),
                }
            )
    return buf.getvalue()


def _normalise_for_json(row: dict[str, Any]) -> dict[str, Any]:
    """asyncpg returns Decimal / datetime — json.dumps wants str. score_final's
    Decimal needs explicit float coercion so consumers don't get "0.6420" as a
    string; score_breakdown / evidence / pipeline_meta stay as JSON objects."""
    out = dict(row)
    for key in ("score_breakdown", "evidence", "pipeline_meta"):
        val = out.get(key)
        if isinstance(val, str):
            out[key] = json.loads(val)
    if out.get("score_final") is not None:
        out["score_final"] = float(out["score_final"])
    return out


def shortlist_json(rows: list[dict[str, Any]]) -> str:
    """Full nested JSON, Decimal/datetime normalised."""
    return json.dumps([_normalise_for_json(r) for r in rows], default=str)
