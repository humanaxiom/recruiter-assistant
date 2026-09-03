"""RED — scoring a candidate against the hiring manager's own requirements.

SPONSOR 2026-09-02 §I4/§O1. The note is extracted and stored; the 0.10 weight
the sponsor moved off the cover letter is wired into the combine. This is the
function that gives that weight something to measure — until it exists, every
candidate scores ``manager_prompt_measured = False`` forever.

**Deterministic, not LLM.** The manager named specific things; matching them is
a name comparison, not a judgement call. That keeps the sub-score explainable
("you asked for MEG; this candidate lists MEG"), free, and immune to the
stage-3 fail-closed path. It also means the number can be defended in a review
without re-running a model.

**It matches through the same canonicalisation the rest of the engine uses**,
which matters more here than anywhere else: ROADMAP open item 3 measures the
vocabulary as recognising only 54.8% of real SFU qualification statements, and
the whole point of this field is the requirements the posting missed — so the
terms a manager types are *disproportionately likely to be out of vocabulary*.
``_basic_normalise`` returns an unresolved name unchanged, so vocab terms
alias-resolve ("Postgres" ≡ "PostgreSQL") and non-vocab terms still match on
their normalised form. Both paths are pinned below.

**It returns ``None``, not 0.0, when there is nothing to measure** — no note, or
a note that named no skills at all. That is what keeps "the manager asked for
nothing" distinct from "the candidate matched nothing", which is the
distinction ``manager_prompt_measured`` exists to carry.

None of this exists yet — RED half of the TDD cycle.
"""

from __future__ import annotations

import pytest

from src.schemas.jobs import ManagerRequirements, Skill
from src.schemas.matching import DEFAULT_WEIGHTS, MatchWeights


def _score(candidate: list[str], reqs: ManagerRequirements | None, **kw: object):
    from src.pipeline.matching.stages import score_manager_prompt

    return score_manager_prompt(
        candidate, reqs, weights=kw.get("weights", DEFAULT_WEIGHTS)  # type: ignore[arg-type]
    )


# ------------------------------------------------------ nothing to measure


def test_no_note_is_unmeasured() -> None:
    score, contributions = _score(["Python"], None)
    assert score is None
    assert contributions == []


def test_a_note_naming_no_skills_is_unmeasured() -> None:
    """A manager who wrote only a non-skill condition ("willing to travel")
    has asked for nothing this sub-score can measure. Scoring 0.0 would dock
    every candidate on the job 10% for a question the scorer cannot answer —
    the fabricated zero, arriving by a different route."""
    reqs = ManagerRequirements(
        other_requirements=["Willing to travel to Burnaby weekly"]
    )
    score, contributions = _score(["Python"], reqs)
    assert score is None
    assert contributions == []


# ------------------------------------------------------------- must-haves


def test_all_must_haves_matched_scores_one() -> None:
    reqs = ManagerRequirements(
        must_have_skills=[Skill(name="Python"), Skill(name="Kafka")]
    )
    score, _ = _score(["Python", "Kafka", "Docker"], reqs)
    assert score == pytest.approx(1.0)


def test_no_must_haves_matched_scores_zero() -> None:
    """A REAL zero, distinct from the unmeasured None above: the manager asked
    and this candidate answered none of it."""
    reqs = ManagerRequirements(must_have_skills=[Skill(name="Kafka")])
    score, _ = _score(["Python"], reqs)
    assert score == pytest.approx(0.0)


def test_partial_coverage_is_proportional() -> None:
    reqs = ManagerRequirements(
        must_have_skills=[Skill(name="Python"), Skill(name="Kafka")]
    )
    score, _ = _score(["Python"], reqs)
    assert score == pytest.approx(0.5)


# ------------------------------------------------- matching and normalisation


def test_vocabulary_aliases_resolve() -> None:
    """ "Postgres" and "PostgreSQL" are the same requirement. Matching on raw
    strings would fail a candidate for the manager's choice of synonym."""
    reqs = ManagerRequirements(must_have_skills=[Skill(name="Postgres")])
    score, _ = _score(["PostgreSQL"], reqs)
    assert score == pytest.approx(1.0)


def test_out_of_vocabulary_terms_still_match() -> None:
    """The load-bearing case. This field exists for requirements the posting
    missed, and the vocabulary recognises ~55% of real SFU qualification
    statements — so the terms a manager types here are disproportionately
    likely to be unknown to it. An unresolved name must still match itself."""
    reqs = ManagerRequirements(must_have_skills=[Skill(name="MEG analysis")])
    score, _ = _score(["MEG analysis"], reqs)
    assert score == pytest.approx(1.0)


