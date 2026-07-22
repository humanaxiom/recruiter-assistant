"""Matching / ranking schemas — ported from hris
``packages/schemas/src/schemas/matching.py``, with the review workflow CUT.

The 2nd-review pipeline (``PipelineStage``, ``DispositionReason``,
``ShortlistDecision*``, ``StageTransition*``, ``DecisionKind``,
``TERMINAL_STAGES``) is not part of recruiter-assistant and is deliberately
absent — ``ShortlistEntry`` keeps only the blind-review fields
(``blinded`` / ``display_label``), not ``current_decision`` / ``current_stage``.

``MatchWeights`` is the ranking contract: its defaults and the
``_sums_close_to_one`` validator encode the plan's algorithm
(``0.6·structured + 0.3·evidence + 0.1·motivation``; skill/exp/edu/sen/vector
sub-weights; the ``0.85`` anti-fabrication fuzz threshold).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

EvidenceStatus = Literal["met", "partial", "missing"]

# ADR-022 follow-up #2 — C0 controls (plus DEL) that must never reach the
# ``json.dumps`` in ``persist_shortlist``. Postgres rejects the JSON escape
# for U+0000 outright ("unsupported Unicode escape sequence"), which kills the
# whole shortlist transaction; the rest are junk in a human-facing quote and
# go under the same rule. TAB (0x09), LF (0x0a) and CR (0x0d) carry real
# formatting in a multi-line quote and are deliberately NOT in the class.
#
# This generalises the NUL-strip idiom already established on the parse path
# (``src/pipeline/parsing/extract.py::_sanitize``) rather than inventing a
# second one. It lives at the SCHEMA boundary, not in ``verify_evidence``:
# the verifier only ever rewrites the two ``evidence`` fields, so
# ``requirement`` / ``overall_summary`` / ``overall_motivation`` would still
# reach Postgres unscrubbed.
#
# It is also not redundant with ``pipeline/llm/client.py::_strip_nuls``, which
# ALREADY recursively strips U+0000 from parsed LLM JSON before validation —
# so the specific byte Postgres rejects is, on the ``chat_json`` path, handled
# twice. Stating the justification that way makes it stronger, not weaker:
# this layer earns its place by being wider on BOTH axes. It removes the other
# C0 controls (invisible junk in a human-facing quote, which ``_strip_nuls``
# leaves alone), and it holds for every caller that never goes through the LLM
# client at all — read-path revalidation, tests, and any future non-LLM
# producer of an evidence object. Defence in depth with the outer layer
# strictly containing the inner one, not two copies of the same check.
_C0_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_control_chars(value: str) -> str:
    """Drop C0 controls and DEL, keeping TAB / LF / CR. No-op on clean text."""
    return _C0_CONTROLS.sub("", value)


# Every LLM-authored free-text field on the evidence models carries this, so
# the scrub holds on ``__init__`` and ``model_validate`` alike and is a fixed
# point across a dump/validate roundtrip.
CleanText = Annotated[str, AfterValidator(_strip_control_chars)]

# ── ADR-022 follow-up #3 — evidence size caps ───────────────────────────────
#
# HUMAN DECISION (reviewer round 2): the READ path is TOLERANT, INGEST is
# STRICT. A cap prevents a bad WRITE; once the bytes are on disk it buys no
# protection whatsoever and only breaks retrieval. This project has no
# migration framework, and ``shortlist_service`` validates stored JSONB with
# an uncaught ``model_validate``, so a cap on the read model turns any
# pre-existing over-cap row into a 500 for the whole job — making exactly the
# pathological output the cap targets PERMANENTLY UNREADABLE.
#
# So the three evidence models below carry NO length constraints and are what
# the DTOs, the read path and ``verify_evidence`` use. The ``*Ingest``
# subclasses at the bottom of this module carry the caps, and are wired at
# exactly one place: the ``chat_json`` call in
# ``pipeline/matching/orchestrator.py::_stage3_per_candidate``.
#
# The ingest caps SCRUB rather than raise, for the same reason
# ``verify_evidence`` scrubs per-requirement instead of rejecting wholesale:
# one over-long quote must not cost a candidate every OTHER requirement's
# evidence, which is what a raising cap did (the object failed validation, so
# ``_stage3_per_candidate`` returned ``None``).
MAX_EVIDENCE_QUOTE_CHARS = 2000
MAX_REQUIREMENT_CHARS = 500
MAX_EVIDENCE_CHUNK_IDS = 8
MAX_REQUIREMENTS = 64
MAX_OVERALL_TEXT_CHARS = 1000

# Confidence ceiling applied to any requirement/theme whose quote was scrubbed.
# Shared with ``pipeline/matching/stages.py::verify_evidence`` so the ingest
# drop and the anti-fabrication scrub cannot drift apart.
SCRUBBED_CONFIDENCE_CAP = 0.3

# A skill family/category label. Free-form string, resolved against the
# config-driven ontology vocabulary at scoring time (not an enum).
SkillCategory = str


class MatchWeights(BaseModel):
    """Top-level + sub-score weights. Sub-weights must sum to ~1.0."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    structured: float = Field(default=0.6, ge=0, le=1)
    evidence: float = Field(default=0.3, ge=0, le=1)
    motivation: float = Field(default=0.1, ge=0, le=1)
    skill: float = Field(default=0.40, ge=0, le=1)
    experience: float = Field(default=0.25, ge=0, le=1)
    education: float = Field(default=0.10, ge=0, le=1)
    seniority: float = Field(default=0.15, ge=0, le=1)
    vector: float = Field(default=0.10, ge=0, le=1)
    must_have_miss_penalty: float = Field(default=0.5, ge=0, le=1)
    implied_experience_relief: float = Field(default=0.75, ge=0, le=1)
    recency_recent_years: int = Field(default=2, ge=0)
    recency_mid_years: int = Field(default=5, ge=0)
    recency_recent: float = Field(default=1.0, ge=0, le=1)
    recency_mid: float = Field(default=0.7, ge=0, le=1)
    recency_old: float = Field(default=0.4, ge=0, le=1)
    overqual_ratio: float = Field(default=2.0, ge=1)
    overqual_slope: float = Field(default=0.1, ge=0)
    overqual_floor: float = Field(default=0.8, ge=0, le=1)
    education_partial: float = Field(default=0.5, ge=0, le=1)
    seniority_floor: float = Field(default=0.5, ge=0, lt=1)
    implied_seniority_factor: float = Field(default=1.5, ge=1)
    implied_min_coverage: float = Field(default=0.5, ge=0, le=1)
    evidence_met_confidence: float = Field(default=0.7, ge=0, le=1)
    evidence_partial_weight: float = Field(default=0.5, ge=0, le=1)
    evidence_verify_fuzz: float = Field(default=0.85, ge=0, le=1)
    # ADR-022 follow-up #4: a quote shorter than this many characters is not
    # evidence of anything — "API" matches any chunk containing it at 1.000.
    evidence_min_quote_chars: int = Field(default=32, ge=0)
    motivation_min_confidence: float = Field(default=0.7, ge=0, le=1)

    @model_validator(mode="after")
    def _sums_close_to_one(self) -> MatchWeights:
        top = self.structured + self.evidence + self.motivation
        sub = (
            self.skill + self.experience + self.education + self.seniority + self.vector
        )
        if abs(top - 1.0) > 0.01:
            raise ValueError(
                f"structured+evidence+motivation must sum to 1.0 (got {top:.3f})"
            )
        if abs(sub - 1.0) > 0.01:
            raise ValueError(
                "skill+experience+education+seniority+vector must sum to 1.0 "
                f"(got {sub:.3f})"
            )
        if self.implied_experience_relief < self.must_have_miss_penalty:
            raise ValueError(
                "implied_experience_relief must be >= must_have_miss_penalty "
                "(relief is a softer penalty): got "
                f"relief={self.implied_experience_relief:.3f} < "
                f"penalty={self.must_have_miss_penalty:.3f}"
            )
        return self


