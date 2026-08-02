"""Matching pipeline orchestrator (Phase 4c).

``generate_shortlist(job_id, ctx)`` runs all four stages and returns a
``ShortlistResult`` ready for persistence; ``match_resume_to_jobs`` is the
reverse match (résumé → jobs). The orchestrator depends on ``MatchingContext``
(db conn, neo4j driver, LLMClient, CachedEmbedder) which the worker constructs
at task start.

``run_match`` is the in-memory stage-4 ranker the eval harness
(``tests/evals/run_evals.py``) wires: it takes already-scored candidates and
ranks them with the same pure combine the DB path uses.

Ported from hris ``packages/pipeline/src/pipeline/matching/orchestrator.py``
with the Phase 4c blocker fixes:

* **blocker #4** — ``_stage2_skill_rows`` reads ``reqSkill.canonical_key``
  (ADR-008 renamed it from ``canonical_name``); a verbatim port returns
  ``skill=None`` and a pydantic ValidationError against a real Neo4j.
* **blocker #5** — NICE_TO_HAVE skills feed stage-3's evidence prompt but never
  the stage-2 structured skill sub-score (only ``REQUIRES`` feeds it).
* **blocker #7** — stage-3 chunk text sources from ``resumes.parsed`` (Postgres),
  never the outbox (ADR-007 stripped chunk text from the outbox).
* **blocker #10** — reverse match runs evidence at the worker-path default
  (``settings.match_reverse_evidence_k`` = 10), not a synchronous-endpoint 0.
* ``load_prompt`` is imported by name so the integration tests can patch
  ``src.pipeline.matching.orchestrator.load_prompt``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg
from neo4j import AsyncDriver

from src.pipeline.llm import CachedEmbedder, LLMClient, LLMOutputInvalidError
from src.pipeline.matching.stages import (
    _CombineInput,
    _evidence_completeness,
    _motivation_score,
    _SkillRowFromCypher,
    is_senior_candidate,
    normalise_vector_scores,
    score_education,
    score_experience,
    score_skill_breakdown,
    stage4_combine,
    verify_evidence,
)
from src.prompts import load_prompt
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    EvidenceObject,
    EvidenceObjectIngest,
    MatchWeights,
    PipelineMeta,
    ScoreBreakdown,
)
from src.settings import Settings, non_matchable_families_from_settings

log = logging.getLogger(__name__)


# ---------------- typed I/O ----------------


@dataclass(frozen=True)
class JobView:
    id: UUID
    title: str
    min_years: int | None
    education_min_level: str | None
    education_fields: tuple[str, ...]
    required_skills: tuple[str, ...]
    nice_to_have_skills: tuple[str, ...]


# Default tuning knobs (ADR 0021). Production callers populate the matching
# config on MatchingContext from Settings; these literals are the in-code
# fallback so unit tests can construct a MatchingContext without wiring
# settings. Kept equal to the Settings defaults.
_FAMILY_MATCH_WEIGHT = 0.5
# Families that are NOT a relatedness signal: "other" is the LLM's catch-all
# junk bucket, "domain" lumps disparate domains under one label.
_NON_MATCHABLE_FAMILIES: tuple[str, ...] = ("other", "domain")
_LLM_CONCURRENCY = 4
_EVIDENCE_MAX_TOKENS = 2048


@dataclass(frozen=True)
class MatchingContext:
    db: asyncpg.Connection
    neo4j: AsyncDriver
    llm: LLMClient
    embedder: CachedEmbedder
    model_gen: str
    model_emb: str
    # Per-run matching tuning (ADR 0021); defaults mirror the constants above.
    family_weight: float = _FAMILY_MATCH_WEIGHT
    non_matchable_families: tuple[str, ...] = _NON_MATCHABLE_FAMILIES
    llm_concurrency: int = _LLM_CONCURRENCY
    evidence_max_tokens: int = _EVIDENCE_MAX_TOKENS
    # Build provenance (reviewer finding, Phase 4c): sourced from
    # ``settings.git_sha`` (never ``os.environ`` directly — see settings.py's
    # docstring invariant) and threaded into PipelineMeta.git_sha.
    git_sha: str | None = None


def matching_context_from_settings(
    settings: Settings,
    *,
    db: asyncpg.Connection,
    neo4j: AsyncDriver,
    llm: LLMClient,
    embedder: CachedEmbedder,
) -> MatchingContext:
    """Build a ``MatchingContext`` sourcing EVERY non-weight tunable from
    ``Settings`` (Phase 4d / ADR-009 REQUIREMENT 1 — the "Tunable-default
    duplication" residual).

    This is the SINGLE call site that populates ``family_weight`` /
    ``non_matchable_families`` / ``llm_concurrency`` / ``evidence_max_tokens`` /
    ``model_gen`` / ``model_emb`` / ``git_sha`` from settings rather than the
    dataclass-field defaults (which mirror ``orchestrator.py``'s module-level
    ``_FAMILY_MATCH_WEIGHT`` / ``_NON_MATCHABLE_FAMILIES`` / ``_LLM_CONCURRENCY``
    / ``_EVIDENCE_MAX_TOKENS`` literals). The worker tasks
    (``src.worker.matching_tasks``) call this with the live ``ctx`` deps so a
    non-default ``.env`` actually reaches the engine — 4c only proved the
    settings bridge (``weights_from_settings``) correct in isolation; nothing
    called it at a real construction site until here.
    """
    return MatchingContext(
        db=db,
        neo4j=neo4j,
        llm=llm,
        embedder=embedder,
        model_gen=settings.llm_model_generation,
        model_emb=settings.llm_model_embedding,
        family_weight=settings.match_family_weight,
        non_matchable_families=non_matchable_families_from_settings(settings),
        llm_concurrency=settings.match_llm_concurrency,
        evidence_max_tokens=settings.match_evidence_max_tokens,
        git_sha=settings.git_sha,
    )


@dataclass(frozen=True)
class Stage1Candidate:
    resume_id: UUID
    vec_score: float


@dataclass(frozen=True)
class Stage2Candidate:
    resume_id: UUID
    vec_score: float
    structured: float
    breakdown: ScoreBreakdown


@dataclass(frozen=True)
class ShortlistResultEntry:
    resume_id: UUID
    rank: int
    score_final: float
    score_structured: float
    score_evidence: float
    breakdown: ScoreBreakdown
    # security FINDING 5 — the WRITE boundary is typed with the STRICT ingest
    # model. ``persist_shortlist`` / ``persist_reverse_match`` read this field
    # straight into ``json.dumps``; with the tolerant ``EvidenceObject`` here,
    # an uncapped instance was type-legal all the way to Postgres and only the
    # accident that both producers funnel through ``_stage3_per_candidate``
    # prevented it. ``verify_evidence`` and ``stage4_combine`` are generic, so
    # ingest-ness survives the pipeline and this costs no cast.
    evidence: EvidenceObjectIngest | None


@dataclass(frozen=True)
class ShortlistResult:
    job_id: UUID
    entries: list[ShortlistResultEntry] = field(default_factory=list)
    pipeline_meta: PipelineMeta | None = None


@dataclass(frozen=True)
class JobMatchResultEntry:
    """One ranked job for a résumé (reverse match). Job-keyed twin of
    ShortlistResultEntry; requirement counts surface JD difficulty."""

    job_id: UUID
    title: str
    rank: int
    score_final: float
    score_structured: float
    score_evidence: float
    breakdown: ScoreBreakdown
    # security FINDING 5 — the WRITE boundary is typed with the STRICT ingest
    # model. ``persist_shortlist`` / ``persist_reverse_match`` read this field
    # straight into ``json.dumps``; with the tolerant ``EvidenceObject`` here,
    # an uncapped instance was type-legal all the way to Postgres and only the
    # accident that both producers funnel through ``_stage3_per_candidate``
    # prevented it. ``verify_evidence`` and ``stage4_combine`` are generic, so
    # ingest-ness survives the pipeline and this costs no cast.
    evidence: EvidenceObjectIngest | None
    requirement_count: int
    must_have_count: int


@dataclass(frozen=True)
class JobMatchResult:
    resume_id: UUID
    entries: list[JobMatchResultEntry] = field(default_factory=list)
    pipeline_meta: PipelineMeta | None = None


_PROMPT_VERSION_EVIDENCE = "shortlist_evidence_v1"
# v2 adds a cover-letter evidence block (Feature 1) — used only when the
# candidate has a cover letter, so résumés without one keep the v1 prompt.
_PROMPT_VERSION_EVIDENCE_V2 = "shortlist_evidence_v2"
_COARSE_K = 50
_EVIDENCE_K = 15
# Reverse match evidence default. recruiter-assistant has no synchronous
# reverse-match endpoint, so this inherits hris's worker-path default (> 0,
# blocker #10). Production callers pass settings.match_reverse_evidence_k.
_REVERSE_EVIDENCE_K = 10
# Combine weights for reverse match when evidence is skipped: rank purely on the
# structured score. Validator requires the top three to sum to 1.0.
_STRUCTURED_ONLY_WEIGHTS = MatchWeights(structured=1.0, evidence=0.0, motivation=0.0)


# ---------------- job loader ----------------


async def load_job_view(db: asyncpg.Connection, job_id: UUID) -> JobView | None:
    """Materialise the JD into the small struct stages need."""
    row = await db.fetchrow(
        "SELECT title, min_years, description_parsed FROM jobs WHERE id = $1", job_id
    )
    if row is None:
        return None
    parsed = row["description_parsed"]
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    parsed = parsed or {}
    edu = parsed.get("education") or {}
    return JobView(
        id=job_id,
        title=row["title"],
        min_years=row["min_years"] or parsed.get("min_years_experience"),
        education_min_level=edu.get("min_level"),
        education_fields=tuple(
            f for f in (edu.get("fields") or []) if isinstance(f, str) and f.strip()
        ),
        required_skills=tuple(
            s.get("name", "")
            for s in parsed.get("required_skills", [])
            if s.get("name")
        ),
        nice_to_have_skills=tuple(
            s.get("name", "")
            for s in parsed.get("nice_to_have_skills", [])
            if s.get("name")
        ),
    )


# ---------------- stage 1 ----------------


async def stage1_coarse(
    neo4j: AsyncDriver, job_id: UUID, *, k: int = _COARSE_K
) -> list[Stage1Candidate]:
    # Per-job scoping: the WHERE clause keeps the shortlist to resumes uploaded
    # for THIS job. We oversample 3x and filter, then bound the result to k.
    async with neo4j.session() as session:
        result = await session.run(
            """
            MATCH (j:Job {id: $jid})
            CALL db.index.vector.queryNodes(
                'resume_summary_idx', $oversample, j.summary_embedding
            )
            YIELD node AS r, score AS vec_score
            WHERE r.id IS NOT NULL AND r.job_id = $jid
            RETURN r.id AS resume_id, vec_score
            ORDER BY vec_score DESC
            LIMIT $k
            """,
            jid=str(job_id),
            k=k,
            oversample=k * 3,
        )
        rows = [dict(r) async for r in result]
    return [
        Stage1Candidate(resume_id=UUID(r["resume_id"]), vec_score=float(r["vec_score"]))
        for r in rows
    ]


# ---------------- stage 2 ----------------


async def _stage2_skill_rows(
    neo4j: AsyncDriver,
    job_id: UUID,
    resume_id: UUID,
    *,
    family_weight: float = _FAMILY_MATCH_WEIGHT,
    non_matchable_families: tuple[str, ...] = _NON_MATCHABLE_FAMILIES,
) -> list[_SkillRowFromCypher]:
    async with neo4j.session() as session:
        result = await session.run(
            """
            MATCH (j:Job {id: $jid})-[req:REQUIRES]->(reqSkill:Skill)
            OPTIONAL MATCH (r:Resume {id: $rid})-[has:HAS_SKILL]->(reqSkill)
            RETURN
              reqSkill.canonical_key AS skill,
              req.min_years           AS req_years,
              coalesce(req.is_must_have, true) AS is_must_have,
              has.years               AS years,
              has.last_used_year      AS last_used_year,
              CASE
                WHEN has IS NOT NULL THEN 1.0
                WHEN reqSkill.categories IS NOT NULL AND EXISTS {
                       MATCH (:Resume {id: $rid})-[:HAS_SKILL]->(c:Skill)
                       WHERE c.categories IS NOT NULL
                         AND any(t IN c.categories
                                 WHERE t IN reqSkill.categories
                                   AND NOT t IN $exclude_fam)
                     }
                THEN $family_weight
                ELSE 0.0
              END AS ontology_weight
            """,
            jid=str(job_id),
            rid=str(resume_id),
            family_weight=family_weight,
            exclude_fam=list(non_matchable_families),
        )
        rows = [dict(r) async for r in result]
    return [
        _SkillRowFromCypher(
            skill=r["skill"],
            req_years=r["req_years"],
            is_must_have=bool(r["is_must_have"]),
            years=r["years"],
            last_used_year=r["last_used_year"],
            ontology_weight=float(r["ontology_weight"]),
        )
        for r in rows
    ]


async def _stage2_per_candidate(
    ctx: MatchingContext,
    job: JobView,
    candidate: Stage1Candidate,
    vec_normalised: float,
    weights: MatchWeights,
) -> Stage2Candidate:
    # Pull resume.parsed first — experience + education + seniority AND the
    # implied-experience seniority gate (ADR 0027) all read off it.
    parsed_row = await ctx.db.fetchrow(
        "SELECT parsed FROM resumes WHERE id = $1", candidate.resume_id
    )
    parsed = parsed_row["parsed"] if parsed_row else None
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    parsed = parsed or {}
    total_years = parsed.get("total_years_experience")
    senior = is_senior_candidate(total_years, job.min_years, weights=weights)

    # Skill — REQUIRES edges only (blocker #5: nice-to-haves never feed this).
    skill_rows = await _stage2_skill_rows(
        ctx.neo4j,
        job.id,
        candidate.resume_id,
        family_weight=ctx.family_weight,
        non_matchable_families=ctx.non_matchable_families,
    )
    skill_overall, skill_contribs = score_skill_breakdown(
        skill_rows, weights=weights, senior=senior
    )

    exp = score_experience(total_years, job.min_years, weights=weights)

    # Build levels AND fields from the SAME iteration to guarantee index
    # alignment (degree i's level pairs with degree i's field).
    edu_entries = parsed.get("education", []) or []
    candidate_levels = [_level_from_degree(e.get("degree")) for e in edu_entries]
    candidate_fields = [e.get("field") for e in edu_entries]
    edu = score_education(
        candidate_levels,
        job.education_min_level,
        candidate_fields=candidate_fields,
        jd_fields=job.education_fields,
        weights=weights,
    )

    # Seniority: cosine between job title + most-recent role title via embedder.
    recent_title = _most_recent_title(parsed)
    if recent_title:
        embs = await ctx.embedder.embed([job.title, recent_title])
        seniority = _cosine(embs[0], embs[1])
        # Normalise [seniority_floor, 1.0] → [0, 1].
        floor = weights.seniority_floor
        seniority = max(0.0, min(1.0, (seniority - floor) / (1.0 - floor)))
    else:
        seniority = 0.0

    breakdown = ScoreBreakdown(
        skill=skill_overall,
        experience=exp,
        education=edu,
        seniority=seniority,
        vector=vec_normalised,
        structured=(
            weights.skill * skill_overall
            + weights.experience * exp
            + weights.education * edu
            + weights.seniority * seniority
            + weights.vector * vec_normalised
        ),
        implied_experience=any(
            c.reason == "implied-experience" for c in skill_contribs
        ),
        skill_contributions=skill_contribs,
    )
    return Stage2Candidate(
        resume_id=candidate.resume_id,
        vec_score=candidate.vec_score,
        structured=breakdown.structured,
        breakdown=breakdown,
    )


_DEGREE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("phd", ("phd", "doctor")),
    ("masters", ("master", "msc", "mba", "ma ")),
    ("bachelors", ("bachelor", "bsc", "ba ", "bs ", "bfa")),
    ("associate", ("associate",)),
    ("high_school", ("high school",)),
)


def _level_from_degree(degree: str | None) -> str | None:
    if not degree:
        return None
    d = degree.lower()
    for level, keywords in _DEGREE_KEYWORDS:
        if any(k in d for k in keywords):
            return level
    return None


def _most_recent_title(parsed: dict[str, Any]) -> str | None:
    roles = parsed.get("experience") or []
    if not roles:
        return None
    # Trust "is_current" first; otherwise the first item.
    current = [r for r in roles if r.get("is_current")]
    role = current[0] if current else roles[0]
    title = role.get("title")
    return str(title) if title else None


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return float(dot / (na * nb))


# ---------------- stage 3 ----------------


async def _fetch_parsed(db: asyncpg.Connection, resume_id: UUID) -> dict[str, Any]:
    """Fetch + decode a résumé's ``parsed`` jsonb (blocker #7: chunk text lives
    here, not the outbox). Callers MUST run this BEFORE the concurrent stage-3
    fan-out: ``ctx.db`` is a single asyncpg connection and asyncpg forbids
    concurrent operations on one connection."""
    row = await db.fetchrow("SELECT parsed FROM resumes WHERE id = $1", resume_id)
    parsed = row["parsed"] if row else None
    if isinstance(parsed, str):
        parsed = json.loads(parsed)
    return parsed if isinstance(parsed, dict) else {}


async def _stage3_per_candidate(
    ctx: MatchingContext,
    job: JobView,
    candidate: Stage2Candidate,
    parsed: dict[str, Any],
    *,
    weights: MatchWeights = DEFAULT_WEIGHTS,
) -> EvidenceObjectIngest | None:
    # ``parsed`` is fetched by the caller before the concurrent fan-out.
    chunks = parsed.get("chunks") or []
    if not chunks or not job.required_skills:
        return None

    # Cover letter (Feature 1): when present, use the v2 prompt and merge both
    # chunk namespaces into the verifier so fabricated ids in either are scrubbed.
    cl_chunks = parsed.get("cover_letter_chunks") or []
    has_cover = bool(cl_chunks)

    # blocker #5: nice-to-haves surface in the EVIDENCE requirements list, but
    # not the stage-2 structured skill sub-score.
    requirements = list(job.required_skills) + list(job.nice_to_have_skills)
    prompt = load_prompt(
        _PROMPT_VERSION_EVIDENCE_V2 if has_cover else _PROMPT_VERSION_EVIDENCE,
        job_title=job.title,
        requirements=requirements,
        chunks=chunks,
        cover_letter_chunks=cl_chunks,
    )
    try:
        # THE ingest boundary. ``EvidenceObjectIngest`` is the strict variant
        # and must not be swapped for the tolerant ``EvidenceObject`` used
        # everywhere downstream: this is the only place the size caps are
        # applied, and they scrub the offending field rather than raising, so
        # one over-long quote no longer drops us into the except-branch below
        # and costs this candidate ALL of their evidence.
        evidence = await ctx.llm.chat_json(
            prompt.messages,
            EvidenceObjectIngest,
            max_tokens=ctx.evidence_max_tokens,
            max_retries=1,
        )
    except LLMOutputInvalidError as exc:
        log.warning(
            "stage3.llm_invalid resume_id=%s error=%s",
            str(candidate.resume_id),
            str(exc),
        )
        return None

    chunks_by_id = {c["id"]: c["text"] for c in chunks if c.get("id")}
    if has_cover:
        chunks_by_id.update({c["id"]: c["text"] for c in cl_chunks if c.get("id")})
    return verify_evidence(evidence, chunks_by_id, weights=weights)


async def stage3_evidence(
    ctx: MatchingContext,
    job: JobView,
    top_k: list[Stage2Candidate],
    *,
    weights: MatchWeights = DEFAULT_WEIGHTS,
) -> dict[UUID, EvidenceObjectIngest | None]:
    """Run LLM evidence extraction with a concurrency cap."""
    sem = asyncio.Semaphore(ctx.llm_concurrency)
    results: dict[UUID, EvidenceObjectIngest | None] = {}

    # Pre-fetch each candidate's parsed row SEQUENTIALLY before the fan-out —
    # the shared asyncpg connection can't be queried concurrently inside gather.
    parsed_by_id: dict[UUID, dict[str, Any]] = {}
    for c in top_k:
        if c.resume_id not in parsed_by_id:
            parsed_by_id[c.resume_id] = await _fetch_parsed(ctx.db, c.resume_id)

    async def _one(candidate: Stage2Candidate) -> None:
        async with sem:
            try:
                results[candidate.resume_id] = await _stage3_per_candidate(
                    ctx,
                    job,
                    candidate,
                    parsed_by_id[candidate.resume_id],
                    weights=weights,
                )
            except Exception:  # noqa: BLE001 — one candidate must not sink all
                log.exception("stage3.failed resume_id=%s", str(candidate.resume_id))
                results[candidate.resume_id] = None

    await asyncio.gather(*(_one(c) for c in top_k))
    return results


# ---------------- orchestrator ----------------


def _apply_top_percent_cap(
    entries: Sequence[ShortlistResultEntry], top_percent: int
) -> list[ShortlistResultEntry]:
    """Prefix-slice a rank-ordered ``entries`` list (rank 1 first) down to the
    top ``top_percent``% — ``n_keep = ceil(len(entries) * top_percent / 100)``,
    floored at 1 for a non-empty pool (never 0, which would silently produce
    an empty shortlist from a nonzero percent), but exactly 0 when ``entries``
    is empty (a floor of 1 there would fabricate a candidate out of nothing).
    Never re-sorts — the caller already hands this a rank-ordered list."""
    if not entries:
        return []
    n_keep = max(1, math.ceil(len(entries) * top_percent / 100))
    return list(entries[:n_keep])


async def generate_shortlist(
    job_id: UUID,
    ctx: MatchingContext,
    *,
    weights: MatchWeights = DEFAULT_WEIGHTS,
    coarse_k: int = _COARSE_K,
    evidence_k: int = _EVIDENCE_K,
    top_percent: int = 100,
) -> ShortlistResult:
    started = dt.datetime.now(dt.UTC)
    timings: dict[str, int] = {}

    job = await load_job_view(ctx.db, job_id)
    if job is None:
        return ShortlistResult(job_id=job_id)

    # Stage 1
    t = dt.datetime.now(dt.UTC)
    candidates_s1 = await stage1_coarse(ctx.neo4j, job_id, k=coarse_k)
    timings["stage1_ms"] = _ms_since(t)

    if not candidates_s1:
        log.info("shortlist.no_candidates job_id=%s", str(job_id))
        return ShortlistResult(
            job_id=job_id,
            pipeline_meta=_shortlist_meta(ctx, weights, started, timings),
        )

    # Stage 2 — per-candidate (sequential; each does its own DB calls)
    t = dt.datetime.now(dt.UTC)
    vec_normalised = normalise_vector_scores([c.vec_score for c in candidates_s1])
    candidates_s2: list[Stage2Candidate] = []
    for c, vn in zip(candidates_s1, vec_normalised, strict=True):
        s2 = await _stage2_per_candidate(ctx, job, c, vn, weights)
        candidates_s2.append(s2)
    candidates_s2.sort(key=lambda c: c.structured, reverse=True)
    timings["stage2_ms"] = _ms_since(t)

    # Stage 3 — top-K evidence
    t = dt.datetime.now(dt.UTC)
    top_k = candidates_s2[:evidence_k]
    evidence_by_id = await stage3_evidence(ctx, job, top_k, weights=weights)
    timings["stage3_ms"] = _ms_since(t)

    # Stage 4 — combine + rank
    t = dt.datetime.now(dt.UTC)
    combine_in: list[_CombineInput[EvidenceObjectIngest]] = [
        _CombineInput(
            resume_id=c.resume_id,
            structured=c.structured,
            breakdown=c.breakdown,
            evidence=evidence_by_id.get(c.resume_id),
        )
        for c in candidates_s2
    ]
    combined = stage4_combine(combine_in, weights)
    timings["stage4_ms"] = _ms_since(t)

    entries = [
        ShortlistResultEntry(
            resume_id=e.resume_id,
            rank=e.rank,
            score_final=e.score_final,
            score_structured=e.score_structured,
            score_evidence=e.score_evidence,
            breakdown=e.breakdown,
            evidence=e.evidence,
        )
        for e in combined
    ]
    entries = _apply_top_percent_cap(entries, top_percent)

    return ShortlistResult(
        job_id=job_id,
        entries=entries,
        pipeline_meta=_shortlist_meta(ctx, weights, started, timings),
    )


def _shortlist_meta(
    ctx: MatchingContext,
    weights: MatchWeights,
    started: dt.datetime,
    timings: dict[str, int],
) -> PipelineMeta:
    return PipelineMeta(
        model_gen=ctx.model_gen,
        model_emb=ctx.model_emb,
        prompt_versions={"shortlist_evidence": _PROMPT_VERSION_EVIDENCE},
        weights=weights,
        git_sha=ctx.git_sha,
        generated_at=started,
        timings_ms=timings,
    )


# ---------------- reverse match (résumé → jobs) ----------------


async def stage1_coarse_jobs(
    neo4j: AsyncDriver, resume_id: UUID, *, k: int = _COARSE_K
) -> list[tuple[UUID, float]]:
    """Inverted stage 1: query the job_summary_idx with a résumé's summary
    embedding to get candidate Job ids ranked by similarity. Returns [] when the
    résumé has no graph node / no embedding. Deliberately NOT job-scoped — the
    whole point is cross-job."""
    async with neo4j.session() as session:
        emb_result = await session.run(
            "MATCH (r:Resume {id: $rid}) RETURN r.summary_embedding AS emb",
            rid=str(resume_id),
        )
        emb_rec = await emb_result.single()
        if emb_rec is None or emb_rec["emb"] is None:
            return []
        result = await session.run(
            """
            CALL db.index.vector.queryNodes('job_summary_idx', $k, $emb)
            YIELD node AS j, score AS vec_score
            WHERE j.id IS NOT NULL
            RETURN j.id AS job_id, vec_score
            ORDER BY vec_score DESC
            LIMIT $k
            """,
            k=k,
            emb=emb_rec["emb"],
        )
        rows = [dict(r) async for r in result]
    return [(UUID(r["job_id"]), float(r["vec_score"])) for r in rows]


async def match_resume_to_jobs(
    resume_id: UUID,
    ctx: MatchingContext,
    *,
    allowed_job_ids: set[UUID] | None = None,
    weights: MatchWeights = DEFAULT_WEIGHTS,
    coarse_k: int = _COARSE_K,
    evidence_k: int = _REVERSE_EVIDENCE_K,
) -> JobMatchResult:
    """Reverse match: rank candidate jobs for one résumé by reusing stages 2-4
    against an inverted stage 1. ``allowed_job_ids`` filters the candidate jobs
    (None = no filter). Structured sub-scores are [0,1] ratios against each JD's
    own requirements, so they're comparable across heterogeneous JDs; JD
    difficulty is surfaced via requirement counts, not a hidden re-weight."""
    started = dt.datetime.now(dt.UTC)
    timings: dict[str, int] = {}

    def _meta() -> PipelineMeta:
        return PipelineMeta(
            model_gen=ctx.model_gen,
            model_emb=ctx.model_emb,
            prompt_versions={"shortlist_evidence": _PROMPT_VERSION_EVIDENCE},
            weights=weights,
            git_sha=ctx.git_sha,
            generated_at=started,
            timings_ms=timings,
        )

    t = dt.datetime.now(dt.UTC)
    candidates = await stage1_coarse_jobs(ctx.neo4j, resume_id, k=coarse_k)
    if allowed_job_ids is not None:
        candidates = [(jid, vs) for jid, vs in candidates if jid in allowed_job_ids]
    timings["stage1_ms"] = _ms_since(t)
    if not candidates:
        return JobMatchResult(resume_id=resume_id, entries=[], pipeline_meta=_meta())

    # Stage 2 — structured score per candidate job.
    t = dt.datetime.now(dt.UTC)
    vec_normalised = normalise_vector_scores([vs for _, vs in candidates])
    scored: list[tuple[JobView, Stage2Candidate]] = []
    for (jid, vs), vn in zip(candidates, vec_normalised, strict=True):
        job = await load_job_view(ctx.db, jid)
        if job is None:
            continue
        s2 = await _stage2_per_candidate(
            ctx, job, Stage1Candidate(resume_id=resume_id, vec_score=vs), vn, weights
        )
        scored.append((job, s2))
    scored.sort(key=lambda pair: pair[1].structured, reverse=True)
    timings["stage2_ms"] = _ms_since(t)

    # Stage 3 — LLM evidence for the top few jobs only (latency cap).
    t = dt.datetime.now(dt.UTC)
    sem = asyncio.Semaphore(ctx.llm_concurrency)
    evidence_by_job: dict[UUID, EvidenceObjectIngest | None] = {}
    # Reverse match scores ONE résumé against many jobs — fetch its parsed row
    # ONCE before the concurrent fan-out (shared asyncpg connection).
    resume_parsed = await _fetch_parsed(ctx.db, resume_id)

    async def _evidence(job: JobView, s2: Stage2Candidate) -> None:
        async with sem:
            try:
                evidence_by_job[job.id] = await _stage3_per_candidate(
                    ctx, job, s2, resume_parsed, weights=weights
                )
            except Exception:  # noqa: BLE001 — one job must not sink all
                log.exception("reverse.stage3.failed job_id=%s", str(job.id))
                evidence_by_job[job.id] = None

    await asyncio.gather(*(_evidence(job, s2) for job, s2 in scored[:evidence_k]))
    timings["stage3_ms"] = _ms_since(t)

    # Stage 4 — combine + rank.
    t = dt.datetime.now(dt.UTC)
    scores = [
        _JobScore(
            job=job,
            structured=s2.structured,
            breakdown=s2.breakdown,
            evidence=evidence_by_job.get(job.id),
        )
        for job, s2 in scored
    ]
    # With evidence skipped, rank on structured fit alone; with evidence on, use
    # the caller's weights as usual.
    combine_weights = weights if evidence_k > 0 else _STRUCTURED_ONLY_WEIGHTS
    entries = rank_job_matches(scores, combine_weights)
    timings["stage4_ms"] = _ms_since(t)

    return JobMatchResult(resume_id=resume_id, entries=entries, pipeline_meta=_meta())


@dataclass(frozen=True)
class _JobScore:
    """Input to rank_job_matches: a candidate job's structured score +
    (optional) evidence."""

    job: JobView
    structured: float
    breakdown: ScoreBreakdown
    # security FINDING 5 — the WRITE boundary is typed with the STRICT ingest
    # model. ``persist_shortlist`` / ``persist_reverse_match`` read this field
    # straight into ``json.dumps``; with the tolerant ``EvidenceObject`` here,
    # an uncapped instance was type-legal all the way to Postgres and only the
    # accident that both producers funnel through ``_stage3_per_candidate``
    # prevented it. ``verify_evidence`` and ``stage4_combine`` are generic, so
    # ingest-ness survives the pipeline and this costs no cast.
    evidence: EvidenceObjectIngest | None


def rank_job_matches(
    scores: list[_JobScore], weights: MatchWeights = DEFAULT_WEIGHTS
) -> list[JobMatchResultEntry]:
    """Pure combine + rank for reverse match: score_final =
    ``weights.structured·structured + weights.evidence·evidence_completeness``,
    sorted descending. Requirement counts are carried through."""

    def _final(s: _JobScore) -> float:
        return (
            weights.structured * s.structured
            + weights.evidence * _evidence_completeness(s.evidence, weights=weights)
        )

    ranked = sorted(scores, key=_final, reverse=True)
    return [
        JobMatchResultEntry(
            job_id=s.job.id,
            title=s.job.title,
            rank=i + 1,
            score_final=_final(s),
            score_structured=s.structured,
            score_evidence=_evidence_completeness(s.evidence, weights=weights),
            breakdown=s.breakdown,
            evidence=s.evidence,
            requirement_count=len(s.job.required_skills)
            + len(s.job.nice_to_have_skills),
            must_have_count=len(s.job.required_skills),
        )
        for i, s in enumerate(ranked)
    ]


# ---------------- in-memory stage-4 ranker (eval harness entrypoint) ----------


@dataclass(frozen=True)
class RankInput:
    """One already-scored candidate handed to :func:`run_match` — the eval
    harness (``tests/evals/run_evals.py``) builds these from fixtures via the
    same pure stage functions the DB path uses. ``resume_id`` is a free-form
    string so a fixture id (not a UUID) can flow straight through."""

    resume_id: str
    structured: float
    breakdown: ScoreBreakdown
    evidence: EvidenceObject | None


@dataclass(frozen=True)
class RankedMatch:
    resume_id: str
    rank: int
    score_final: float
    score_structured: float
    score_evidence: float
    breakdown: ScoreBreakdown
    evidence: EvidenceObject | None


def run_match(
    inputs: Sequence[RankInput], weights: MatchWeights = DEFAULT_WEIGHTS
) -> list[RankedMatch]:
    """Stage-4 combine + rank over already-scored candidates (the pipeline
    entrypoint the eval harness wires). Identical arithmetic to
    :func:`stage4_combine`, but string-keyed so a fixture id flows through and
    the deterministic motivation sub-score is surfaced onto ``breakdown``."""
    scored: list[RankedMatch] = []
    for c in inputs:
        completeness = _evidence_completeness(c.evidence, weights=weights)
        motivation = _motivation_score(c.evidence, weights=weights)
        final = (
            weights.structured * c.structured
            + weights.evidence * completeness
            + weights.motivation * motivation
        )
        breakdown = c.breakdown.model_copy(update={"motivation": motivation})
        scored.append(
            RankedMatch(
                resume_id=c.resume_id,
                rank=0,
                score_final=final,
                score_structured=c.structured,
                score_evidence=completeness,
                breakdown=breakdown,
                evidence=c.evidence,
            )
        )
    scored.sort(key=lambda m: m.score_final, reverse=True)
    return [
        RankedMatch(
            resume_id=m.resume_id,
            rank=i + 1,
            score_final=m.score_final,
            score_structured=m.score_structured,
            score_evidence=m.score_evidence,
            breakdown=m.breakdown,
            evidence=m.evidence,
        )
        for i, m in enumerate(scored)
    ]


def _ms_since(t: dt.datetime) -> int:
    delta = dt.datetime.now(dt.UTC) - t
    return int(delta.total_seconds() * 1000)
