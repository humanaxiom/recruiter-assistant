"""RED — the sponsor's "additional requirements" prompt (2026-09-02 §I4/§O1).

The hiring manager can list special skills / experience they are looking for
that are NOT in the job posting. Those requirements must:

* ride on the job as their own field, NEVER folded into ``description_raw``
  (which is the JD of record and must stay byte-faithful to the posting),
* extract into the same ``Skill`` shapes ``JDExtracted`` produces, so the
  ranking engine needs no second requirement representation,
* carry PROVENANCE, so a shortlist can say *why* a requirement is scored —
  this repo's "never a number without a cited source" rule applied to the
  requirement side rather than the evidence side,
* be **must-have by default** (that is what "special skills I am looking for"
  means), and
* carry the **10% weight the sponsor reassigned off the cover letter**
  (§O3 — a cover letter must be identified but must not rank).

The weight move is ADDITIVE, not a rename: ``motivation`` stays on
``MatchWeights``/``ScoreBreakdown`` defaulting to 0.0 so every already-persisted
``score_breakdown`` jsonb still validates and a job can opt the old behaviour
back in. That is pinned below and is the reason this is not a breaking change.

These contracts do not exist yet — this is the RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

import src.schemas as schemas_pkg
from src.schemas.jobs import JobCreate, JobOut, JobUpdate, ManagerRequirements, Skill
from src.schemas.matching import DEFAULT_WEIGHTS, MatchWeights, ScoreBreakdown

_DESC = "A detailed job description of the role. " * 3
_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------- job fields


def test_job_create_accepts_additional_requirements() -> None:
    job = JobCreate(
        title="Research Analyst",
        description_raw=_DESC,
        additional_requirements="Must have MEG analysis experience and a licence.",
    )
    assert job.additional_requirements is not None
    assert "MEG" in job.additional_requirements


def test_job_create_additional_requirements_defaults_to_none() -> None:
    """Omitting the prompt must leave a job byte-identical to a pre-feature one."""
    job = JobCreate(title="Analyst", description_raw=_DESC)
    assert job.additional_requirements is None


def test_job_create_rejects_an_oversize_prompt() -> None:
    """Capped like every other free-text job field — it rides into a JSONB
    extraction payload and an LLM prompt, so an unbounded value is both a
    storage-growth vector and a token-budget hazard."""
    with pytest.raises(ValidationError):
        JobCreate(
            title="Analyst",
            description_raw=_DESC,
            additional_requirements="x" * 4001,
        )


def test_job_update_can_clear_and_set_the_prompt() -> None:
    """PATCH omit means "unchanged", matching every other JobUpdate field."""
    assert JobUpdate().additional_requirements is None
    patch = JobUpdate(additional_requirements="Kafka in production")
    assert patch.additional_requirements == "Kafka in production"


def test_job_out_exposes_the_prompt_and_its_extraction() -> None:
    out = JobOut(
        id=uuid4(),
        title="Analyst",
        department=None,
        location=None,
        employment_type=None,
        seniority=None,
        min_years=None,
        description_raw=_DESC,
        description_parsed=None,
        status="draft",
        retention_days=180,
        shortlist_top_percent=100,
        failure_reason=None,
        created_by=None,
        created_at=_TS,
        updated_at=_TS,
        parsed_at=None,
        closed_at=None,
        additional_requirements="Kafka in production",
        additional_requirements_parsed=ManagerRequirements(
            must_have_skills=[Skill(name="Kafka")]
        ),
    )
    assert out.additional_requirements_parsed is not None
    assert out.additional_requirements_parsed.must_have_skills[0].name == "Kafka"


def test_job_out_prompt_fields_default_to_none_for_pre_feature_rows() -> None:
    """A row written before this feature has neither column populated; reading
    it back must not require the caller to supply them."""
    out = JobOut(
        id=uuid4(),
        title="Analyst",
        department=None,
        location=None,
        employment_type=None,
        seniority=None,
        min_years=None,
        description_raw=_DESC,
        description_parsed=None,
        status="draft",
        retention_days=180,
        shortlist_top_percent=100,
        failure_reason=None,
        created_by=None,
        created_at=_TS,
        updated_at=_TS,
        parsed_at=None,
        closed_at=None,
    )
    assert out.additional_requirements is None
    assert out.additional_requirements_parsed is None


# ------------------------------------------------------- ManagerRequirements


def test_manager_requirements_reuses_the_jd_skill_shape() -> None:
    """Same ``Skill`` model the JD extraction emits — the ranking engine must
    never learn a second requirement representation."""
    mr = ManagerRequirements(
        must_have_skills=[Skill(name="MEG", min_years=3)],
        nice_to_have_skills=[Skill(name="Python")],
        other_requirements=["Willing to work weekends during study periods"],
    )
    assert mr.must_have_skills[0].min_years == 3
    assert mr.nice_to_have_skills[0].name == "Python"
    assert mr.other_requirements == ["Willing to work weekends during study periods"]


def test_manager_requirements_is_empty_by_default() -> None:
    mr = ManagerRequirements()
    assert mr.must_have_skills == []
    assert mr.nice_to_have_skills == []
    assert mr.other_requirements == []


def test_manager_requirements_caps_every_list() -> None:
    """Uncapped LLM-emitted lists are the same unbounded-JSONB hole as an
    uncapped string — every sibling list on ``JDExtracted`` is capped."""
    with pytest.raises(ValidationError):
        ManagerRequirements(must_have_skills=[Skill(name=f"s{i}") for i in range(51)])
    with pytest.raises(ValidationError):
        ManagerRequirements(
            nice_to_have_skills=[Skill(name=f"s{i}") for i in range(51)]
        )
    with pytest.raises(ValidationError):
        ManagerRequirements(other_requirements=[f"r{i}" for i in range(21)])


def test_manager_requirements_is_exported_from_the_schemas_package() -> None:
    assert schemas_pkg.ManagerRequirements is ManagerRequirements
    assert "ManagerRequirements" in schemas_pkg.__all__


# ------------------------------------------------------------ the 10% move


def test_default_weights_move_the_ten_percent_off_the_cover_letter() -> None:
    """Sponsor §O3: a cover letter is identified but must NOT rank. The 10%
    it carried is reassigned to the manager's own requirements (§I4)."""
    assert DEFAULT_WEIGHTS.motivation == 0.0
    assert DEFAULT_WEIGHTS.manager_prompt == pytest.approx(0.10)


