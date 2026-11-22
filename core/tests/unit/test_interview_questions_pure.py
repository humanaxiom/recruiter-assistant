"""RED pin -- interview-question generator, slice S1 (the pure layer).

The generator turns the requirements a candidate's evidence does NOT cover into
interview questions. Every rule here exists because the obvious implementation
is a harm on a hiring screen:

* **The evidence cliff (ADR-040 / ROADMAP A4).** A candidate ranked past
  ``evidence_k`` was never examined, so ``requirements`` is empty or all
  ``missing`` by compute placement. Generating "gap" questions from that would
  present "not examined" as "missing". ``evidence_evaluated is not True`` --
  False *and* None -- is a typed refusal, never an empty or a fabricated list.
* **Cite a gap or be dropped.** A question must point at a supplied requirement
  index, and any quote it claims to probe must be a substring of the quote we
  supplied. Anything else is the model inventing a gap.
* **No candidate identifiers in, none out.** The prompt never receives name,
  email or phone, and a post-generation scrub drops any question that echoes
  one anyway.
* **Protected characteristics** are prohibited in the system prompt.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import jinja2
import pytest
from pydantic import ValidationError

from src.prompts import load_prompt
from src.schemas.matching import (
    EvidenceObject,
    EvidenceStatus,
    RequirementEvidence,
    ScoreBreakdown,
    ShortlistEntry,
)
from src.services.interview_questions import (
    MAX_QUESTION_CHARS,
    MAX_QUESTIONS_PER_GAP,
    MAX_QUESTIONS_TOTAL,
    PROMPT_VERSION,
    Gap,
    InterviewQuestionItem,
    InterviewQuestionsIngest,
    InterviewRefusal,
    build_messages,
    filter_questions,
    gaps_hash,
    select_gaps,
)

_SRC = Path(__file__).resolve().parents[2] / "src"

NAME = "Priyanka Venkataraman"
EMAIL = "priyanka.v@example.org"
PHONE = "604-555-0199"


def _req(text: str, status: EvidenceStatus, quote: str = "") -> RequirementEvidence:
    return RequirementEvidence.model_validate(
        {"requirement": text, "status": status, "evidence": quote, "confidence": 0.6}
    )


def _entry(
    requirements: list[RequirementEvidence],
    *,
    evaluated: bool | None = True,
    label: str | None = None,
) -> ShortlistEntry:
    return ShortlistEntry.model_validate(
        {
            "id": uuid4(),
            "job_id": uuid4(),
            "resume_id": uuid4(),
            "rank": 2,
            "score_final": 0.7,
            "score_breakdown": ScoreBreakdown(
                skill=0.8,
                experience=0.7,
                education=0.6,
                seniority=0.5,
                vector=0.4,
                structured=0.65,
            ),
            "evidence": EvidenceObject(requirements=requirements),
            "pipeline_meta": None,
            "generated_at": dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
            "evidence_evaluated": evaluated,
            "display_label": label,
        }
    )


def _mixed_entry() -> ShortlistEntry:
    return _entry(
        [
            _req("5 years of Python", "met", "Built Python services since 2016"),
            _req("Kubernetes operations", "partial", "Deployed a pod once in a lab"),
            _req("Budget ownership", "missing"),
            _req("Mentoring juniors", "met", "Mentored four interns"),
            _req("Terraform", "missing"),
        ]
    )


def _reason(exc: InterviewRefusal) -> str:
    reason: Any = exc.reason
    return str(getattr(reason, "value", reason))


def _g(index: int, req: str, status: EvidenceStatus, quote: str | None = None) -> Gap:
    return Gap(index=index, requirement=req, status=status, quote=quote)


# -- select_gaps ----------------------------------------------------------


def test_select_gaps_returns_only_missing_and_partial_in_order() -> None:
    gaps = select_gaps(_mixed_entry())
    assert [g.requirement for g in gaps] == [
        "Kubernetes operations",
        "Budget ownership",
        "Terraform",
    ]
    assert [g.status for g in gaps] == ["partial", "missing", "missing"]


def test_gap_index_is_position_in_evidence_requirements() -> None:
    gaps = select_gaps(_mixed_entry())
    assert [g.index for g in gaps] == [1, 2, 4]


def test_partial_gap_carries_quote_and_missing_gap_carries_none() -> None:
    gaps = select_gaps(_mixed_entry())
    assert gaps[0].quote == "Deployed a pod once in a lab"
    assert gaps[1].quote is None
    assert gaps[2].quote is None


def test_missing_row_quote_is_none_even_if_row_has_stray_evidence_text() -> None:
    entry = _entry([_req("Terraform", "missing", "stray text on a missing row")])
    assert select_gaps(entry)[0].quote is None


def test_met_row_evidence_never_surfaces_in_any_gap() -> None:
    blob = repr(select_gaps(_mixed_entry()))
    assert "Built Python services" not in blob
    assert "Mentored four interns" not in blob


@pytest.mark.parametrize("evaluated", [False, None])
def test_cliff_refuses_when_evidence_not_evaluated(evaluated: bool | None) -> None:
    # Even with rows that look like gaps: "not examined" is not "missing".
    entry = _entry([_req("Terraform", "missing")], evaluated=evaluated)
    with pytest.raises(InterviewRefusal) as info:
        select_gaps(entry)
    assert _reason(info.value) == "not_evaluated"


def test_cliff_refusal_takes_precedence_over_other_refusals() -> None:
    with pytest.raises(InterviewRefusal) as info:
        select_gaps(_entry([], evaluated=False))
    assert _reason(info.value) == "not_evaluated"


def test_refuses_when_there_are_no_requirements() -> None:
    with pytest.raises(InterviewRefusal) as info:
        select_gaps(_entry([]))
    assert _reason(info.value) == "no_requirements"


def test_refuses_when_everything_is_met() -> None:
    entry = _entry([_req("A", "met", "qa"), _req("B", "met", "qb")])
    with pytest.raises(InterviewRefusal) as info:
        select_gaps(entry)
    assert _reason(info.value) == "no_gaps"


def test_refusal_reasons_are_distinct_and_a_fixed_set() -> None:
    reasons: set[str] = set()
    for entry in (
        _entry([_req("T", "missing")], evaluated=False),
        _entry([]),
        _entry([_req("A", "met", "q")]),
    ):
        with pytest.raises(InterviewRefusal) as info:
            select_gaps(entry)
        reasons.add(_reason(info.value))
    assert reasons == {"not_evaluated", "no_requirements", "no_gaps"}


def test_refusal_is_an_exception_with_a_pii_free_message() -> None:
    with pytest.raises(InterviewRefusal) as info:
        select_gaps(_entry([], label=NAME))
    assert isinstance(info.value, Exception)
    assert NAME not in str(info.value)


# -- gaps_hash ------------------------------------------------------------


def test_gaps_hash_is_a_sha256_hexdigest() -> None:
    assert re.fullmatch(r"[0-9a-f]{64}", gaps_hash([_g(0, "A", "missing")]))


def test_gaps_hash_is_stable_across_calls() -> None:
    assert gaps_hash(select_gaps(_mixed_entry())) == gaps_hash(
        select_gaps(_mixed_entry())
    )


def test_gaps_hash_changes_when_a_status_changes() -> None:
    assert gaps_hash([_g(0, "A", "missing")]) != gaps_hash([_g(0, "A", "partial", "q")])


def test_gaps_hash_ignores_quote_text() -> None:
    a = gaps_hash([_g(0, "A", "partial", "first quote")])
    b = gaps_hash([_g(0, "A", "partial", "a totally different quote")])
    assert a == b


def test_gaps_hash_depends_on_order_and_requirement_text() -> None:
    x, y = _g(0, "A", "missing"), _g(1, "B", "missing")
    assert gaps_hash([x, y]) != gaps_hash([y, x])
    assert gaps_hash([x]) != gaps_hash([_g(0, "A2", "missing")])


def test_gaps_hash_is_not_trivially_the_hash_of_nothing() -> None:
    assert gaps_hash([_g(0, "A", "missing")]) != hashlib.sha256(b"").hexdigest()


def test_gaps_hash_distinguishes_boundary_shifts() -> None:
    # ("ab","c") vs ("a","bc") must not collide through naive concatenation.
    assert gaps_hash([_g(0, "ab", "missing"), _g(1, "c", "missing")]) != gaps_hash(
        [_g(0, "a", "missing"), _g(1, "bc", "missing")]
    )


# -- InterviewQuestionsIngest --------------------------------------------


def _item(
    idx: int = 1, q: str = "Walk me through a cluster you ran?"
) -> dict[str, Any]:
    return {"requirement_index": idx, "question": q}


def test_ingest_accepts_a_minimal_valid_payload() -> None:
    ing = InterviewQuestionsIngest.model_validate({"questions": [_item()]})
    assert len(ing.questions) == 1
    assert ing.questions[0].probes_quote is None


def test_ingest_probes_quote_is_optional_and_kept() -> None:
    item = {**_item(), "probes_quote": "Deployed a pod once"}
    ing = InterviewQuestionsIngest.model_validate({"questions": [item]})
    assert ing.questions[0].probes_quote == "Deployed a pod once"


def test_ingest_caps_total_questions_by_scrubbing_not_raising() -> None:
    # Precedent: the evidence ingest caps scrub so one excess item cannot cost
    # the whole payload. Spread across gaps so the per-gap cap is not the limit.
    items = [_item(i // 2, f"Question {i}?") for i in range(MAX_QUESTIONS_TOTAL + 5)]
    ing = InterviewQuestionsIngest.model_validate({"questions": items})
    assert len(ing.questions) == MAX_QUESTIONS_TOTAL


def test_ingest_caps_questions_per_gap() -> None:
    items = [
        _item(1, f"Question number {n}?") for n in range(MAX_QUESTIONS_PER_GAP + 2)
    ]
    ing = InterviewQuestionsIngest.model_validate({"questions": items})
    assert [q.question for q in ing.questions] == [
        f"Question number {n}?" for n in range(MAX_QUESTIONS_PER_GAP)
    ]


def test_ingest_drops_an_overlong_question_rather_than_truncating_it() -> None:
    long_q = "x" * (MAX_QUESTION_CHARS + 1)
    ing = InterviewQuestionsIngest.model_validate(
        {"questions": [_item(1, long_q), _item(2, "Short one?")]}
    )
    assert [q.question for q in ing.questions] == ["Short one?"]


def test_ingest_drops_a_blank_question() -> None:
    ing = InterviewQuestionsIngest.model_validate(
        {"questions": [_item(1, "   "), _item(2, "Real one?")]}
    )
    assert [q.question for q in ing.questions] == ["Real one?"]


def test_ingest_strips_control_characters_from_question_text() -> None:
    nul = chr(0)
    bidi = chr(0x202E)
    ing = InterviewQuestionsIngest.model_validate(
        {"questions": [_item(1, f"Tell me{nul} about{bidi} it?")]}
    )
    q = ing.questions[0].question
    assert nul not in q and bidi not in q


def test_ingest_rejects_a_non_integer_requirement_index() -> None:
    with pytest.raises(ValidationError):
        InterviewQuestionsIngest.model_validate(
            {"questions": [{"requirement_index": "one", "question": "Q?"}]}
        )


def test_ingest_rejects_questions_that_is_not_a_list() -> None:
    with pytest.raises(ValidationError):
        InterviewQuestionsIngest.model_validate({"questions": "not a list"})


def test_ingest_item_type_is_exported() -> None:
    ing = InterviewQuestionsIngest.model_validate({"questions": [_item()]})
    assert isinstance(ing.questions[0], InterviewQuestionItem)


# -- filter_questions -----------------------------------------------------


def _ing(*items: dict[str, Any]) -> InterviewQuestionsIngest:
    return InterviewQuestionsIngest.model_validate({"questions": list(items)})


def _gaps() -> list[Gap]:
    return [
        _g(1, "Kubernetes operations", "partial", "Deployed a pod once in a lab"),
        _g(2, "Budget ownership", "missing"),
    ]


def _filter(
    ing: InterviewQuestionsIngest,
    gaps: list[Gap] | None = None,
    *,
    name: str | None = NAME,
    email: str | None = EMAIL,
    phone: str | None = PHONE,
) -> tuple[list[InterviewQuestionItem], int]:
    return filter_questions(ing, gaps or _gaps(), name=name, email=email, phone=phone)


def test_filter_keeps_valid_questions_in_order_and_counts_zero_dropped() -> None:
    kept, dropped = _filter(
        _ing(
            _item(2, "How have you managed a budget?"), _item(1, "Describe a rollout.")
        )
    )
    assert [q.requirement_index for q in kept] == [2, 1]
    assert dropped == 0


def test_filter_drops_an_unknown_requirement_index() -> None:
    kept, dropped = _filter(_ing(_item(99, "Invented gap?"), _item(1, "Real gap?")))
    assert [q.requirement_index for q in kept] == [1]
    assert dropped == 1


def test_filter_drops_a_met_row_index_not_in_the_gap_list() -> None:
    # index 0 is a met row in the entry; it is not among the supplied gaps.
    kept, dropped = _filter(_ing(_item(0, "Probe a met row?")))
    assert kept == [] and dropped == 1


def test_filter_accepts_probes_quote_that_is_a_substring() -> None:
    kept, dropped = _filter(_ing({**_item(1), "probes_quote": "deployed a pod"}))
    assert len(kept) == 1 and dropped == 0


def test_filter_quote_check_normalizes_whitespace_and_case() -> None:
    kept, dropped = _filter(
        _ing({**_item(1), "probes_quote": "  DEPLOYED   a\n pod  ONCE "})
    )
    assert len(kept) == 1 and dropped == 0


def test_filter_drops_a_probes_quote_not_in_the_supplied_quote() -> None:
    kept, dropped = _filter(
        _ing({**_item(1), "probes_quote": "Led a team of twelve SREs"})
    )
    assert kept == [] and dropped == 1


def test_filter_drops_a_probes_quote_on_a_missing_gap_with_no_quote() -> None:
    kept, dropped = _filter(_ing({**_item(2), "probes_quote": "anything at all"}))
    assert kept == [] and dropped == 1


def test_filter_keeps_a_missing_gap_question_with_no_probes_quote() -> None:
    kept, dropped = _filter(_ing(_item(2, "Tell me about owning a budget?")))
    assert len(kept) == 1 and dropped == 0


def test_filter_probes_quote_is_checked_against_its_own_gap_not_another() -> None:
    gaps = [
        _g(1, "K8s", "partial", "alpha quote"),
        _g(3, "Go", "partial", "beta quote"),
    ]
    kept, dropped = _filter(_ing({**_item(1), "probes_quote": "beta quote"}), gaps)
    assert kept == [] and dropped == 1


@pytest.mark.parametrize(
    "question",
    [
        "Why did Priyanka leave her last role?",
        "Why did PRIYANKA leave?",
        "Tell me, Venkataraman, about Kubernetes?",
        "Email priyanka.v@example.org for the details?",
        "Email PRIYANKA.V@EXAMPLE.ORG for the details?",
        "Call 604-555-0199 and describe it?",
        "Call 6045550199 and describe it?",
        "Call (604) 555 0199 and describe it?",
    ],
)
def test_filter_drops_questions_containing_candidate_identifiers(question: str) -> None:
    kept, dropped = _filter(_ing(_item(1, question)))
    assert kept == [] and dropped == 1


def test_filter_name_match_is_on_word_boundaries() -> None:
    kept, dropped = _filter(
        _ing(_item(1, "Describe a priyankana-free rollout of Kubernetes?")),
        name="Priya",
    )
    assert len(kept) == 1 and dropped == 0


def test_filter_name_tokens_shorter_than_two_letters_are_ignored() -> None:
    # The initial "Q" must not drop every question containing the letter q.
    kept, dropped = _filter(
        _ing(_item(1, "What quality gates did you run for Kubernetes?")),
        name="Anna Q Smithson",
        email=None,
        phone=None,
    )
    assert len(kept) == 1 and dropped == 0


def test_filter_matches_any_name_token_not_only_the_full_name() -> None:
    kept, dropped = _filter(
        _ing(_item(1, "Has Smithson worked on clusters?")),
        name="Anna Q Smithson",
    )
    assert kept == [] and dropped == 1


def test_filter_with_no_identifiers_supplied_drops_nothing() -> None:
    kept, dropped = _filter(
        _ing(_item(1, "Anything goes here?")), name=None, email=None, phone=None
    )
    assert len(kept) == 1 and dropped == 0


def test_filter_dropped_count_sums_every_reason() -> None:
    kept, dropped = _filter(
        _ing(
            _item(1, "Fine question?"),
            _item(77, "Bad index?"),
            {**_item(1, "Bad quote?"), "probes_quote": "never said"},
            _item(2, "Why did Priyanka leave?"),
        )
    )
    assert len(kept) == 1 and dropped == 3


def test_filter_empty_ingest_returns_empty_and_zero() -> None:
    kept, dropped = _filter(_ing())
    assert kept == [] and dropped == 0


# -- prompt templates -----------------------------------------------------


def _render(gaps: list[Gap] | None = None) -> Any:
    return load_prompt(PROMPT_VERSION, gaps=gaps or select_gaps(_mixed_entry()))


def test_prompt_version_constant_names_the_template_pair() -> None:
    assert PROMPT_VERSION == "interview_questions_v1"


def test_prompt_renders_requirements_and_partial_quotes() -> None:
    p = _render()
    assert p.version == "interview_questions_v1"
    for text in ("Kubernetes operations", "Budget ownership", "Terraform"):
        assert text in p.user
    assert "Deployed a pod once in a lab" in p.user


def test_prompt_never_contains_a_met_rows_requirement_or_evidence() -> None:
    p = _render()
    blob = p.system + p.user
    assert "5 years of Python" not in blob
    assert "Built Python services" not in blob
    assert "Mentored four interns" not in blob


def test_prompt_exposes_each_gap_index_so_the_model_can_cite_it() -> None:
    p = _render()
    assert "requirement_index" in p.system + p.user
    for idx in (1, 2, 4):
        assert re.search(rf"\b{idx}\b", p.user)


def test_system_prompt_prohibits_protected_characteristics() -> None:
    system = _render().system.lower()
    assert re.search(r"\b(never|must not|do not|prohibit|forbidden|not ask)", system)
    for word in (
        "age",
        "marital",
        "family",
        "national",
        "religio",
        "health",
        "disabilit",
        "gender",
    ):
        assert word in system, word


def test_system_prompt_restricts_to_supplied_requirements_and_quotes() -> None:
    system = _render().system.lower()
    assert "only" in system
    assert "requirement" in system and "quote" in system


def test_system_prompt_frames_gaps_as_not_evidenced_never_as_lacking() -> None:
    system = _render().system
    assert "not evidenced in the résumé" in system
    # The word "lack" may appear only inside a prohibition.
    for m in re.finditer(r"lack", system, flags=re.IGNORECASE):
        window = system[max(0, m.start() - 80) : m.end() + 40].lower()
        assert re.search(r"never|not|avoid", window)


def test_system_prompt_demands_json_output() -> None:
    assert "json" in _render().system.lower()


def test_prompt_uses_strict_undefined() -> None:
    with pytest.raises(jinja2.UndefinedError):
        load_prompt(PROMPT_VERSION)


# -- build_messages -------------------------------------------------------


def test_build_messages_returns_system_then_user() -> None:
    msgs = build_messages(select_gaps(_mixed_entry()))
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert all(m["content"] for m in msgs)


def test_build_messages_matches_the_loaded_prompt() -> None:
    gaps = select_gaps(_mixed_entry())
    assert build_messages(gaps) == load_prompt(PROMPT_VERSION, gaps=gaps).messages


def test_build_messages_contains_no_candidate_identifiers() -> None:
    entry = _entry(
        [_req("Kubernetes operations", "partial", "Deployed a pod once")],
        label=NAME,
    )
    blob = " ".join(m["content"] for m in build_messages(select_gaps(entry)))
    for token in ("Priyanka", "Venkataraman", EMAIL, PHONE):
        assert token not in blob


def test_build_messages_only_takes_gaps_so_no_entry_can_leak_through() -> None:
    assert list(inspect.signature(build_messages).parameters) == ["gaps"]


def test_build_messages_includes_partial_quotes_but_not_met_evidence() -> None:
    blob = " ".join(m["content"] for m in build_messages(select_gaps(_mixed_entry())))
    assert "Deployed a pod once in a lab" in blob
    assert "Built Python services since 2016" not in blob
    assert "Mentored four interns" not in blob


# -- token floor ----------------------------------------------------------


def test_generate_call_site_uses_the_reasoning_floor_constant() -> None:
    src = (_SRC / "services" / "interview_questions.py").read_text(encoding="utf-8")
    assert re.search(r"max_tokens\s*=\s*REASONING_JSON_MIN_TOKENS\b", src)
    assert not re.search(r"max_tokens\s*=\s*\d", src)
    assert re.search(r"^from .* import .*REASONING_JSON_MIN_TOKENS", src, re.M) or (
        re.search(r"^\s+REASONING_JSON_MIN_TOKENS,", src, re.M)
    )
