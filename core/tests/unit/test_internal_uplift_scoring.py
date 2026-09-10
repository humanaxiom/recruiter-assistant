"""RED — the bounded, disclosed internal-status uplift (Sponsor requirements
PR2 slice 3): "APSA/CUPE internal status means SFU employee gets high marks",
resolved by the user as a **bounded uplift inside ``score_final``**, not a
hard band above every external candidate and not a tiebreak — set at
``+0.05`` (5 of 100 points), configurable.

Three constraints this file exists to hold, each named after the defect it
prevents:

1. **The uplift is never a ``MatchWeights`` field.** ``pipeline_meta.weights``
   is a historical reproducibility stamp, read back verbatim off a persisted
   row and validated on the way back in (see
   ``MatchWeights._sums_close_to_one``). A new weight with a non-zero default
   makes every pre-existing stamp fail that validator — the exact 500-on-every-
   shortlist-page defect ``MatchWeights._legacy_stamp_has_no_manager_prompt``'s
   own docstring already survived once for ``manager_prompt``. The uplift
   therefore lives OFF ``MatchWeights`` entirely, and
   ``test_match_weights_rejects_an_internal_uplift_field_it_must_never_carry``
   below asserts the schema itself refuses it.

2. ``_combine_final`` (the documented "top-level blend, in ONE place") stays
   UNCHANGED. The uplift is added AFTER it, in ``stage4_combine`` and
   ``run_match``, and clamped: ``final = min(1.0, base + uplift)``.

3. **``rank_job_matches`` (the reverse résumé→jobs ranker) is untouched.** It
   already omits ``manager_prompt``/``motivation`` — a pre-existing,
   out-of-scope gap — and must not quietly grow the uplift as a second term to
   go missing in a path nobody checks.

Most of this file drives ``stage4_combine``/``run_match`` directly with
``_CombineInput``/``RankInput`` (mirroring
``test_top_blend_is_fully_applied.py``'s own shape), rather than the full
async DB-backed pipeline — the uplift is pure arithmetic over two booleans,
and that is the layer at which it can go missing.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.pipeline.matching.orchestrator import (
    JobView,
    RankInput,
    _JobScore,
    rank_job_matches,
    run_match,
)
from src.pipeline.matching.stages import _combine_final, _CombineInput, stage4_combine
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    MatchWeights,
    PipelineMeta,
    ScoreBreakdown,
)

_UPLIFT = 0.05

_TS = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)


def _breakdown(**over: Any) -> ScoreBreakdown:
    base: dict[str, Any] = dict(
        skill=0.6,
        experience=0.55,
        education=0.4,
        seniority=0.5,
        vector=0.45,
        structured=0.52,
    )
    base.update(over)
    return ScoreBreakdown(**base)


def _combine_score(**flags: Any) -> float:
    combine_in = _CombineInput(
        resume_id=uuid4(),
        structured=0.52,
        breakdown=_breakdown(),
        evidence=None,
        **flags,
    )
    [entry] = stage4_combine([combine_in], DEFAULT_WEIGHTS)
    return entry.score_final


def _run_match_score(**flags: Any) -> float:
    rank_in = RankInput(
        resume_id="r1",
        structured=0.52,
        breakdown=_breakdown(),
        evidence=None,
        **flags,
    )
    [match] = run_match([rank_in], DEFAULT_WEIGHTS)
    return match.score_final


# ---------------------------------------------------- default-absent (inert)


def test_default_absent_internal_flags_produce_an_unchanged_score_final() -> None:
    """Constructing ``_CombineInput``/``RankInput`` exactly as every
    pre-existing call site already does — no ``internal_apsa``/
    ``internal_cupe`` kwarg at all — must produce the SAME ``score_final`` as
    before this slice. This is what proves the feature is inert until a
    roster is actually ingested; it does not reference ``internal_apsa``/
    ``internal_cupe``/the uplift anywhere, so it already passes today and
    stays green across the change."""
    expected = _combine_final(
        structured=0.52,
        evidence_completeness=0.0,
        motivation=0.0,
        manager_prompt=None,
        weights=DEFAULT_WEIGHTS,
    )
    assert _combine_score() == pytest.approx(expected)
    assert _run_match_score() == pytest.approx(expected)


# --------------------------------------------------------------- applied


def test_internal_apsa_uplift_is_added_after_the_blend_in_both_combine_sites() -> None:
    base = _combine_score()
    assert _combine_score(internal_apsa=True) == pytest.approx(base + _UPLIFT)
    assert _run_match_score(internal_apsa=True) == pytest.approx(base + _UPLIFT)


def test_the_uplift_amount_is_configurable_as_a_stage4_combine_kwarg() -> None:
    """+0.05 is the sponsor's chosen DEFAULT, not a hardcoded constant --
    both combine sites must accept an override."""
    combine_in = _CombineInput(
        resume_id=uuid4(),
        structured=0.52,
        breakdown=_breakdown(),
        evidence=None,
        internal_apsa=True,
    )
    [default_entry] = stage4_combine([combine_in], DEFAULT_WEIGHTS)
    [overridden_entry] = stage4_combine(
        [combine_in], DEFAULT_WEIGHTS, internal_uplift_amount=0.2
    )
    assert overridden_entry.score_final == pytest.approx(
        default_entry.score_final - _UPLIFT + 0.2
    )

    rank_in = RankInput(
        resume_id="r1",
        structured=0.52,
        breakdown=_breakdown(),
        evidence=None,
        internal_apsa=True,
    )
    [default_match] = run_match([rank_in], DEFAULT_WEIGHTS)
    [overridden_match] = run_match(
        [rank_in], DEFAULT_WEIGHTS, internal_uplift_amount=0.2
    )
    assert overridden_match.score_final == pytest.approx(
        default_match.score_final - _UPLIFT + 0.2
    )


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param({"internal_apsa": True, "internal_cupe": False}, id="apsa-only"),
        pytest.param({"internal_apsa": False, "internal_cupe": True}, id="cupe-only"),
        pytest.param(
            {"internal_apsa": True, "internal_cupe": True}, id="both-flags-set"
        ),
    ],
)
def test_either_flag_suffices_and_both_together_do_not_double_the_uplift(
    flags: dict[str, bool],
) -> None:
    """The real roster has a row carrying both flags (11 of 315 rows carry at
    least one). A candidate with both APSA and CUPE gets ONE uplift, not
    two."""
    base = _combine_score()
    assert _combine_score(**flags) == pytest.approx(base + _UPLIFT)
    assert _run_match_score(**flags) == pytest.approx(base + _UPLIFT)


# --------------------------------------------------------------- clamped


def test_the_uplift_clamps_at_1_0_rather_than_overshooting() -> None:
    """A near-perfect (but not perfect) candidate plus the uplift must not
    exceed 1.0. The PERFECT-candidate variant of this guard lives beside
    ``test_top_blend_is_fully_applied.py``'s own perfect-candidate fixtures
    (same invariant, same file) -- this one uses an ordinary, non-perfect
    candidate close enough to 1.0 that an unclamped add would overshoot."""
    breakdown = _breakdown(
        skill=1.0,
        experience=1.0,
        education=1.0,
        seniority=1.0,
        vector=1.0,
        structured=1.0,
    )
    combine_in = _CombineInput(
        resume_id=uuid4(),
        structured=1.0,
        breakdown=breakdown,
        evidence=None,
        internal_apsa=True,
    )
    [entry] = stage4_combine([combine_in], DEFAULT_WEIGHTS)
    assert entry.score_final <= 1.0
    assert entry.score_final == pytest.approx(1.0)

    rank_in = RankInput(
        resume_id="r1",
        structured=1.0,
        breakdown=breakdown,
        evidence=None,
        internal_apsa=True,
    )
    [match] = run_match([rank_in], DEFAULT_WEIGHTS)
    assert match.score_final <= 1.0
    assert match.score_final == pytest.approx(1.0)


# ------------------------------------------- the two combine sites agree


def test_the_two_combine_implementations_agree_on_the_internal_uplift() -> None:
    """Mirrors ``test_the_two_combine_implementations_agree`` in
    ``test_top_blend_is_fully_applied.py`` -- they are the same formula
    written twice, so a shared omission of the uplift in both would
    otherwise need two separate failures before anyone noticed they had
    diverged."""
    breakdown = _breakdown()
    [entry] = stage4_combine(
        [
            _CombineInput(
                resume_id=uuid4(),
                structured=0.7,
                breakdown=breakdown,
                evidence=None,
                manager_prompt=0.5,
                internal_cupe=True,
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
                evidence=None,
                manager_prompt=0.5,
                internal_cupe=True,
            )
        ],
        DEFAULT_WEIGHTS,
    )
    assert entry.score_final == pytest.approx(match.score_final)


# --------------------------------------------- rank_job_matches is untouched


def test_rank_job_matches_formula_is_unchanged_and_accepts_no_flags() -> None:
    """Pin BOTH halves of "do not touch the third combine site":

    * ``_JobScore`` (the reverse ranker's ONLY input type) has no
      ``internal_apsa``/``internal_cupe`` field to plumb the uplift through at
      all -- constructing one with either raises ``TypeError`` today, and
      must keep doing so.
    * ``rank_job_matches``' own formula is exactly
      ``weights.structured * structured + weights.evidence *
      evidence_completeness``, unchanged by this slice.

    This test needs no new fixture-building to prove the point -- both
    assertions already hold against the code as it stands before this slice's
    implementation lands, which is deliberate: the point is that nothing
    *ever* wires the uplift in here, not that something is removed.
    """
    job = JobView(
        id=uuid4(),
        title="Data Analyst",
        min_years=None,
        education_min_level=None,
        education_fields=(),
        required_skills=("sql",),
        nice_to_have_skills=(),
    )
    breakdown = _breakdown()
    score = _JobScore(job=job, structured=0.75, breakdown=breakdown, evidence=None)
    [entry] = rank_job_matches([score], DEFAULT_WEIGHTS)
    expected = DEFAULT_WEIGHTS.structured * 0.75 + DEFAULT_WEIGHTS.evidence * 0.0
    assert entry.score_final == pytest.approx(expected)

    with pytest.raises(TypeError):
        _JobScore(
            job=job,
            structured=0.75,
            breakdown=breakdown,
            evidence=None,
            internal_apsa=True,  # type: ignore[call-arg]
        )


# --------------------------------------------------------- PipelineMeta stamp


def _meta(**over: Any) -> PipelineMeta:
    base: dict[str, Any] = dict(
        model_gen="gpt-oss:20b",
        model_emb="nomic-embed-text",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=DEFAULT_WEIGHTS,
        generated_at=_TS,
    )
    base.update(over)
    return PipelineMeta(**base)


def test_match_weights_rejects_an_internal_uplift_field_it_must_never_carry() -> None:
    """The schema-level half of constraint 1. ``MatchWeights`` is
    ``extra="forbid"``; this must stay true of ``internal_uplift_amount``
    specifically, not just true in general, so a future change that adds it
    there fails HERE rather than 500ing every legacy ``pipeline_meta`` stamp
    the way ``manager_prompt`` almost did."""
    with pytest.raises(ValidationError):
        MatchWeights(internal_uplift_amount=0.05)  # type: ignore[call-arg]


def test_internal_uplift_amount_is_a_sibling_not_nested_inside_weights() -> None:
    meta = _meta(internal_uplift_amount=0.05)
    assert meta.internal_uplift_amount == pytest.approx(0.05)
    # `weights` itself carries nothing new -- the sibling-field placement is
    # the whole point of constraint 1.
    assert "internal_uplift_amount" not in meta.weights.model_dump()


def test_a_legacy_pipeline_meta_blob_with_no_internal_uplift_key_still_parses() -> None:
    """No ``mode="before"`` shim is needed here, unlike ``MatchWeights``' own
    ``_legacy_stamp_has_no_manager_prompt``. That shim exists ONLY because a
    missing ``manager_prompt`` key would otherwise make ``MatchWeights``'
    ``_sums_close_to_one`` validator fail on a payload that used to be valid
    -- an absent key is indistinguishable from a wrong sum without rewriting
    it first. ``PipelineMeta`` has no such validator over
    ``internal_uplift_amount``: a missing optional field simply takes its
    ordinary pydantic default, exactly like ``git_sha`` or ``timings_ms``
    already do on a pre-existing legacy blob.
    """
    legacy_blob: dict[str, Any] = {
        "model_gen": "gpt-oss:20b",
        "model_emb": "nomic-embed-text",
        "prompt_versions": {"shortlist_evidence": "shortlist_evidence_v1"},
        "weights": {"structured": 0.6, "evidence": 0.3, "motivation": 0.1},
        "generated_at": _TS.isoformat(),
    }
    meta = PipelineMeta.model_validate(legacy_blob)
    assert meta.internal_uplift_amount == 0.0
