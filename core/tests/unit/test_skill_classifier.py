"""Unit tests -- ``src.pipeline.skill_classifier`` (ROADMAP A2, Phase 3.3,
slice 1). All LLM I/O mocked at the ``llm.chat_json`` boundary.

Design (``CLASSIFIER-SPEC.md``, not restated here): a skill name outside the
306-canonical vocabulary is hashed one-way at projection and can never earn
family credit. This module classifies such names into one of the 32
``categories.yaml`` families AT PARSE TIME (where the LLM is already being
called and there is no drain deadline) -- projection itself gains no LLM
call.

``src.pipeline.skill_classifier`` does not exist yet -- this whole file
fails at collection (``ModuleNotFoundError``). RED half of the TDD cycle.

── The LLM response contract this file pins ────────────────────────────────
The spec deliberately leaves the exact ``chat_json`` schema unspecified
("a pydantic model constrained to known_families()"). Since ``llm.chat_json``
is mocked wholesale below (never actually parses JSON), this file pins the
one attribute ``classify_families`` must read off whatever object
``chat_json`` resolves to: a ``.categories`` mapping of
``{skill_name: [family, ...]}`` -- mirroring the function's own public
return shape. A future implementation is free to name its *internal*
pydantic schema class anything it likes, as long as an instance of it
exposes ``.categories`` in this shape.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from src.pipeline import skill_classifier, skills_graph
from src.settings import get_settings

# ── independent oracle for known_families() ─────────────────────────────


def _real_family_keys() -> set[str]:
    """Parses ``categories.yaml`` directly -- NOT via
    ``skills_graph._category_table()`` -- so this is a genuinely independent
    check of ``known_families()``, not a tautology against the same cache."""
    data = (
        yaml.safe_load(skills_graph._CATEGORIES_PATH.read_text(encoding="utf-8")) or {}
    )
    return {str(k).strip().lower() for k in data}


def test_known_families_matches_categories_yaml_exactly() -> None:
    assert set(skill_classifier.known_families()) == _real_family_keys()


def test_known_families_has_thirty_two_families_today() -> None:
    """A count sanity pin -- catches an accidental gutting of the taxonomy
    (or a hard-coded second copy that silently forked from the real file)."""
    assert len(skill_classifier.known_families()) == 32


def test_known_families_returns_a_tuple() -> None:
    result = skill_classifier.known_families()
    assert isinstance(result, tuple)
    assert all(isinstance(f, str) for f in result)


def test_known_families_shares_the_real_category_table_not_a_hardcoded_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift guard: ``known_families`` must read the SAME
    ``skills_graph._category_table()`` the graph itself uses, not a second,
    independently-maintained copy of the family list. Proven by mutating the
    real cached table and confirming ``known_families()`` tracks it."""

    def _stub_category_table() -> dict[str, list[str]]:
        return {"some-skill": ["probe_family_xyz"]}

    monkeypatch.setattr(skills_graph, "_category_table", _stub_category_table)

    assert skill_classifier.known_families() == ("probe_family_xyz",)


# ── unclassified_names: the drift guard ──────────────────────────────────
#
# Requirement (CLASSIFIER-SPEC.md): "It MUST share the existing predicate
# rather than restating it: derive from `_basic_normalise` + the alias/
# category table lookups already used by `_canonical_key_for_normalised`."
# A second, independently-maintained copy of that condition is the A7 drift
# shape this repo has recorded seventeen times.


