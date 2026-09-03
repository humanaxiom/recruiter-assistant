"""RED — the manager-prompt sub-score has to reach ``score_final``.

SPONSOR 2026-09-02 §I4. Three pieces already exist and are individually green:
the extraction (`manager_prompt_v1`), the scorer (`score_manager_prompt`), and
the combine term (`_combine_final`). This file pins the wiring BETWEEN them,
which is the part that has already gone wrong once on this branch — the 0.10
weight was declared, validated, surfaced on the breakdown, and multiplied in by
nothing, and no gate could see it because uniform deflation reorders nobody.

The chain has four links and a break in any one is silent:

    jobs.additional_requirements_parsed
      -> JobView.manager_requirements          (the read)
      -> Stage2Candidate.manager_prompt        (the score)
      -> _CombineInput.manager_prompt          (the combine)
      -> ScoreBreakdown.manager_prompt(_measured)

Each link is asserted separately, because a test that only checks the far end
tells you the chain is broken without telling you where.

**The contributions travel with it.** A 10% share of a hiring decision must be
able to say WHICH of the manager's requirements it is about — this repo's
"never a number without a cited source" rule, applied to a sub-score whose
source is a free-text note rather than a résumé quote.

None of this is wired yet — RED half of the TDD cycle.
"""

from __future__ import annotations

from src.schemas.jobs import ManagerRequirements, Skill
from src.schemas.matching import ScoreBreakdown


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.5,
        experience=0.5,
        education=0.5,
        seniority=0.5,
        vector=0.5,
        structured=0.5,
    )


# ------------------------------------------------------------ link 1: the read


def test_job_view_carries_the_manager_requirements() -> None:
    """A JobView field is what every stage downstream reads. Without it the
    extraction sits in Postgres and the scorer is never called with it."""
    from src.pipeline.matching.orchestrator import JobView

    assert "manager_requirements" in JobView.__dataclass_fields__


def test_the_job_read_selects_the_extraction_column() -> None:
    """The column has to be SELECTed. A field the query never fetches reads
    back None forever, which is indistinguishable from "no manager note" —
    the failure would present as the feature simply never applying."""
    import inspect

    from src.pipeline.matching.orchestrator import load_job_view

    assert "additional_requirements_parsed" in inspect.getsource(load_job_view)


# ----------------------------------------------------------- link 2: the score


def test_stage2_candidate_carries_the_sub_score() -> None:
    from src.pipeline.matching.orchestrator import Stage2Candidate

    fields = Stage2Candidate.__dataclass_fields__
    assert "manager_prompt" in fields


def test_the_sub_score_is_optional_so_no_note_stays_unmeasured() -> None:
    """``None`` must survive the whole chain. Defaulting it to 0.0 anywhere
    turns "the manager asked nothing" into "this candidate matched nothing"
    and docks every candidate on the job 10% for an unasked question."""
    from src.pipeline.matching.orchestrator import Stage2Candidate

    c = Stage2Candidate(
        resume_id=__import__("uuid").uuid4(),
        vec_score=0.5,
        structured=0.5,
        breakdown=_breakdown(),
    )
    assert c.manager_prompt is None


# --------------------------------------------------------- link 3: the combine


def test_the_orchestrator_passes_the_sub_score_into_the_combine() -> None:
    """The link that broke last time. ``_CombineInput`` accepts the value; what
    this asserts is that ``generate_shortlist`` actually supplies it, rather
    than letting it default to None for every candidate on every job."""
    import inspect

    from src.pipeline.matching.orchestrator import generate_shortlist

    source = inspect.getsource(generate_shortlist)
    assert "manager_prompt=" in source, (
        "generate_shortlist builds _CombineInput without manager_prompt — the "
        "sub-score is computed and then dropped on the floor, which is exactly "
        "how the weight itself came to be applied by nothing"
    )


# ------------------------------------------------------ link 4: the disclosure


def test_the_breakdown_carries_the_manager_contributions() -> None:
    """Provenance. The shortlist must be able to show which requirements came
    from the manager rather than the posting — the question the whole field
    exists to make answerable."""
    b = ScoreBreakdown(
        skill=0.5,
        experience=0.5,
        education=0.5,
        seniority=0.5,
        vector=0.5,
        structured=0.5,
        manager_prompt_contributions=[],
    )
    assert b.manager_prompt_contributions == []


def test_manager_contributions_default_empty_for_pre_feature_rows() -> None:
    """``score_breakdown`` is persisted verbatim; a row written before this
    field existed must still validate."""
    assert _breakdown().manager_prompt_contributions == []


def test_manager_contributions_are_separate_from_the_jd_ones() -> None:
    """Two distinct lists, because they answer different questions: one is
    "what did the posting require", the other "what did the manager add". A
    single merged list cannot tell a reviewer which is which, and that
    distinction is the reason the note is not simply appended to the JD."""
    from src.schemas.matching import SkillContribution

    b = ScoreBreakdown(
        skill=0.5,
        experience=0.5,
        education=0.5,
        seniority=0.5,
        vector=0.5,
        structured=0.5,
        skill_contributions=[SkillContribution(skill="Python", score=1.0)],
        manager_prompt_contributions=[SkillContribution(skill="MEG", score=0.0)],
    )
    assert [c.skill for c in b.skill_contributions] == ["Python"]
    assert [c.skill for c in b.manager_prompt_contributions] == ["MEG"]


def test_the_requirements_shape_round_trips_through_the_job_view() -> None:
    """Sanity on the payload itself: what the extraction writes is what the
    scorer reads, with no lossy re-shaping in between."""
    from src.pipeline.matching.orchestrator import JobView

    reqs = ManagerRequirements(must_have_skills=[Skill(name="MEG")])
    view = JobView(
        id=__import__("uuid").uuid4(),
        title="Analyst",
        min_years=None,
        education_min_level=None,
        education_fields=(),
        required_skills=(),
        nice_to_have_skills=(),
        manager_requirements=reqs,
    )
    assert view.manager_requirements is not None
    assert view.manager_requirements.must_have_skills[0].name == "MEG"