DEFAULT_WEIGHTS = MatchWeights()


class SkillContribution(BaseModel):
    """One row in the skill breakdown: per required skill."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    score: float = Field(ge=0, le=1)
    years: int | None = None
    recency: float | None = None
    ontology_weight: float | None = None
    is_must_have: bool = False
    reason: str | None = None  # "missing", "ontology-fallback", etc.


class ScoreBreakdown(BaseModel):
    """All sub-scores for one candidate. Stored verbatim in
    shortlist_entries.score_breakdown (jsonb)."""

    model_config = ConfigDict(extra="forbid")

    skill: float = Field(ge=0, le=1)
    experience: float = Field(ge=0, le=1)
    education: float = Field(ge=0, le=1)
    seniority: float = Field(ge=0, le=1)
    vector: float = Field(ge=0, le=1)
    structured: float = Field(ge=0, le=1)
    motivation: float = Field(default=0.0, ge=0, le=1)
    implied_experience: bool = False
    skill_contributions: list[SkillContribution] = Field(default_factory=list)


class RequirementEvidence(BaseModel):
    """TOLERANT read/DTO model. Deliberately carries no length caps — see the
    ingest/read note above. ``RequirementEvidenceIngest`` is the strict one."""

    model_config = ConfigDict(extra="ignore")

    requirement: CleanText
    status: EvidenceStatus = "missing"
    evidence: CleanText = ""
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    # FU-2 (display-only): the resolved source text behind ``evidence_chunk_ids``,
    # expanded from ``resumes.parsed`` at read/export time. Redacted under blind
    # review, exactly like ``evidence``. Populated only on the display paths; the
    # LLM never emits it, so at write time it is always ``None`` and persists as
    # JSONB ``null`` — a pure display expansion, never a stored value.
    source_context: str | None = None


CoverLetterTheme = Literal["motivation", "role_alignment", "cultural_fit", "growth"]


class CoverLetterEvidence(BaseModel):
    """One cover-letter theme with a cited quote (Feature 1).

    TOLERANT read/DTO model — no length caps, by the same decision as
    ``RequirementEvidence``."""

    model_config = ConfigDict(extra="ignore")

    theme: CoverLetterTheme
    evidence: CleanText = ""
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)


class EvidenceObject(BaseModel):
    """Evidence for one candidate: the READ/DTO shape.

    This is what ``shortlist_service`` validates stored JSONB with, what the
    ``ShortlistEntry`` / ``JobMatchEntry`` DTOs hold, and what
    ``verify_evidence`` takes and returns. It must accept anything ever
    written, so it declares no length caps. The LLM ingest boundary uses
    ``EvidenceObjectIngest``."""

    model_config = ConfigDict(extra="ignore")

    requirements: list[RequirementEvidence] = Field(default_factory=list)
    overall_summary: CleanText = ""
    cover_letter_presence: bool = False
    cover_letter_evidence: list[CoverLetterEvidence] = Field(default_factory=list)
    overall_motivation: CleanText = ""


# ── STRICT ingest variants — LLM boundary ONLY ──────────────────────────────
#
# Never use these on a read path. They subclass their tolerant counterparts so
# variance runs one way only: an ingest instance is accepted everywhere a read
# instance is (``verify_evidence``, the DTOs, ``persist_shortlist``), and a
# read instance can never stand in for an ingest one.


class RequirementEvidenceIngest(RequirementEvidence):
    """``RequirementEvidence`` with the size caps enforced by scrubbing."""

    @model_validator(mode="after")
    def _enforce_ingest_caps(self) -> RequirementEvidenceIngest:
        if len(self.requirement) > MAX_REQUIREMENT_CHARS:
            # The label is the row's KEY, not evidence of anything, so it is
            # trimmed rather than dropped — blanking it would orphan the row.
            self.requirement = self.requirement[:MAX_REQUIREMENT_CHARS]
        if len(self.evidence_chunk_ids) > MAX_EVIDENCE_CHUNK_IDS:
            self.evidence_chunk_ids = self.evidence_chunk_ids[:MAX_EVIDENCE_CHUNK_IDS]
        if len(self.evidence) > MAX_EVIDENCE_QUOTE_CHARS:
            # DROP the quote, never truncate it. A truncated superset-bypass
            # quote can still contain the cited chunk verbatim in its prefix
            # and would then verify at 1.000 — trimming would manufacture the
            # fabrication the guard exists to catch. Demote exactly as
            # ``verify_evidence`` does: a blanked-but-still-``met`` row would
            # keep full credit in ``_evidence_completeness``, which scores on
            # status and confidence and never looks at the quote text.
            self.evidence = ""
            if self.status == "met":
                self.status = "missing"
            self.confidence = min(self.confidence, SCRUBBED_CONFIDENCE_CAP)
        return self


class CoverLetterEvidenceIngest(CoverLetterEvidence):
    """``CoverLetterEvidence`` with the size caps enforced by scrubbing."""

    @model_validator(mode="after")
    def _enforce_ingest_caps(self) -> CoverLetterEvidenceIngest:
        if len(self.evidence_chunk_ids) > MAX_EVIDENCE_CHUNK_IDS:
            self.evidence_chunk_ids = self.evidence_chunk_ids[:MAX_EVIDENCE_CHUNK_IDS]
        if len(self.evidence) > MAX_EVIDENCE_QUOTE_CHARS:
            self.evidence = ""
            self.confidence = min(self.confidence, SCRUBBED_CONFIDENCE_CAP)
        return self


class EvidenceObjectIngest(EvidenceObject):
    """LLM-output schema for shortlist_evidence_v1/v2 — the ``chat_json``
    target, and the ONLY place the evidence size caps are enforced."""

    @model_validator(mode="before")
    @classmethod
    def _bound_lists_before_item_validation(cls, data: Any) -> Any:
        """Slice the raw lists BEFORE pydantic validates their items.

        This is what actually bounds the DoS. The previous ``max_length``
        constraint did not: pydantic validates every item first and only then
        checks the length, so a 100,000-entry list of 2,000,000-char quotes
        ran the per-item scrub 100,000 times before raising.
        """
        if isinstance(data, dict):
            for key in ("requirements", "cover_letter_evidence"):
                value = data.get(key)
                if isinstance(value, list) and len(value) > MAX_REQUIREMENTS:
                    data = {**data, key: value[:MAX_REQUIREMENTS]}
        return data

    @model_validator(mode="after")
    def _enforce_ingest_caps(self) -> EvidenceObjectIngest:
        # Re-validate the children through their strict variants: the field
        # annotations are deliberately NOT narrowed (that would be an
        # invariant-list override), so the per-item caps are applied here.
        reqs: list[RequirementEvidence] = [
            RequirementEvidenceIngest.model_validate(r.model_dump())
            for r in self.requirements[:MAX_REQUIREMENTS]
        ]
        self.requirements = reqs
        themes: list[CoverLetterEvidence] = [
            CoverLetterEvidenceIngest.model_validate(c.model_dump())
            for c in self.cover_letter_evidence[:MAX_REQUIREMENTS]
        ]
        self.cover_letter_evidence = themes
        self.overall_summary = self.overall_summary[:MAX_OVERALL_TEXT_CHARS]
        self.overall_motivation = self.overall_motivation[:MAX_OVERALL_TEXT_CHARS]
        return self


class PipelineMeta(BaseModel):
    """Reproducibility stamp written to every shortlist_entries row."""

    model_config = ConfigDict(extra="forbid")

    model_gen: str
    model_emb: str
    prompt_versions: dict[str, str]
    weights: MatchWeights
    git_sha: str | None = None
    generated_at: dt.datetime
    timings_ms: dict[str, int] = Field(default_factory=dict)


class ShortlistEntry(BaseModel):
    """One row in /jobs/{id}/shortlist.

    The review-workflow fields (``current_decision`` / ``current_stage``) are
    CUT; ``blinded`` / ``display_label`` are blind-review (v1 scope) and stay.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    job_id: UUID
    resume_id: UUID
    rank: int
    score_final: float
    score_breakdown: ScoreBreakdown
    evidence: EvidenceObject | None
    generated_at: dt.datetime
    blinded: bool = False
    display_label: str | None = None