def test_matching_ignores_case_and_whitespace() -> None:
    reqs = ManagerRequirements(must_have_skills=[Skill(name="  kAfKa  ")])
    score, _ = _score(["Kafka"], reqs)
    assert score == pytest.approx(1.0)


def test_an_unmatched_out_of_vocabulary_term_is_a_real_miss() -> None:
    """The normalisation fallback must not become a fuzzy match. "MEG" and
    "EEG" are different techniques and one does not answer the other."""
    reqs = ManagerRequirements(must_have_skills=[Skill(name="MEG")])
    score, _ = _score(["EEG"], reqs)
    assert score == pytest.approx(0.0)


# ----------------------------------------------------------- nice-to-haves


def test_nice_to_haves_count_for_less_than_must_haves() -> None:
    """Sponsor §I4: anything stated plainly is a requirement; a softened one is
    a preference. A candidate who has only the preference must not out-score
    one who has only the requirement."""
    reqs = ManagerRequirements(
        must_have_skills=[Skill(name="Python")],
        nice_to_have_skills=[Skill(name="Kafka")],
    )
    has_must, _ = _score(["Python"], reqs)
    has_nice, _ = _score(["Kafka"], reqs)
    assert has_must is not None and has_nice is not None
    assert has_must > has_nice, (
        "the must-have carries less weight than the nice-to-have — a manager's "
        "requirement is being treated as a preference"
    )


def test_a_note_with_only_nice_to_haves_is_still_measured() -> None:
    """A manager who softened everything still asked a question, and it has an
    answer. Distinct from the no-skills case, which has none."""
    reqs = ManagerRequirements(nice_to_have_skills=[Skill(name="Kafka")])
    score, _ = _score(["Kafka"], reqs)
    assert score == pytest.approx(1.0)


def test_everything_matched_scores_one_regardless_of_the_split() -> None:
    """Whatever the must/nice blend, a candidate holding every named skill is
    a full match — the weighting redistributes emphasis, it must not cap the
    ceiling below 1.0."""
    reqs = ManagerRequirements(
        must_have_skills=[Skill(name="Python")],
        nice_to_have_skills=[Skill(name="Kafka")],
    )
    score, _ = _score(["Python", "Kafka"], reqs)
    assert score == pytest.approx(1.0)


def test_the_nice_to_have_weight_is_configurable() -> None:
    """It is a hiring-policy decimal, so it belongs on ``MatchWeights`` where
    it can be ratified — not as a literal buried in the scorer."""
    reqs = ManagerRequirements(
        must_have_skills=[Skill(name="Python")],
        nice_to_have_skills=[Skill(name="Kafka")],
    )
    low = MatchWeights(manager_prompt_nice_weight=0.1)
    high = MatchWeights(manager_prompt_nice_weight=0.5)
    only_nice_low, _ = _score(["Kafka"], reqs, weights=low)
    only_nice_high, _ = _score(["Kafka"], reqs, weights=high)
    assert only_nice_low is not None and only_nice_high is not None
    assert only_nice_high > only_nice_low


# --------------------------------------------------------- the contributions


def test_every_requirement_is_cited_in_the_contributions() -> None:
    """Never a number without a source. The panel has to be able to say WHICH
    of the manager's requirements this candidate met, or the sub-score is an
    unexplained 10% of a hiring decision."""
    reqs = ManagerRequirements(
        must_have_skills=[Skill(name="Python"), Skill(name="Kafka")],
        nice_to_have_skills=[Skill(name="Docker")],
    )
    _, contributions = _score(["Python"], reqs)

    by_name = {c.skill: c for c in contributions}
    assert set(by_name) == {
        "Python",
        "Kafka",
        "Docker",
    }, "every requirement the manager named must appear, matched or not"
    assert by_name["Python"].score == pytest.approx(1.0)
    assert by_name["Python"].is_must_have is True
    assert by_name["Kafka"].score == pytest.approx(0.0)
    assert by_name["Kafka"].reason == "missing"
    assert by_name["Docker"].is_must_have is False


def test_contributions_carry_the_manager_s_own_wording() -> None:
    """The label a manager reads back must be what they typed, not the
    canonical form the matcher used internally — "Postgres" stays "Postgres".
    The same JD-authored-cleartext rule the skill labels already follow."""
    reqs = ManagerRequirements(must_have_skills=[Skill(name="Postgres")])
    _, contributions = _score(["PostgreSQL"], reqs)
    assert [c.skill for c in contributions] == ["Postgres"]
