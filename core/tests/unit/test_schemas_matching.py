"""Unit tests for ``src/schemas/matching.py`` — the ranking-pipeline contracts.

Phase 2 ports the KEEP set of hris ``packages/schemas/src/schemas/matching.py``
and DELETES the review workflow. These tests pin, as merge-blocking contracts:

* the CUT set (review/2nd-review types) is NOT importable — a guard against the
  review workflow creeping back in,
* ``ShortlistEntry`` loses ``current_decision`` / ``current_stage`` but keeps the
  blind-review fields ``blinded`` / ``display_label``,
* the ``MatchWeights`` ranking contract: exact defaults, the ``_sums_close_to_one``
  validator (top trio + sub-five each sum to 1.0; relief ≥ penalty), and frozen,
* the jsonb-stored shapes (``ScoreBreakdown`` / ``EvidenceObject`` / ``PipelineMeta``)
  round-trip faithfully,
* ``extra="forbid"``/``"ignore"`` behave per model.

These modules do not exist yet — this is the RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

import src.schemas as schemas_pkg
import src.schemas.matching as matching_mod
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    CoverLetterEvidence,
    EvidenceObject,
    JobMatchEntry,
    JobMatchResultOut,
    MatchWeights,
    PipelineMeta,
    RequirementEvidence,
    ScoreBreakdown,
    ShortlistEntry,
    SkillContribution,
)

_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)

# The KEEP public surface the module and package must expose.
MATCHING_PUBLIC: tuple[str, ...] = (
    "EvidenceStatus",
    "SkillCategory",
    "CoverLetterTheme",
    "MatchWeights",
    "DEFAULT_WEIGHTS",
    "SkillContribution",
    "ScoreBreakdown",
    "RequirementEvidence",
    "CoverLetterEvidence",
    "EvidenceObject",
    "PipelineMeta",
    "ShortlistEntry",
    "JobMatchEntry",
    "JobMatchResultOut",
)

# The review workflow — CUT entirely. None of these may be importable from the
# module OR re-exported by the package (merge-blocking against review creep).
CUT_MATCHING: tuple[str, ...] = (
    "PipelineStage",
    "TERMINAL_STAGES",
    "DispositionReason",
    "DecisionKind",
    "ShortlistDecisionCreate",
    "ShortlistDecisionOut",
    "StageTransitionCreate",
    "StageTransitionOut",
)

# The exact DEFAULT_WEIGHTS the plan's ranking algorithm depends on.
EXPECTED_DEFAULTS: tuple[tuple[str, float], ...] = (
    ("structured", 0.6),
    ("evidence", 0.3),
    ("motivation", 0.1),
    ("skill", 0.40),
    ("experience", 0.25),
    ("education", 0.10),
    ("seniority", 0.15),
    ("vector", 0.10),
    ("evidence_verify_fuzz", 0.85),
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _score_breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.5,
        experience=0.5,
        education=0.5,
        seniority=0.5,
        vector=0.5,
        structured=0.5,
    )


# ── Public surface / re-export ───────────────────────────────────────────────


@pytest.mark.parametrize("name", MATCHING_PUBLIC)
def test_module_exposes_keep_name(name: str) -> None:
    assert hasattr(matching_mod, name)


@pytest.mark.parametrize("name", MATCHING_PUBLIC)
def test_package_reexports_keep_name(name: str) -> None:
    assert hasattr(schemas_pkg, name)


# ── Cut-scope guard (merge-blocking against review-workflow creep) ───────────


@pytest.mark.parametrize("name", CUT_MATCHING)
def test_cut_review_type_is_not_importable_from_module(name: str) -> None:
    assert not hasattr(matching_mod, name)


@pytest.mark.parametrize("name", CUT_MATCHING)
def test_cut_review_type_is_not_reexported_by_package(name: str) -> None:
    assert not hasattr(schemas_pkg, name)


@pytest.mark.parametrize("name", CUT_MATCHING)
def test_cut_review_type_is_not_in_module_all(name: str) -> None:
    assert name not in getattr(matching_mod, "__all__", [])


@pytest.mark.parametrize("field", ["current_decision", "current_stage"])
def test_shortlist_entry_drops_review_fields(field: str) -> None:
    assert field not in ShortlistEntry.model_fields


@pytest.mark.parametrize("field", ["blinded", "display_label"])
def test_shortlist_entry_keeps_blind_review_fields(field: str) -> None:
    """Blind review is v1 scope — NOT the cut 2nd-review workflow."""
    assert field in ShortlistEntry.model_fields


# ── MatchWeights: the ranking contract ───────────────────────────────────────


def test_default_weights_is_a_valid_matchweights() -> None:
    assert isinstance(DEFAULT_WEIGHTS, MatchWeights)


@pytest.mark.parametrize("field, value", EXPECTED_DEFAULTS)
def test_default_weights_exact_values(field: str, value: float) -> None:
    assert getattr(DEFAULT_WEIGHTS, field) == value


def test_matchweights_rejects_top_trio_not_summing_to_one() -> None:
    """structured+evidence+motivation must sum to 1.0 — 0.7+0.3+0.1 = 1.1."""
    with pytest.raises(ValidationError):
        MatchWeights(structured=0.7, evidence=0.3, motivation=0.1)


def test_matchweights_rejects_sub_five_not_summing_to_one() -> None:
    """skill+experience+education+seniority+vector must sum to 1.0."""
    with pytest.raises(ValidationError):
        MatchWeights(skill=0.50)  # 0.50+0.25+0.10+0.15+0.10 = 1.10


def test_matchweights_rejects_relief_below_penalty() -> None:
    with pytest.raises(ValidationError):
        MatchWeights(implied_experience_relief=0.4, must_have_miss_penalty=0.5)


def test_matchweights_accepts_relief_equal_to_penalty() -> None:
    """== disables the relief while keeping the flag — must be allowed."""
    weights = MatchWeights(implied_experience_relief=0.5, must_have_miss_penalty=0.5)
    assert weights.implied_experience_relief == 0.5


@pytest.mark.parametrize("field, bad", [("structured", 1.5), ("skill", -0.1)])
def test_matchweights_field_bounds(field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        MatchWeights(**{field: bad})


def test_matchweights_is_frozen() -> None:
    weights = MatchWeights()
    with pytest.raises(ValidationError):
        weights.structured = 0.5  # type: ignore[misc]


# ── Sub-score / evidence models ──────────────────────────────────────────────


def test_skill_contribution_minimal_valid() -> None:
    contrib = SkillContribution(skill="Python", score=0.9)
    assert contrib.is_must_have is False


def test_score_breakdown_minimal_valid() -> None:
    sb = _score_breakdown()
    assert sb.motivation == 0.0  # default
    assert sb.implied_experience is False


def test_requirement_evidence_defaults() -> None:
    ev = RequirementEvidence(requirement="Python")
    assert ev.status == "missing"
    assert ev.confidence == 0.0


def test_cover_letter_evidence_minimal_valid() -> None:
    cle = CoverLetterEvidence(theme="motivation")
    assert cle.evidence == ""


def test_job_match_entry_minimal_valid() -> None:
    entry = JobMatchEntry(
        job_id=uuid4(),
        title="Backend Engineer",
        rank=1,
        score_final=0.8,
        score_structured=0.7,
        score_evidence=0.6,
        score_breakdown=_score_breakdown(),
        requirement_count=5,
        must_have_count=2,
    )
    assert entry.department is None


def test_job_match_result_out_defaults_empty() -> None:
    out = JobMatchResultOut(resume_id=uuid4())
    assert out.entries == []
    assert out.generated_at is None


def test_shortlist_entry_minimal_valid() -> None:
    entry = ShortlistEntry(
        id=uuid4(),
        job_id=uuid4(),
        resume_id=uuid4(),
        rank=1,
        score_final=0.9,
        score_breakdown=_score_breakdown(),
        evidence=None,
        generated_at=_TS,
    )
    assert entry.blinded is False
    assert entry.display_label is None


# ── jsonb round-trip fidelity (stored verbatim in Postgres jsonb) ────────────


def test_score_breakdown_roundtrips_faithfully() -> None:
    sb = ScoreBreakdown(
        skill=0.5,
        experience=0.4,
        education=0.3,
        seniority=0.2,
        vector=0.1,
        structured=0.6,
        motivation=0.1,
        implied_experience=True,
        skill_contributions=[
            SkillContribution(skill="Python", score=0.9, is_must_have=True)
        ],
    )
    again = ScoreBreakdown.model_validate(sb.model_dump())
    assert again.model_dump() == sb.model_dump()


def test_evidence_object_roundtrips_faithfully() -> None:
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="met", confidence=0.9)
        ],
        overall_summary="Strong fit",
        cover_letter_presence=True,
        cover_letter_evidence=[CoverLetterEvidence(theme="motivation", confidence=0.8)],
        overall_motivation="Keen",
    )
    again = EvidenceObject.model_validate(ev.model_dump())
    assert again.model_dump() == ev.model_dump()


def test_pipeline_meta_roundtrips_faithfully() -> None:
    meta = PipelineMeta(
        model_gen="gpt-oss:20b",
        model_emb="nomic-embed-text",
        prompt_versions={"resume_core": "v1"},
        weights=MatchWeights(),
        generated_at=_TS,
        timings_ms={"structured": 12},
    )
    again = PipelineMeta.model_validate(meta.model_dump())
    assert again.model_dump() == meta.model_dump()


# ── extra="forbid" vs extra="ignore" ─────────────────────────────────────────


def test_score_breakdown_forbids_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ScoreBreakdown.model_validate(
            {
                "skill": 0.5,
                "experience": 0.5,
                "education": 0.5,
                "seniority": 0.5,
                "vector": 0.5,
                "structured": 0.5,
                "bogus": 1,
            }
        )


def test_matchweights_forbids_unknown_key() -> None:
    with pytest.raises(ValidationError):
        MatchWeights.model_validate({"bogus": 1})


def test_evidence_object_ignores_unknown_key() -> None:
    ev = EvidenceObject.model_validate({"bogus": 1})
    assert "bogus" not in ev.model_dump()


def test_requirement_evidence_ignores_unknown_key() -> None:
    ev = RequirementEvidence.model_validate({"requirement": "Python", "bogus": 1})
    assert "bogus" not in ev.model_dump()