@pytest.mark.parametrize(
    ("name", "expect_unclassified"),
    [
        ("python", False),  # in-vocab canonical
        ("Py", False),  # alias.yaml alias of "python"
        ("dns", False),  # category-only: in categories.yaml, NOT aliases.yaml
        ("voip", False),  # category-only, multi-family (networking + hardware)
        ("mri methods", True),  # genuinely out of vocab -- the 45.2% case
        ("microfabrication", True),  # genuinely out of vocab
    ],
)
def test_unclassified_names_agrees_with_the_real_hashing_decision(
    name: str, expect_unclassified: bool
) -> None:
    """Cross-checked against the REAL, independent oracle
    (``skills_graph._canonical_key_for_normalised`` on the same normalised
    string) for a table spanning in-vocab, alias, category-only, and
    out-of-vocab names."""
    normalised = skills_graph._basic_normalise(name)
    would_be_hashed = skills_graph._canonical_key_for_normalised(normalised).startswith(
        skills_graph._HASH_KEY_PREFIX
    )
    assert would_be_hashed is expect_unclassified, (
        f"test table itself is wrong for {name!r} -- fix the parametrize case, "
        "not the assertion below"
    )

    result = skill_classifier.unclassified_names([name])
    assert (name in result) is expect_unclassified


def test_unclassified_names_preserves_order_and_drops_only_vocab_hits() -> None:
    names = ["python", "mri methods", "voip", "microfabrication"]
    result = skill_classifier.unclassified_names(names)
    assert result == ["mri methods", "microfabrication"]


def test_unclassified_names_empty_input_returns_empty_list() -> None:
    assert skill_classifier.unclassified_names([]) == []


def test_unclassified_names_all_vocab_returns_empty_list() -> None:
    assert skill_classifier.unclassified_names(["python", "voip", "dns"]) == []