class JobMatchEntry(BaseModel):
    """One ranked job for a résumé in the reverse match (match-jobs)."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    title: str
    department: str | None = None
    rank: int
    score_final: float
    score_structured: float
    score_evidence: float
    score_breakdown: ScoreBreakdown
    evidence: EvidenceObject | None = None
    requirement_count: int
    must_have_count: int


class JobMatchResultOut(BaseModel):
    """Response for GET /resumes/{id}/match-results."""

    model_config = ConfigDict(extra="forbid")

    resume_id: UUID
    entries: list[JobMatchEntry] = Field(default_factory=list)
    pipeline_meta: PipelineMeta | None = None
    generated_at: dt.datetime | None = None


__all__ = [
    "CoverLetterEvidence",
    "CoverLetterEvidenceIngest",
    "CoverLetterTheme",
    "DEFAULT_WEIGHTS",
    "EvidenceObject",
    "EvidenceObjectIngest",
    "EvidenceStatus",
    "JobMatchEntry",
    "JobMatchResultOut",
    "MAX_EVIDENCE_CHUNK_IDS",
    "MAX_EVIDENCE_QUOTE_CHARS",
    "MAX_OVERALL_TEXT_CHARS",
    "MAX_REQUIREMENTS",
    "MAX_REQUIREMENT_CHARS",
    "MatchWeights",
    "PipelineMeta",
    "RequirementEvidence",
    "RequirementEvidenceIngest",
    "SCRUBBED_CONFIDENCE_CAP",
    "ScoreBreakdown",
    "ShortlistEntry",
    "SkillCategory",
    "SkillContribution",
]
