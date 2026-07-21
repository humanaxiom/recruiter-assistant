"""Unit tests for the Phase 4c matching **stages** — pure scoring functions
ported from hris ``packages/pipeline/src/pipeline/matching/stages.py``.

RED half of the TDD cycle: ``src/pipeline/matching/stages.py`` (and the
``src.pipeline.matching`` package itself) does not exist yet, so every test
below fails at collection time with ``ModuleNotFoundError``. Once 4c lands
``stages.py``, these pin the intended, CORRECTED behavior — not a literal
copy of hris's known-buggy behavior — for the blockers recorded in the 4c
resume-point handoff:

* blocker #1 — ``score_skill_breakdown``'s must-have-miss detection must key
  off a row's ``ontology_weight == 0`` (genuinely no ontology/family credit
  at all), NOT ``score == 0.0`` (which a present-but-insufficient-tenure row
  can also hit, wrongly labelling it a miss).
* blocker #6 (carry-forward from Phase 4a round 5's F1 finding, "hris's own
  shipped ``_fuzz_substring`` verifies all four fabricated anchors") —
  ``verify_evidence`` must use a rapidfuzz metric that REJECTS every
  fabricated quote in ``tests/evals/fixtures/labels.json``'s
  ``negative_evidence`` while ACCEPTING every ``gold_evidence`` anchor; the
  landmine test below documents, with real fixture text, exactly why
  ``fuzz.WRatio``/``fuzz.ratio`` are the wrong choice and
  ``partial_ratio``/``token_set_ratio`` are required.

Also covers the remaining pure functions plainly, against the read hris
source: ``is_senior_candidate`` (a YEARS-based boolean gate for the
implied-experience relief — NOT the cosine-title ``seniority`` sub-score,
which orchestrator.py computes separately with a live embedder and is
therefore out of scope for this pure-function file), ``score_experience``,
``score_education`` (degree LEVEL only — ``jd.education.fields`` is not
read, by current design; see labels.json's F2 finding and
``docs/EXTRACTION_PLAN.md``'s open decision), ``normalise_vector_scores``,
``stage4_combine``, ``_evidence_completeness``, ``_motivation_score``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

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
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    CoverLetterEvidence,
    EvidenceObject,
    RequirementEvidence,
    ScoreBreakdown,
)

# ── Evals-corpus fixture loading (read-only — mirrors test_evals_corpus.py) ──

_EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
_FIXTURES_DIR = _EVALS_DIR / "fixtures"
_LABELS_PATH = _FIXTURES_DIR / "labels.json"


def _load_labels() -> dict[str, Any]:
    with _LABELS_PATH.open(encoding="utf-8") as fh:
        result: dict[str, Any] = json.load(fh)
        return result


def _load_resume_raw(resume_id: str) -> dict[str, Any]:
    labels = _load_labels()
    fixture = labels["resumes"][resume_id]["fixture"]
    with (_FIXTURES_DIR / fixture).open(encoding="utf-8") as fh:
        result: dict[str, Any] = json.load(fh)
        return result


def _chunks_by_id(resume_raw: dict[str, Any]) -> dict[str, str]:
    return {c["id"]: c["text"] for c in resume_raw["chunks"]}


def _skill_chunk_id(resume_raw: dict[str, Any], skill_name: str) -> str:
    for skill in resume_raw["skills"]:
        if skill["name"] == skill_name:
            ids = skill["evidence_chunk_ids"]
            assert ids, f"{skill_name} has no evidence_chunk_ids in fixture"
            return str(ids[0])
    raise AssertionError(f"skill {skill_name!r} not found in resume fixture")


_LABELS = _load_labels()

_NEGATIVE_EVIDENCE_CASES: list[tuple[str, str, str]] = [
    (resume_id, chunk_id, fabricated_quote)
    for resume_id, entry in _LABELS["resumes"].items()
    for chunk_id, fabricated_quote in entry.get("negative_evidence", {}).items()
]

_GOLD_EVIDENCE_CASES: list[tuple[str, str, str]] = [
    (resume_id, skill_name, quote)
    for resume_id, entry in _LABELS["resumes"].items()
    for skill_name, quote in entry.get("gold_evidence", {}).items()
]


def test_labels_json_has_exactly_four_negative_evidence_fabrications() -> None:
    """Pins labels.json's falsifiability invariant (see its ``_comment``):
    four fabricated quotes across r01/r02/r09 must all fail verification."""
    assert len(_NEGATIVE_EVIDENCE_CASES) == 4


def test_labels_json_has_gold_evidence_anchors_to_verify() -> None:
    assert len(_GOLD_EVIDENCE_CASES) >= 2


# ── is_senior_candidate: years-based boolean gate for implied-experience ────
# (NOT the cosine-title `seniority` sub-score — that's computed with a live
# embedder in orchestrator.py and is out of scope for this pure-function file.)


@pytest.mark.parametrize(
    "total_years, jd_min_years, expected",
    [
        (None, 5, False),
        (10, None, False),
        (10, 0, False),  # falsy jd_min_years
        (0, 5, False),  # falsy total_years
        (7, 5, False),  # 7 < 5 * 1.5 = 7.5 -> just below the relief threshold
        (8, 5, True),  # 8 >= 7.5
        (6, 4, True),  # boundary: 6 >= 4 * 1.5 = 6.0 exactly (>=)
        (5, 4, False),  # boundary: 5 < 6.0
    ],
)
def test_is_senior_candidate(
    total_years: int | None, jd_min_years: int | None, expected: bool
) -> None:
    assert (
        is_senior_candidate(total_years, jd_min_years, weights=DEFAULT_WEIGHTS)
        is expected
    )


# ── score_skill_breakdown: general behavior ─────────────────────────────────


def _row(**overrides: Any) -> _SkillRowFromCypher:
    base: dict[str, Any] = {
        "skill": "Python",
        "req_years": 3,
        "is_must_have": False,
        "years": None,
        "last_used_year": 2026,
        "ontology_weight": 1.0,
    }
    base.update(overrides)
    return _SkillRowFromCypher(**base)


def test_score_skill_breakdown_empty_rows_returns_zero_and_empty_list() -> None:
    overall, contributions = score_skill_breakdown(
        [], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    assert overall == 0.0
    assert contributions == []


def test_score_skill_breakdown_unknown_years_gets_full_years_credit() -> None:
    """Résumé extraction frequently omits per-skill years; treating that as
    zero would unfairly zero the sub-score for almost every candidate."""
    row = _row(years=None, ontology_weight=1.0, last_used_year=2026)
    overall, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.score == 1.0
    assert contrib.years is None
    assert overall == 1.0


@pytest.mark.parametrize(
    "last_used_year, expected_recency",
    [
        (2026, 1.0),  # delta 0 <= recency_recent_years (2)
        (2024, 1.0),  # delta 2
        (2023, 0.7),  # delta 3 <= recency_mid_years (5)
        (2021, 0.7),  # delta 5
        (2020, 0.4),  # delta 6 > recency_mid_years
    ],
)
def test_score_skill_breakdown_recency_buckets(
    last_used_year: int, expected_recency: float
) -> None:
    row = _row(years=None, ontology_weight=1.0, last_used_year=last_used_year)
    _, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.recency == expected_recency
    assert contrib.score == pytest.approx(expected_recency)


def test_score_skill_breakdown_reason_is_ontology_fallback_when_indirect() -> None:
    row = _row(ontology_weight=0.6, last_used_year=2026)
    _, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.reason == "ontology-fallback"


def test_score_skill_breakdown_reason_is_none_when_direct_match() -> None:
    row = _row(ontology_weight=1.0, last_used_year=2026)
    _, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.reason is None


def test_score_skill_breakdown_years_score_caps_at_req_years() -> None:
    row = _row(years=10, req_years=5, ontology_weight=1.0, last_used_year=2026)
    _, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.score == 1.0  # min(1.0, 10/5) capped, not 2.0


# ── blocker #1: missing_must keys off ontology_weight == 0, NOT score==0.0 ──


def test_family_credited_present_must_have_alone_is_not_penalized() -> None:
    """``ontology_weight=0.5`` means the candidate DOES hold this must-have
    via an ontology/family match — it is present, not missing — so as the
    sole must-have it must not trigger ``must_have_miss_penalty``."""
    row = _row(
        skill="Kubernetes",
        is_must_have=True,
        ontology_weight=0.5,
        years=None,
        last_used_year=2026,
    )
    overall, contributions = score_skill_breakdown(
        [row], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    [contrib] = contributions
    assert contrib.score == 0.5
    assert overall == 0.5  # NOT halved


def test_missing_must_have_is_not_masked_by_a_present_family_credited_sibling() -> None:
    """A must-have that is genuinely missing (``ontology_weight=0``) must
    still trigger ``must_have_miss_penalty`` even when scored alongside
    ANOTHER must-have that is present via family credit
    (``ontology_weight=0.5``, ``score=0.5``) — the nonzero sibling must not
    mask the real miss."""
    missing = _row(
        skill="Terraform",
        is_must_have=True,
        ontology_weight=0.0,
        years=None,
        last_used_year=None,
    )
    present = _row(
        skill="Kubernetes",
        is_must_have=True,
        ontology_weight=0.5,
        years=None,
        last_used_year=2026,
    )
    overall, contributions = score_skill_breakdown(
        [missing, present], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    assert {c.skill: c.score for c in contributions} == {
        "Terraform": 0.0,
        "Kubernetes": 0.5,
    }
    # (0.0 + 0.5) / 2 = 0.25, halved by must_have_miss_penalty (0.5) -> 0.125
    assert overall == pytest.approx(0.125)


def test_truly_missing_must_have_fires_penalty() -> None:
    missing = _row(
        skill="Terraform",
        is_must_have=True,
        ontology_weight=0.0,
        years=None,
        last_used_year=None,
    )
    held = _row(
        skill="Python",
        is_must_have=True,
        ontology_weight=1.0,
        years=None,
        last_used_year=2026,
    )
    overall, _ = score_skill_breakdown(
        [missing, held], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    # (0.0 + 1.0) / 2 = 0.5, halved by must_have_miss_penalty (0.5) -> 0.25
    assert overall == pytest.approx(0.25)


def test_fully_held_must_have_does_not_fire_penalty() -> None:
    row = _row(
        skill="Python",
        is_must_have=True,
        ontology_weight=1.0,
        years=None,
        last_used_year=2026,
    )
    overall, _ = score_skill_breakdown([row], weights=DEFAULT_WEIGHTS, today_year=2026)
    assert overall == 1.0


def test_missing_must_keys_off_ontology_weight_not_score_mutant() -> None:
    """The must-have-miss filter must key off ``ontology_weight == 0`` (the
    row genuinely lacks any ontology/family credit), NOT ``score == 0.0``.

    A present-but-insufficient-tenure row (``ontology_weight=0.5`` — the
    candidate DOES hold the skill via family credit — but ``years=0``
    against a positive ``req_years`` zeroes its numeric score) must NOT be
    treated as missing. A mutant that keys the filter off ``score == 0.0``
    instead would wrongly classify this row as a miss and apply
    ``must_have_miss_penalty``, producing 0.25 instead of the correct 0.5 —
    this assertion is sharp enough to kill that mutant.
    """
    held = _row(
        skill="Python",
        is_must_have=True,
        ontology_weight=1.0,
        years=None,
        last_used_year=2026,
    )
    present_zero_score = _row(
        skill="Docker",
        is_must_have=True,
        ontology_weight=0.5,
        years=0,
        req_years=5,
        last_used_year=2026,
    )
    overall, contributions = score_skill_breakdown(
        [held, present_zero_score], weights=DEFAULT_WEIGHTS, today_year=2026
    )
    scores = {c.skill: c.score for c in contributions}
    assert scores == {"Python": 1.0, "Docker": 0.0}
    # (1.0 + 0.0) / 2 = 0.5. Neither row has ontology_weight == 0, so no
    # penalty should apply. A score==0.0-keyed mutant would (wrongly) apply
    # the 0.5x must_have_miss_penalty here, producing 0.25 instead.
    assert overall == pytest.approx(0.5)


def test_senior_implied_experience_relief_softens_penalty_and_flags_reason() -> None:
    missing = _row(
        skill="Terraform",
        is_must_have=True,
        ontology_weight=0.0,
        years=None,
        last_used_year=None,
    )
    held = _row(
        skill="Python",
        is_must_have=True,
        ontology_weight=1.0,
        years=None,
        last_used_year=2026,
    )
    overall, contributions = score_skill_breakdown(
        [missing, held], weights=DEFAULT_WEIGHTS, senior=True, today_year=2026
    )
    # matched_coverage = 1 - 1/2 = 0.5 >= implied_min_coverage (0.5) -> relief.
    # (0.0 + 1.0) / 2 = 0.5, softened by implied_experience_relief (0.75) -> 0.375
    assert overall == pytest.approx(0.375)
    missing_contrib = next(c for c in contributions if c.skill == "Terraform")
    assert missing_contrib.reason == "implied-experience"


# ── score_experience ─────────────────────────────────────────────────────────


def test_score_experience_no_min_years_is_perfect() -> None:
    assert score_experience(3, None, weights=DEFAULT_WEIGHTS) == 1.0
    assert score_experience(None, None, weights=DEFAULT_WEIGHTS) == 1.0


def test_score_experience_none_total_years_treated_as_zero() -> None:
    assert score_experience(None, 5, weights=DEFAULT_WEIGHTS) == 0.0


def test_score_experience_under_minimum_is_raw_ratio() -> None:
    assert score_experience(2, 4, weights=DEFAULT_WEIGHTS) == pytest.approx(0.5)


def test_score_experience_meets_minimum_exactly() -> None:
    assert score_experience(5, 5, weights=DEFAULT_WEIGHTS) == 1.0


def test_score_experience_moderately_over_minimum_stays_perfect() -> None:
    """raw <= overqual_ratio (2.0) does not increase past 1.0 either."""
    assert score_experience(8, 5, weights=DEFAULT_WEIGHTS) == 1.0  # raw = 1.6


def test_score_experience_overqualified_past_ratio_mildly_drops() -> None:
    # raw = 15/5 = 3.0 -> 1.0 - (3.0 - 2.0) * 0.1 = 0.9
    assert score_experience(15, 5, weights=DEFAULT_WEIGHTS) == pytest.approx(0.9)


def test_score_experience_far_overqualified_hits_floor() -> None:
    # raw = 100/5 = 20 -> 1.0 - (20-2)*0.1 = -0.8 -> clamped at overqual_floor 0.8
    assert score_experience(100, 5, weights=DEFAULT_WEIGHTS) == pytest.approx(0.8)


# ── score_education: degree LEVEL only, jd.education.fields NOT read ───────


def test_score_education_no_min_level_is_perfect() -> None:
    assert score_education(["bachelors"], None, weights=DEFAULT_WEIGHTS) == 1.0


def test_score_education_no_candidate_levels_is_zero() -> None:
    assert score_education([], "bachelors", weights=DEFAULT_WEIGHTS) == 0.0


def test_score_education_all_none_levels_is_zero() -> None:
    assert score_education([None, None], "bachelors", weights=DEFAULT_WEIGHTS) == 0.0


def test_score_education_meets_required_level_is_perfect() -> None:
    assert score_education(["bachelors"], "bachelors", weights=DEFAULT_WEIGHTS) == 1.0


def test_score_education_exceeds_required_level_is_perfect() -> None:
    assert (
        score_education(["masters", "associate"], "bachelors", weights=DEFAULT_WEIGHTS)
        == 1.0
    )


def test_score_education_below_required_level_gets_partial_credit() -> None:
    # associate(2) < bachelors(3) -> education_partial (0.5) * 2/3
    assert score_education(
        ["associate"], "bachelors", weights=DEFAULT_WEIGHTS
    ) == pytest.approx(0.5 * (2 / 3))


def test_score_education_unrecognized_candidate_level_scores_as_zero_floor() -> None:
    assert score_education(
        ["not_a_real_degree_level"], "bachelors", weights=DEFAULT_WEIGHTS
    ) == pytest.approx(0.0)


def test_score_education_unknown_jd_level_defaults_to_full_credit() -> None:
    """An unrecognized JD requirement maps to level 0 via _LEVEL_ORDER.get
    default; any recognized candidate level clears that trivially."""
    assert (
        score_education(["bachelors"], "not_a_real_jd_level", weights=DEFAULT_WEIGHTS)
        == 1.0
    )


# ── normalise_vector_scores ───────────────────────────────────────────────


def test_normalise_vector_scores_empty_list() -> None:
    assert normalise_vector_scores([]) == []


def test_normalise_vector_scores_single_element_is_degenerate() -> None:
    assert normalise_vector_scores([5.0]) == [1.0]


def test_normalise_vector_scores_all_identical_is_degenerate() -> None:
    assert normalise_vector_scores([3.0, 3.0, 3.0]) == [1.0, 1.0, 1.0]


def test_normalise_vector_scores_min_max_scales_linearly() -> None:
    assert normalise_vector_scores([1.0, 2.0, 3.0]) == pytest.approx([0.0, 0.5, 1.0])


def test_normalise_vector_scores_handles_negative_values() -> None:
    assert normalise_vector_scores([-5.0, 5.0]) == pytest.approx([0.0, 1.0])


# ── verify_evidence: rapidfuzz anti-fabrication (blocker #6) ────────────────


@pytest.mark.parametrize(
    "resume_id, chunk_id, fabricated_quote", _NEGATIVE_EVIDENCE_CASES
)
def test_verify_evidence_rejects_every_negative_evidence_fabrication(
    resume_id: str, chunk_id: str, fabricated_quote: str
) -> None:
    resume_raw = _load_resume_raw(resume_id)
    chunks_by_id = _chunks_by_id(resume_raw)
    assert chunk_id in chunks_by_id  # sanity: the fixture's chunk really exists
    req = RequirementEvidence(
        requirement="whatever the JD asked for",
        status="met",
        evidence=fabricated_quote,
        evidence_chunk_ids=[chunk_id],
        confidence=0.9,
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, chunks_by_id, weights=DEFAULT_WEIGHTS)
    result = cleaned.requirements[0]
    assert result.evidence == "", (
        f"fabricated quote {fabricated_quote!r} against real chunk "
        f"{chunks_by_id[chunk_id]!r} must be blanked, not leaked"
    )
    assert result.status == "missing"
    assert result.confidence <= 0.3


@pytest.mark.parametrize("resume_id, skill_name, quote", _GOLD_EVIDENCE_CASES)
def test_verify_evidence_accepts_every_gold_evidence_anchor(
    resume_id: str, skill_name: str, quote: str
) -> None:
    resume_raw = _load_resume_raw(resume_id)
    chunks_by_id = _chunks_by_id(resume_raw)
    chunk_id = _skill_chunk_id(resume_raw, skill_name)
    assert quote.lower() in chunks_by_id[chunk_id].lower()  # sanity: real substring
    req = RequirementEvidence(
        requirement=skill_name,
        status="met",
        evidence=quote,
        evidence_chunk_ids=[chunk_id],
        confidence=0.9,
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, chunks_by_id, weights=DEFAULT_WEIGHTS)
    result = cleaned.requirements[0]
    assert result.evidence == quote
    assert result.status == "met"
    assert result.confidence == 0.9


def test_verify_evidence_strips_hallucinated_chunk_ids() -> None:
    req = RequirementEvidence(
        requirement="Python",
        status="met",
        evidence="",
        evidence_chunk_ids=["c_999_does_not_exist"],
        confidence=0.9,
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, {}, weights=DEFAULT_WEIGHTS)
    assert cleaned.requirements[0].evidence_chunk_ids == []


def test_verify_evidence_blanks_and_demotes_an_uncited_quote() -> None:
    """A quote with NO surviving citation gets the SAME scrub as a quote that
    fails to match its cited chunk: evidence blanked, ``met`` demoted to
    ``missing``, confidence capped. No citation is strictly less evidence than
    a bad citation, so it cannot warrant a weaker response."""
    req = RequirementEvidence(
        requirement="Python",
        status="met",
        evidence="some quote with no valid citation",
        evidence_chunk_ids=["c_999_does_not_exist"],
        confidence=0.95,
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, {}, weights=DEFAULT_WEIGHTS)
    result = cleaned.requirements[0]
    assert result.evidence == ""
    assert result.status == "missing"
    assert result.confidence <= 0.3


def test_verify_evidence_passes_through_requirement_with_no_quote() -> None:
    req = RequirementEvidence(
        requirement="Docker", status="missing", evidence="", confidence=0.0
    )
    evidence = EvidenceObject(requirements=[req])
    cleaned = verify_evidence(evidence, {}, weights=DEFAULT_WEIGHTS)
    assert cleaned.requirements[0].requirement == "Docker"
    assert cleaned.requirements[0].status == "missing"


def test_verify_evidence_does_not_mutate_input() -> None:
    req = RequirementEvidence(
        requirement="Python",
        status="met",
        evidence="junk",
        evidence_chunk_ids=["c1"],
        confidence=0.9,
    )
    evidence = EvidenceObject(requirements=[req])
    chunks_by_id = {"c1": "totally unrelated text with no overlap at all"}
    verify_evidence(evidence, chunks_by_id, weights=DEFAULT_WEIGHTS)
    assert evidence.requirements[0].evidence == "junk"


def test_verify_evidence_scrubs_fabricated_cover_letter_quote() -> None:
    cle = CoverLetterEvidence(
        theme="motivation",
        evidence="I have always dreamed of astrophysics since childhood",
        evidence_chunk_ids=["cl_1"],
        confidence=0.9,
    )
    evidence = EvidenceObject(cover_letter_evidence=[cle])
    chunks_by_id = {
        "cl_1": "I am excited to apply my backend engineering skills to this role."
    }
    cleaned = verify_evidence(evidence, chunks_by_id, weights=DEFAULT_WEIGHTS)
    result = cleaned.cover_letter_evidence[0]
    assert result.evidence == ""
    assert result.confidence <= 0.3


def test_landmine_wratio_leaks_a_fabrication_and_ratio_rejects_gold_evidence() -> None:
    """Documents WHY verify_evidence's chosen metric must be
    ``partial_ratio`` or ``token_set_ratio`` — measured against this
    corpus's real fixture text (see ``labels.json``'s negative_evidence /
    gold_evidence anchors):

    * ``fuzz.WRatio`` scores r02's ``c_002`` fabrication at 0.855 — it would
      LEAK past the 0.85 ``evidence_verify_fuzz`` bar.
    * ``fuzz.ratio`` scores r01's Python/PostgreSQL gold anchors at
      0.648/0.796 — both would be WRONGLY REJECTED, even though they are
      verbatim substrings of their cited chunks.
    """
    pytest.importorskip("rapidfuzz")
    from rapidfuzz import fuzz

    r02 = _load_resume_raw("r02_jordan_kim")
    r02_chunks = _chunks_by_id(r02)
    fabricated = _LABELS["resumes"]["r02_jordan_kim"]["negative_evidence"]["c_002"]
    haystack = r02_chunks["c_002"].lower()
    needle = fabricated.lower()

    wratio = fuzz.WRatio(needle, haystack) / 100
    assert wratio == pytest.approx(0.855, abs=0.005), (
        "landmine: WRatio scores this FABRICATED quote high enough to leak "
        "past the 0.85 evidence_verify_fuzz bar"
    )
    assert wratio >= DEFAULT_WEIGHTS.evidence_verify_fuzz  # would (wrongly) verify

    r01 = _load_resume_raw("r01_casey_rivera")
    r01_chunks = _chunks_by_id(r01)
    gold = _LABELS["resumes"]["r01_casey_rivera"]["gold_evidence"]
    py_chunk_id = _skill_chunk_id(r01, "Python")
    pg_chunk_id = _skill_chunk_id(r01, "PostgreSQL")

    py_ratio = fuzz.ratio(gold["Python"].lower(), r01_chunks[py_chunk_id].lower()) / 100
    pg_ratio = (
        fuzz.ratio(gold["PostgreSQL"].lower(), r01_chunks[pg_chunk_id].lower()) / 100
    )
    assert py_ratio == pytest.approx(0.648, abs=0.005), (
        "landmine: plain ratio() scores this GOLD (verbatim substring) quote "
        "below the 0.85 bar -- would wrongly reject valid evidence"
    )
    assert pg_ratio == pytest.approx(0.796, abs=0.005)
    assert py_ratio < DEFAULT_WEIGHTS.evidence_verify_fuzz
    assert pg_ratio < DEFAULT_WEIGHTS.evidence_verify_fuzz

    # The metric verify_evidence must actually use clears BOTH traps at once:
    # rejects the fabrication, accepts both gold anchors.
    for metric in (fuzz.partial_ratio, fuzz.token_set_ratio):
        assert metric(needle, haystack) / 100 < DEFAULT_WEIGHTS.evidence_verify_fuzz
        assert (
            metric(gold["Python"].lower(), r01_chunks[py_chunk_id].lower()) / 100
            >= DEFAULT_WEIGHTS.evidence_verify_fuzz
        )
        assert (
            metric(gold["PostgreSQL"].lower(), r01_chunks[pg_chunk_id].lower()) / 100
            >= DEFAULT_WEIGHTS.evidence_verify_fuzz
        )


# ── _evidence_completeness ──────────────────────────────────────────────────


def test_evidence_completeness_none_evidence_is_zero() -> None:
    assert _evidence_completeness(None, weights=DEFAULT_WEIGHTS) == 0.0


def test_evidence_completeness_empty_requirements_is_zero() -> None:
    assert (
        _evidence_completeness(EvidenceObject(requirements=[]), weights=DEFAULT_WEIGHTS)
        == 0.0
    )


def test_evidence_completeness_all_met_high_confidence_is_one() -> None:
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="met", confidence=0.9),
            RequirementEvidence(requirement="SQL", status="met", confidence=0.8),
        ]
    )
    assert _evidence_completeness(ev, weights=DEFAULT_WEIGHTS) == 1.0


def test_evidence_completeness_met_below_confidence_threshold_does_not_count() -> None:
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="met", confidence=0.5),
            RequirementEvidence(requirement="SQL", status="missing", confidence=0.0),
        ]
    )
    assert _evidence_completeness(ev, weights=DEFAULT_WEIGHTS) == 0.0


def test_evidence_completeness_partial_counts_at_partial_weight() -> None:
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="partial", confidence=0.6),
            RequirementEvidence(requirement="SQL", status="missing", confidence=0.0),
        ]
    )
    # (0 met + 0.5 * 1 partial) / 2 = 0.25
    assert _evidence_completeness(ev, weights=DEFAULT_WEIGHTS) == pytest.approx(0.25)


# ── _motivation_score ────────────────────────────────────────────────────────


def test_motivation_score_none_evidence_is_zero() -> None:
    assert _motivation_score(None, weights=DEFAULT_WEIGHTS) == 0.0


def test_motivation_score_empty_cover_letter_evidence_is_zero() -> None:
    assert (
        _motivation_score(
            EvidenceObject(cover_letter_evidence=[]), weights=DEFAULT_WEIGHTS
        )
        == 0.0
    )


def test_motivation_score_all_verified_is_mean_confidence() -> None:
    ev = EvidenceObject(
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation", confidence=0.9, evidence_chunk_ids=["cl_1"]
            ),
            CoverLetterEvidence(
                theme="growth", confidence=0.7, evidence_chunk_ids=["cl_2"]
            ),
        ]
    )
    assert _motivation_score(ev, weights=DEFAULT_WEIGHTS) == pytest.approx(0.8)


def test_motivation_score_unverified_theme_drags_score_down() -> None:
    """Denominator is ALL emitted themes, not just verified ones — an
    unverified/uncited theme drags the score down rather than being
    excluded outright."""
    ev = EvidenceObject(
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation", confidence=0.9, evidence_chunk_ids=["cl_1"]
            ),
            CoverLetterEvidence(
                theme="cultural_fit", confidence=0.9, evidence_chunk_ids=[]
            ),
        ]
    )
    assert _motivation_score(ev, weights=DEFAULT_WEIGHTS) == pytest.approx(
        0.45
    )  # 0.9 / 2


def test_motivation_score_all_unverified_is_zero() -> None:
    ev = EvidenceObject(
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation", confidence=0.5, evidence_chunk_ids=["cl_1"]
            ),
        ]
    )
    assert _motivation_score(ev, weights=DEFAULT_WEIGHTS) == 0.0


# ── stage4_combine ───────────────────────────────────────────────────────────


def _breakdown(**overrides: Any) -> ScoreBreakdown:
    base: dict[str, Any] = {
        "skill": 0.8,
        "experience": 0.8,
        "education": 0.8,
        "seniority": 0.8,
        "vector": 0.8,
        "structured": 0.8,
    }
    base.update(overrides)
    return ScoreBreakdown(**base)


def test_stage4_combine_empty_input_returns_empty_list() -> None:
    assert stage4_combine([], DEFAULT_WEIGHTS) == []


def test_stage4_combine_ranks_descending_by_final_score() -> None:
    low_id, high_id = uuid4(), uuid4()
    low = _CombineInput(
        resume_id=low_id,
        structured=0.3,
        breakdown=_breakdown(structured=0.3),
        evidence=None,
    )
    high = _CombineInput(
        resume_id=high_id,
        structured=0.9,
        breakdown=_breakdown(structured=0.9),
        evidence=None,
    )
    entries = stage4_combine([low, high], DEFAULT_WEIGHTS)
    assert entries[0].resume_id == high_id
    assert entries[0].rank == 1
    assert entries[1].resume_id == low_id
    assert entries[1].rank == 2


def test_stage4_combine_final_score_is_weighted_sum() -> None:
    rid = uuid4()
    ev = EvidenceObject(
        requirements=[
            RequirementEvidence(requirement="Python", status="met", confidence=0.9)
        ]
    )
    combine_in = _CombineInput(
        resume_id=rid, structured=0.8, breakdown=_breakdown(structured=0.8), evidence=ev
    )
    entries = stage4_combine([combine_in], DEFAULT_WEIGHTS)
    [entry] = entries
    expected = (
        DEFAULT_WEIGHTS.structured * 0.8
        + DEFAULT_WEIGHTS.evidence * 1.0
        + DEFAULT_WEIGHTS.motivation * 0.0
    )
    assert entry.score_final == pytest.approx(expected)
    assert entry.score_structured == 0.8
    assert entry.score_evidence == 1.0


def test_stage4_combine_writes_computed_motivation_into_breakdown() -> None:
    """The deterministic motivation sub-score is surfaced back onto
    ``breakdown.motivation`` so the cover-letter contribution is auditable."""
    rid = uuid4()
    ev = EvidenceObject(
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation", confidence=0.9, evidence_chunk_ids=["cl_1"]
            )
        ]
    )
    combine_in = _CombineInput(
        resume_id=rid,
        structured=0.5,
        breakdown=_breakdown(structured=0.5, motivation=0.0),
        evidence=ev,
    )
    entries = stage4_combine([combine_in], DEFAULT_WEIGHTS)
    [entry] = entries
    assert entry.breakdown.motivation == pytest.approx(0.9)