def test_unclassified_names_drift_guard_cannot_silently_diverge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core anti-drift proof: if ``unclassified_names`` shares the real
    predicate (rather than restating it), mutating the REAL alias table both
    it and ``_canonical_key_for_normalised`` read must flip BOTH sides'
    answer for the same probe name together -- they cannot disagree.

    Patches ``_alias_table`` on BOTH ``src.pipeline.skills`` (the single
    source of truth per ``test_skill_normalise_parity.py``) and
    ``src.pipeline.skills_graph`` (which imports the same function object by
    name, so a module-level rebind on one side does not reach the other's
    already-bound reference -- the exact footgun that file's own docstring
    documents) so this test does not care which import path
    ``unclassified_names`` happens to use.
    """
    from src.pipeline import skills

    probe = "zzz-probe-term-never-shipped-in-any-vocab-file"

    # Sanity: today, genuinely out of vocab on both sides.
    assert skills_graph._canonical_key_for_normalised(probe).startswith(
        skills_graph._HASH_KEY_PREFIX
    )
    assert probe in skill_classifier.unclassified_names([probe])

    def _stub_alias_table() -> dict[str, str]:
        return {probe: probe}

    monkeypatch.setattr(skills, "_alias_table", _stub_alias_table)
    monkeypatch.setattr(skills_graph, "_alias_table", _stub_alias_table)

    # The shared predicate now says `probe` IS vocab (cleartext).
    assert skills_graph._canonical_key_for_normalised(probe) == probe

    # If `unclassified_names` shares that predicate rather than restating
    # its own copy, it MUST agree: `probe` drops out of the unclassified set.
    assert probe not in skill_classifier.unclassified_names([probe])


# ── ADR-042: the gate-critical family-less canonicals are structurally
# unreachable by the classifier (never merely untested) ──────────────────


@pytest.mark.parametrize("canonical", ["rest api design", "c++", "hudson", "julia"])
def test_gate_critical_canonicals_are_structurally_unreachable_by_the_classifier(
    canonical: str,
) -> None:
    """ADR-042 pinned these four canonicals family-less
    (``test_skill_vocabulary_families.py::test_gate_critical_canonical_stays_familyless``,
    ~line 209) because giving any of them a family collapses a ranking-evals
    ordering pair's margin 80% while still passing. The classifier here runs
    ONLY on ``unclassified_names()`` output, so an in-vocab canonical like
    these four can never reach it -- unreachable BY CONSTRUCTION, not merely
    untested."""
    assert canonical not in skill_classifier.unclassified_names([canonical])


# ── classify_families: happy path, batched ───────────────────────────────


def _fake_llm(categories: dict[str, list[str]]) -> MagicMock:
    return MagicMock(
        chat_json=AsyncMock(return_value=SimpleNamespace(categories=categories))
    )


@pytest.mark.asyncio
async def test_classify_families_happy_path_returns_assigned_families() -> None:
    llm = _fake_llm(
        {"mri methods": ["health_wellness"], "microfabrication": ["hardware"]}
    )
    result = await skill_classifier.classify_families(
        llm, ["mri methods", "microfabrication"], settings=get_settings()
    )
    assert result == {
        "mri methods": ["health_wellness"],
        "microfabrication": ["hardware"],
    }


@pytest.mark.asyncio
async def test_classify_families_issues_exactly_one_batched_call_not_one_per_name() -> (
    None
):
    llm = _fake_llm({"a": ["backend"], "b": ["data"], "c": ["ml"]})
    await skill_classifier.classify_families(
        llm, ["a", "b", "c"], settings=get_settings()
    )
    llm.chat_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_families_empty_names_never_calls_the_llm() -> None:
    llm = _fake_llm({})
    result = await skill_classifier.classify_families(llm, [], settings=get_settings())
    assert result == {}
    llm.chat_json.assert_not_awaited()


# ── conservative rules ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_families_drops_a_family_the_model_invents() -> None:
    """A family the model returns that is not in ``known_families()`` is
    dropped -- the valid family alongside it survives."""
    llm = _fake_llm({"skill x": ["not_a_real_family_xyz", "backend"]})
    result = await skill_classifier.classify_families(
        llm, ["skill x"], settings=get_settings()
    )
    assert result == {"skill x": ["backend"]}


@pytest.mark.asyncio
async def test_classify_families_no_confident_answer_omits_the_key_entirely() -> None:
    """An EXPLICIT empty-list answer means 'no confident family' -- the key
    must be ABSENT from the result, not present with an empty list. Absence
    is what makes this identical to today's (pre-feature) behaviour for that
    skill: ``ResumeSkill.categories`` stays at its default (``None``)."""
    llm = _fake_llm({"skill y": []})
    result = await skill_classifier.classify_families(
        llm, ["skill y"], settings=get_settings()
    )
    assert "skill y" not in result
    assert result == {}


@pytest.mark.asyncio
async def test_classify_families_name_the_model_never_addresses_is_also_omitted() -> (
    None
):
    """The model may simply not mention a name at all (not even an empty
    list) -- same conservative outcome: absent, not an empty list."""
    llm = _fake_llm({})  # model addressed nothing
    result = await skill_classifier.classify_families(
        llm, ["skill z"], settings=get_settings()
    )
    assert "skill z" not in result
    assert result == {}


@pytest.mark.asyncio
async def test_classify_families_all_invented_families_leaves_the_name_omitted() -> (
    None
):
    """Every family offered for a name is invented (unknown) -- after
    dropping them all, zero real families remain, which is the same
    "no confident answer" outcome as an explicit empty list: the key must be
    absent, not present with ``[]``."""
    llm = _fake_llm({"skill w": ["not_real_1", "not_real_2"]})
    result = await skill_classifier.classify_families(
        llm, ["skill w"], settings=get_settings()
    )
    assert "skill w" not in result


@pytest.mark.asyncio
async def test_classify_families_caps_at_two_families_per_skill() -> None:
    """Family credit is transitive across the whole résumé (any résumé
    skill in the family credits a matching requirement), so unbounded
    breadth here amplifies false credit -- capped at 2, a named constant."""
    llm = _fake_llm({"skill v": ["backend", "data", "ml"]})  # 3 real families
    result = await skill_classifier.classify_families(
        llm, ["skill v"], settings=get_settings()
    )
    assert len(result["skill v"]) == 2, (
        f"expected the per-skill family cap (<=2) to trim 3 valid families "
        f"down to 2, got {result['skill v']!r}"
    )
    assert set(result["skill v"]) <= {"backend", "data", "ml"}


@pytest.mark.asyncio
async def test_classify_families_two_valid_families_are_not_trimmed() -> None:
    """The cap must not be so aggressive it drops a legitimate SECOND
    family — exactly 2 offered, exactly 2 kept."""
    llm = _fake_llm({"skill u": ["backend", "data"]})
    result = await skill_classifier.classify_families(
        llm, ["skill u"], settings=get_settings()
    )
    assert result == {"skill u": ["backend", "data"]}


# ── failure posture: best-effort, never fails the caller ─────────────────


@pytest.mark.asyncio
async def test_classify_families_llm_raises_returns_empty_dict_without_raising() -> (
    None
):
    llm = MagicMock(chat_json=AsyncMock(side_effect=RuntimeError("ollama down")))
    result = await skill_classifier.classify_families(
        llm, ["mri methods"], settings=get_settings()
    )
    assert result == {}


@pytest.mark.asyncio
async def test_classify_families_llm_output_invalid_error_is_non_fatal() -> None:
    """The REAL ``LLMClient.chat_json`` raises this (not a bare
    ``RuntimeError``) after exhausting its self-correction retries."""
    from src.pipeline.llm import LLMOutputInvalidError

    llm = MagicMock(
        chat_json=AsyncMock(side_effect=LLMOutputInvalidError("categories: list_type"))
    )
    result = await skill_classifier.classify_families(
        llm, ["mri methods"], settings=get_settings()
    )
    assert result == {}


# ── PII: no skill name in any logged or persisted string ─────────────────


class _Probe(BaseModel):
    x: int


def _leaky_validation_error(offending_value: str) -> ValidationError:
    """A REAL ``pydantic.ValidationError`` whose ``str()`` embeds
    ``offending_value`` as ``input_value=...`` -- exactly the pydantic v2
    behaviour ``validation_error_digest`` exists to neutralise. Simulates
    what an UNGUARDED validation failure inside ``classify_families`` (e.g.
    building its own per-name structured object) would leak if the skill
    name itself is embedded in the model's structured response."""
    try:
        _Probe.model_validate({"x": offending_value})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")  # pragma: no cover


