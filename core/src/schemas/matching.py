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
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

EvidenceStatus = Literal["met", "partial", "missing"]

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
    model_config = ConfigDict(extra="ignore")

    requirement: str
    status: EvidenceStatus = "missing"
    evidence: str = ""
    evidence_chunk_ids: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0.0, ge=0, le=1)
    # FU-2 (display-only): the resolved source text behind ``evidence_chunk_ids``,
    # expanded from ``resumes.parsed`` at read/export time. Redacted under blind
    # review, exactly like ``evidence``. Populated only on the display paths; the
    # LLM never emits it, so at write time it is always ``None`` and persists as
    # JSONB ``null`` — a pure display expansion, never a stored value.
    source_context: str | None = None


CoverLetterTheme = Literal["motivation", "role_alignment", "cultural_fit", "growth"]


class CoverLetterEvidence(BaseModel):
    """One cover-letter theme with a cited quote (Feature 1)."""

    model_config = ConfigDict(extra="ignore")

    theme: CoverLetterTheme
    evidence: str = ""
    evidence_chunk_ids: list[str] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0.0, ge=0, le=1)


class EvidenceObject(BaseModel):
    """LLM-output schema for shortlist_evidence_v1/v2 / chat_json target."""

    model_config = ConfigDict(extra="ignore")

    requirements: list[RequirementEvidence] = Field(default_factory=list)
    overall_summary: str = Field(default="", max_length=1000)
    cover_letter_presence: bool = False
    cover_letter_evidence: list[CoverLetterEvidence] = Field(default_factory=list)
    overall_motivation: str = Field(default="", max_length=1000)


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
    "CoverLetterTheme",
    "DEFAULT_WEIGHTS",
    "EvidenceObject",
    "EvidenceStatus",
    "JobMatchEntry",
    "JobMatchResultOut",
    "MatchWeights",
    "PipelineMeta",
    "RequirementEvidence",
    "ScoreBreakdown",
    "ShortlistEntry",
    "SkillCategory",
    "SkillContribution",
]
