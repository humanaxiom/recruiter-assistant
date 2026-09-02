"""RED — every weight in the top-level blend must actually reach score_final.

This file exists because of a defect introduced on 2026-09-02 and caught by
reading the combine rather than by any gate.

``MatchWeights`` gained ``manager_prompt = 0.10`` (sponsor §I4) and its
sums-to-1.0 validator was widened to count it. Both facts were true, tested,
and green. What nothing checked is that ``stage4_combine`` **multiplies it into
the score** — and it did not. The blend applied was
``0.6·structured + 0.3·evidence + 0.0·motivation``, summing to 0.9, so every
``score_final`` came out uniformly 10% low.

**Why every existing gate missed it.** The deflation is uniform, so it moves no
candidate relative to any other. `ranking-evals` is an ORDERING gate —
precision@k, the adversarial backstop, the twin pairs — and every one of them
was satisfied by a corpus scaled by 0.9. The unit suite asserted that the
weights sum to 1.0 (they do) and that the breakdown carries the sub-score (it
does). The one thing nobody asserted is the thing that matters: that the
arithmetic uses what the schema declares.

That is this repository's characteristic defect stated exactly — an invariant
declared in one place with nothing enforcing that the code honours it — and it
reappeared in the change that added a weight, which is the single most likely
moment for it to reappear again.

**The guard is deliberately shaped to survive future weights.** It does not
enumerate the terms; it drives every sub-score to 1.0 and asserts
``score_final == 1.0``. A perfect candidate must score a perfect 1.0, because
the weights sum to 1.0 by their own validator. Add a fifth top-level weight and
forget to apply it, and this fails with a number that names the gap — without
anyone remembering to update this file.
"""

from __future__ import annotations

import pytest

from src.pipeline.matching.orchestrator import RankInput, run_match
from src.pipeline.matching.stages import stage4_combine
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    CoverLetterEvidence,
    EvidenceObject,
    MatchWeights,
    RequirementEvidence,
    ScoreBreakdown,
)


def _perfect_breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=1.0,
        experience=1.0,
        education=1.0,
        seniority=1.0,
        vector=1.0,
        structured=1.0,
    )


def _perfect_evidence() -> EvidenceObject:
    """Evidence that maxes BOTH derived sub-scores: every requirement met with
    full confidence (completeness = 1.0) and a cited cover-letter theme above
    ``motivation_min_confidence`` (motivation = 1.0)."""
    return EvidenceObject(
        requirements=[
            RequirementEvidence(
                requirement="Python",
                status="met",
                evidence="ten years of production Python across three teams",
                confidence=1.0,
                evidence_chunk_ids=["c_001"],
            )
        ],
        cover_letter_evidence=[
            CoverLetterEvidence(
                theme="motivation",
                evidence="I have wanted to work on this problem for years",
                confidence=1.0,
                evidence_chunk_ids=["cl_001"],
            )
        ],
    )


# ``motivation`` is 0.0 by default since the sponsor's §O3 answer, so the
# default blend cannot exercise the motivation term. Both blends are checked:
# the shipping default, and one where every top-level term carries weight.
_BLENDS = [
    pytest.param(DEFAULT_WEIGHTS, id="shipping-default"),
    pytest.param(
        MatchWeights(structured=0.4, evidence=0.3, motivation=0.2, manager_prompt=0.1),
        id="all-four-terms-weighted",
    ),
]


@pytest.mark.parametrize("weights", _BLENDS)
def test_stage4_combine_applies_the_whole_top_blend(weights: MatchWeights) -> None:
    """A candidate perfect on every dimension must score exactly 1.0.

    Any top-level weight the combine forgets to multiply in shows up here as a
    shortfall equal to that weight.
    """
    [entry] = stage4_combine(
        [
            _CombineInput(
                resume_id="r1",
                structured=1.0,
                breakdown=_perfect_breakdown(),
                evidence=_perfect_evidence(),
                manager_prompt=1.0,
            )
        ],
        weights,
    )
    assert entry.score_final == pytest.approx(1.0), (
        f"a candidate perfect on every dimension scored {entry.score_final:.4f}, "
        f"not 1.0 — the combine is dropping {1.0 - entry.score_final:.4f} of "
        "weight that MatchWeights declares. Some top-level term is validated "
        "but never multiplied into score_final."
    )


@pytest.mark.parametrize("weights", _BLENDS)
def test_run_match_applies_the_whole_top_blend(weights: MatchWeights) -> None:
    """``run_match`` duplicates ``stage4_combine``'s arithmetic for the eval
    harness. Two copies of a formula drift; both are asserted."""
    [match] = run_match(
        [
            RankInput(
                resume_id="r1",
                structured=1.0,
                breakdown=_perfect_breakdown(),
                evidence=_perfect_evidence(),
                manager_prompt=1.0,
            )
        ],
        weights,
    )
    assert match.score_final == pytest.approx(1.0), (
        f"run_match scored a perfect candidate {match.score_final:.4f}, not 1.0 "
        "— it has drifted from stage4_combine's blend"
    )


