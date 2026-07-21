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
import json
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

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


# ── ADR-022 follow-up #2 — C0 control sanitisation at the SCHEMA boundary ────
#
# The bug (ADR-022 "Follow-up items" #2): a NUL (U+0000) in an LLM-authored
# string survives the verifier, ``json.dumps`` emits it as a lowercase-u
# Unicode escape, and Postgres rejects that escape outright ("unsupported
# Unicode escape sequence ... cannot be converted to text"). The whole
# ``persist_shortlist`` transaction dies, so ONE malformed quote loses the
# ENTIRE shortlist. This is an availability bug, not a cosmetic one.
#
# WHY THESE TESTS TARGET THE SCHEMA AND NOT ``verify_evidence``:
#
#   The written spec (ADR-022 line 104; HANDOFF line 1003) says "Strip C0
#   controls (except newline and tab) in ``verify_evidence``". That instruction
#   is WRONG, and these tests are deliberately built so a spec-literal fix
#   still FAILS. ``verify_evidence`` only ever rewrites the two ``evidence``
#   fields (and only ever to blank them); it never touches ``requirement``,
#   ``overall_summary`` or ``overall_motivation``. All three of those reach
#   ``persist_shortlist`` unmodified, so the transaction still dies and the
#   shortlist is still lost.
#
#   Sanitising at the schema boundary closes all five fields at the single
#   point every LLM response must pass through, and it GENERALISES the
#   NUL-strip idiom already established on the parse path
#   (``src/pipeline/parsing/extract.py`` ``_sanitize``) rather than inventing a
#   second, narrower idiom beside it.
#
# NOTE ON THIS FILE'S OWN SOURCE: control characters are built with ``chr()``
# and never written as literal escapes. ADR-022 records that its first draft
# embedded a literal NUL while describing the bug and git classified the file
# as binary. The same thing happened to the first draft of this test module.
# The byte is easy to propagate by accident — which is the point.

NUL = chr(0x00)

# ADR-022's character class, as an explicit codepoint set. Equivalent to the
# regex the ADR names (C0 controls and DEL, excluding TAB 0x09, LF 0x0a and
# CR 0x0d), written without escapes for the reason above.
C0_STRIPPED_CODEPOINTS: frozenset[int] = frozenset(
    {*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F}
)

# Every LLM-authored free-text field on the evidence models. All five must be
# sanitised — pinning only the two ``evidence`` fields would let the
# spec-literal ``verify_evidence`` fix pass while the bug stayed open.
C0_GUARDED_FIELDS: tuple[tuple[str, str], ...] = (
    ("RequirementEvidence", "evidence"),
    ("RequirementEvidence", "requirement"),
    ("CoverLetterEvidence", "evidence"),
    ("EvidenceObject", "overall_summary"),
    ("EvidenceObject", "overall_motivation"),
)

_C0_IDS = [f"{model}.{field}" for model, field in C0_GUARDED_FIELDS]

# Minimal valid payload per model, so each field can be exercised in isolation.
_C0_MODEL_BASE: dict[str, tuple[type[BaseModel], dict[str, object]]] = {
    "RequirementEvidence": (RequirementEvidence, {"requirement": "Python"}),
    "CoverLetterEvidence": (CoverLetterEvidence, {"theme": "motivation"}),
    "EvidenceObject": (EvidenceObject, {}),
}


def _validate_with(model_name: str, field: str, value: str) -> BaseModel:
    """Build ``model_name`` via ``model_validate`` with ``field`` set to
    ``value`` — the path an LLM JSON response actually takes."""
    model, base = _C0_MODEL_BASE[model_name]
    payload: dict[str, object] = {**base, field: value}
    return model.model_validate(payload)


def _construct_with(model_name: str, field: str, value: str) -> BaseModel:
    """Same, but via direct ``__init__`` — the path in-process callers take."""
    model, base = _C0_MODEL_BASE[model_name]
    kwargs: dict[str, object] = {**base, field: value}
    return model(**kwargs)


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_nul_byte_is_stripped_from_every_llm_authored_evidence_field(
    model_name: str, field: str
) -> None:
    """The exact byte that kills the ``persist_shortlist`` transaction."""
    obj = _validate_with(model_name, field, f"before{NUL}after")
    assert getattr(obj, field) == "beforeafter"


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_c0_controls_are_stripped_from_every_llm_authored_evidence_field(
    model_name: str, field: str
) -> None:
    """A spread across the class: NUL, BEL, VT, FF, ESC, DEL."""
    dirty = (
        f"a{chr(0x00)}b{chr(0x07)}c{chr(0x0B)}d" f"{chr(0x0C)}e{chr(0x1B)}f{chr(0x7F)}g"
    )
    obj = _validate_with(model_name, field, dirty)
    assert getattr(obj, field) == "abcdefg"


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_c0_sanitisation_also_applies_on_direct_construction(
    model_name: str, field: str
) -> None:
    """``model_validate`` and ``__init__`` must not diverge: a field validator
    covers both, an ad-hoc scrub in one calling helper covers neither."""
    obj = _construct_with(model_name, field, f"x{NUL}y")
    assert getattr(obj, field) == "xy"


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_newline_and_tab_survive_c0_sanitisation(model_name: str, field: str) -> None:
    """Multi-line quotes are legitimate — the scrub must not flatten them."""
    kept = f"line one{chr(0x0A)}line two{chr(0x09)}column two"
    obj = _validate_with(model_name, field, kept)
    assert getattr(obj, field) == kept