def test_top_level_weights_still_sum_to_one() -> None:
    w = DEFAULT_WEIGHTS
    total = w.structured + w.evidence + w.motivation + w.manager_prompt
    assert total == pytest.approx(1.0)


def test_the_sum_validator_counts_the_new_term() -> None:
    """The validator must not be satisfiable by ignoring ``manager_prompt`` —
    that is precisely how a weight silently stops contributing."""
    with pytest.raises(ValidationError, match="sum to 1.0"):
        MatchWeights(structured=0.6, evidence=0.3, motivation=0.0, manager_prompt=0.0)
    with pytest.raises(ValidationError, match="sum to 1.0"):
        MatchWeights(structured=0.6, evidence=0.3, motivation=0.1, manager_prompt=0.1)


def test_a_legacy_weights_stamp_still_validates() -> None:
    """REGRESSION — every ``pipeline_meta.weights`` stamp already persisted on
    the pilot box predates ``manager_prompt`` and reads
    ``{structured: 0.6, evidence: 0.3, motivation: 0.1}``.

    Give that payload the new field's 0.10 default and it sums to 1.10, the
    sum validator rejects it, and the API read path — which validates
    UNCAUGHT — 500s on every shortlist page for every job ranked before this
    change. A pre-feature stamp genuinely carried no manager-prompt weight, so
    it reads back as 0.0.
    """
    legacy = {
        "structured": 0.6,
        "evidence": 0.3,
        "motivation": 0.1,
        "skill": 0.40,
        "experience": 0.25,
        "education": 0.10,
        "seniority": 0.15,
        "vector": 0.10,
    }
    w = MatchWeights.model_validate(legacy)
    assert w.manager_prompt == 0.0
    assert w.motivation == pytest.approx(0.1)


def test_the_legacy_shim_does_not_mask_a_genuinely_bad_blend() -> None:
    """The shim keys off "``motivation`` named, ``manager_prompt`` absent". A
    payload that names BOTH is new code and gets no forgiveness — otherwise
    the shim would quietly absorb a real misconfiguration."""
    with pytest.raises(ValidationError, match="sum to 1.0"):
        MatchWeights.model_validate(
            {
                "structured": 0.6,
                "evidence": 0.3,
                "motivation": 0.5,
                "manager_prompt": 0.1,
            }
        )


def test_the_old_blend_is_still_expressible_per_job() -> None:
    """A requisition that wants the pre-2026-09-02 cover-letter behaviour back
    must be able to say so — the move is a DEFAULT change, not a deletion."""
    w = MatchWeights(structured=0.6, evidence=0.3, motivation=0.1, manager_prompt=0.0)
    assert w.motivation == pytest.approx(0.1)
    assert w.manager_prompt == 0.0


# ------------------------------------------------------------ ScoreBreakdown


def _breakdown(**kw: object) -> ScoreBreakdown:
    base: dict[str, object] = {
        "skill": 0.5,
        "experience": 0.5,
        "education": 0.5,
        "seniority": 0.5,
        "vector": 0.5,
        "structured": 0.5,
    }
    base.update(kw)
    return ScoreBreakdown(**base)  # type: ignore[arg-type]


def test_breakdown_carries_the_manager_prompt_sub_score() -> None:
    assert _breakdown(manager_prompt=0.75).manager_prompt == pytest.approx(0.75)


def test_breakdown_manager_prompt_defaults_to_zero_for_pre_feature_rows() -> None:
    """``shortlist_entries.score_breakdown`` is persisted verbatim. A row
    written before this feature has no such key and must still validate."""
    assert _breakdown().manager_prompt == 0.0


def test_breakdown_still_round_trips_a_persisted_motivation() -> None:
    """The move must not orphan the thousands of already-persisted breakdowns
    that carry a real motivation score."""
    assert _breakdown(motivation=0.8).motivation == pytest.approx(0.8)


def test_breakdown_carries_a_manager_prompt_measurement_marker() -> None:
    """ADR-041's three-state contract: True = the comparison ran, False = no
    prompt was supplied so the stored 0.0 is a fallback rather than a
    measurement, None = the row predates the marker. Without this, a job with
    no manager prompt is indistinguishable from a candidate who matched none
    of it — the exact "fabricated zero" this repo has shipped before."""
    assert _breakdown().manager_prompt_measured is None
    assert _breakdown(manager_prompt_measured=False).manager_prompt_measured is False
    assert _breakdown(manager_prompt_measured=True).manager_prompt_measured is True
