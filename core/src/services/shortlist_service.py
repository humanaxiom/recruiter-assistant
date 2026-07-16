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

import json

from src.pipeline.matching.orchestrator import JobMatchResult, ShortlistResult
from src.schemas.matching import DEFAULT_WEIGHTS, PipelineMeta
from src.services import DbConn

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