@pytest.mark.parametrize("model_name, field", C0_GUARDED_FIELDS, ids=_C0_IDS)
def test_clean_text_is_returned_unchanged_by_sanitisation(
    model_name: str, field: str
) -> None:
    """Anti-over-scrub: ordinary text, accents, CJK and punctuation the corpus
    actually contains must survive unchanged."""
    clean = "Built FastAPI services — café, 東京, 40+ teams; 99.9% uptime."
    obj = _validate_with(model_name, field, clean)
    assert getattr(obj, field) == clean


@pytest.mark.parametrize("codepoint", range(0x00, 0x21))
def test_every_low_codepoint_matches_the_adr_022_character_class(
    codepoint: int,
) -> None:
    """Exhaustive sweep of 0x00-0x20 against ADR-022's class: stripped iff the
    class contains it. Pins that TAB (0x09), LF (0x0a), CR (0x0d) and SPACE
    (0x20) are the ONLY survivors below 0x21."""
    char = chr(codepoint)
    ev = RequirementEvidence.model_validate(
        {"requirement": "Python", "evidence": f"a{char}b"}
    )
    expected = "ab" if codepoint in C0_STRIPPED_CODEPOINTS else f"a{char}b"
    assert ev.evidence == expected


def test_del_codepoint_is_stripped() -> None:
    """0x7f sits outside the 0x00-0x20 sweep and is inside the class."""
    ev = RequirementEvidence.model_validate(
        {"requirement": "Python", "evidence": f"a{chr(0x7F)}b"}
    )
    assert ev.evidence == "ab"


def test_nested_evidence_object_sanitises_its_child_models() -> None:
    """The real ingest shape: one ``model_validate`` of the whole LLM response.
    The nested ``RequirementEvidence`` / ``CoverLetterEvidence`` rows must be
    sanitised too, not just the two top-level ``overall_*`` strings."""
    ev = EvidenceObject.model_validate(
        {
            "requirements": [
                {
                    "requirement": f"Py{NUL}thon",
                    "status": "met",
                    "evidence": f"Shipped{NUL} Python APIs",
                    "confidence": 0.9,
                }
            ],
            "overall_summary": f"Strong{NUL} fit",
            "cover_letter_presence": True,
            "cover_letter_evidence": [
                {"theme": "motivation", "evidence": f"Eager{NUL} to join"}
            ],
            "overall_motivation": f"Keen{NUL}",
        }
    )
    assert ev.requirements[0].requirement == "Python"
    assert ev.requirements[0].evidence == "Shipped Python APIs"
    assert ev.overall_summary == "Strong fit"
    assert ev.cover_letter_evidence[0].evidence == "Eager to join"
    assert ev.overall_motivation == "Keen"


def test_serialised_evidence_carries_no_unicode_nul_escape() -> None:
    """The availability bug itself, at the boundary that causes it: whatever
    ``persist_shortlist`` hands to ``json.dumps`` must not contain the Unicode
    NUL escape, which Postgres rejects — taking the whole shortlist
    transaction down with it."""
    ev = EvidenceObject.model_validate(
        {
            "requirements": [
                {"requirement": f"Py{NUL}thon", "evidence": f"quote{NUL}here"}
            ],
            "cover_letter_evidence": [
                {"theme": "motivation", "evidence": f"cl{NUL}quote"}
            ],
            "overall_summary": f"sum{NUL}mary",
            "overall_motivation": f"moti{NUL}vation",
        }
    )
    dumped = json.dumps(ev.model_dump())
    nul_escape = chr(0x5C) + "u0000"  # backslash + u0000, the escape PG rejects
    assert nul_escape not in dumped
    assert NUL not in dumped


def test_c0_sanitisation_survives_the_dump_validate_roundtrip() -> None:
    """Sanitised output must be a fixed point: re-validating a dumped model
    changes nothing, so the shape stored in JSONB is stable."""
    ev = EvidenceObject.model_validate(
        {
            "requirements": [
                {"requirement": f"Py{NUL}thon", "evidence": f"a{chr(0x0B)}b"}
            ],
            "overall_summary": f"s{chr(0x1F)}um",
        }
    )
    again = EvidenceObject.model_validate(ev.model_dump())
    assert again.model_dump() == ev.model_dump()
    assert again.requirements[0].requirement == "Python"