@pytest.mark.asyncio
async def test_classify_families_never_logs_the_skill_name_on_validation_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "SENTINEL_SKILL_NAME_9f3a2b"
    llm = MagicMock(chat_json=AsyncMock(side_effect=_leaky_validation_error(sentinel)))

    with caplog.at_level(logging.DEBUG):
        result = await skill_classifier.classify_families(
            llm, [sentinel], settings=get_settings()
        )

    assert result == {}
    assert sentinel not in caplog.text, (
        "a skill name leaked into a log line via str(ValidationError) -- "
        "must use validation_error_digest (or count-only logging) instead"
    )


@pytest.mark.asyncio
async def test_classify_families_never_raises_the_leaky_validation_error_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Belt-and-braces: even if the caller's own exception handler somehow
    logged the RAISED exception's repr (not just a log message this module
    authored), the exception itself must never propagate out of
    ``classify_families`` carrying the raw skill name -- it must be caught
    and converted to the safe empty-result outcome before it ever leaves."""
    sentinel = "SENTINEL_SKILL_NAME_leaks_via_repr"
    llm = MagicMock(chat_json=AsyncMock(side_effect=_leaky_validation_error(sentinel)))

    result = await skill_classifier.classify_families(
        llm, [sentinel], settings=get_settings()
    )
    assert result == {}


# ── settings is accepted (keyword-only, per the spec'd signature) ────────


@pytest.mark.asyncio
async def test_classify_families_accepts_settings_as_keyword_only() -> None:
    import inspect

    sig = inspect.signature(skill_classifier.classify_families)
    params = sig.parameters
    assert "settings" in params
    assert params["settings"].kind == inspect.Parameter.KEYWORD_ONLY


def test_classify_families_is_async() -> None:
    import inspect

    assert inspect.iscoroutinefunction(skill_classifier.classify_families)