def test_the_two_combine_implementations_agree() -> None:
    """They are the same formula written twice, so pin them against each other
    directly rather than only against 1.0 — a shared omission in both would
    otherwise need two tests to fail before anyone noticed they had diverged."""
    breakdown, evidence = _perfect_breakdown(), _perfect_evidence()
    [entry] = stage4_combine(
        [
            _CombineInput(
                resume_id="r1",
                structured=0.7,
                breakdown=breakdown,
                evidence=evidence,
                manager_prompt=0.5,
            )
        ],
        DEFAULT_WEIGHTS,
    )
    [match] = run_match(
        [
            RankInput(
                resume_id="r1",
                structured=0.7,
                breakdown=breakdown,
                evidence=evidence,
                manager_prompt=0.5,
            )
        ],
        DEFAULT_WEIGHTS,
    )
    assert entry.score_final == pytest.approx(match.score_final)


def test_a_job_with_no_manager_prompt_scores_as_it_did_before_the_term() -> None:
    """SPONSOR §I4 — the unasked question, and why it is DISCLOSED not fixed.

    A requisition whose manager typed nothing contributes 0.0 on this term, so
    its candidates sit 10% below a theoretical 1.0. That is a fabricated zero,
    and it is deliberately left in place:

    * It reproduces EXACTLY what shipped before. ``motivation`` used to hold
      this 0.10 and a candidate with no cover letter scored 0.0 on it, so such
      a job now scores byte-identically to the same job last week. **No live
      shortlist moves.** The assertion below is written as that equivalence
      rather than as a literal, so it keeps its meaning if the weights change.
    * Renormalising the surviving weights is the better answer and is **not
      ours to make** — ROADMAP §5 records it as open and owned by HR. Settling
      it inside a change about something else is how a hiring policy gets
      rewritten by an implementation detail.

    What must hold instead is that the zero is *marked* as unmeasured, so the
    "Why this rank?" panel can say "no additional requirements were set" rather
    than asserting the candidate matched none of them.
    """
    [entry] = stage4_combine(
        [
            _CombineInput(
                resume_id="r1",
                structured=1.0,
                breakdown=_perfect_breakdown(),
                evidence=_perfect_evidence(),
                manager_prompt=None,  # no prompt on this job
            )
        ],
        DEFAULT_WEIGHTS,
    )
    pre_feature_blend = (
        DEFAULT_WEIGHTS.structured * 1.0
        + DEFAULT_WEIGHTS.evidence * 1.0
        + DEFAULT_WEIGHTS.motivation * 1.0
    )
    assert entry.score_final == pytest.approx(pre_feature_blend), (
        "a job with no manager prompt must score exactly as it did before the "
        "term existed — this change must not move a single live shortlist"
    )
    # The disclosure is the whole mitigation. Without it, "nobody asked" and
    # "matched nothing" are the same stored number.
    assert entry.breakdown.manager_prompt_measured is False
    assert entry.breakdown.manager_prompt == 0.0


def test_the_unasked_zero_is_ordering_neutral() -> None:
    """The deflation is uniform across a job, so it cannot reorder anyone.

    That is what makes leaving it in place safe: the number is understated,
    but no candidate is disadvantaged relative to another.
    """
    strong = _CombineInput(
        resume_id="strong",
        structured=0.9,
        breakdown=_perfect_breakdown(),
        evidence=_perfect_evidence(),
        manager_prompt=None,
    )
    weak = _CombineInput(
        resume_id="weak",
        structured=0.3,
        breakdown=_perfect_breakdown(),
        evidence=_perfect_evidence(),
        manager_prompt=None,
    )
    ranked = stage4_combine([weak, strong], DEFAULT_WEIGHTS)
    assert [e.resume_id for e in ranked] == ["strong", "weak"]
    assert [e.rank for e in ranked] == [1, 2]


def test_a_measured_manager_prompt_is_marked_measured() -> None:
    [entry] = stage4_combine(
        [
            _CombineInput(
                resume_id="r1",
                structured=1.0,
                breakdown=_perfect_breakdown(),
                evidence=_perfect_evidence(),
                manager_prompt=0.25,
            )
        ],
        DEFAULT_WEIGHTS,
    )
    assert entry.breakdown.manager_prompt_measured is True
    assert entry.breakdown.manager_prompt == pytest.approx(0.25)


# Imported last so the module still collects while ``_CombineInput`` does not
# yet carry ``manager_prompt`` (the RED state).
from src.pipeline.matching.stages import _CombineInput  # noqa: E402
