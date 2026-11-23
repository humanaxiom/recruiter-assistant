"""RED -- interview-question generator, review round 2, G3 and G4.

G3 -- a candidate name token that is shorter than 3 characters, or that is in
a small fixed list of common English words, is matched ONLY as part of the
full-name sequence ("An Tran"), never alone. Otherwise a candidate called
"An Tran" turns "Tell me about an occasion" into "Tell me about [redacted]
occasion" or drops the question. Uncommon tokens ("Tran") still match alone.
The scrub and the filter must agree (the filter drops what the scrub would
change).

The common-word list the coder must implement contains AT LEAST: an, can, do,
will, may, mark, grant, max, rose, june, guy, bill, art, ray, page, hope, joy,
sky -- and every token shorter than 3 characters.

G4 -- a phone number written with an en dash, em dash, slash or middle dot as
its separator is still a phone number. Short runs ("2016<en dash>2019") must
be left alone. These tests call the public functions only, so they hold
whether the coder widens ``redaction._PHONE_CAND_RE`` or implements the
separators inside ``interview_questions.py``.

The three separators below are literal UTF-8 (en dash E2 80 93, em dash E2 80 94,
middle dot C2 B7), verified by bytes; nothing else in this file is non-ASCII.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.services.interview_questions import (
    Gap,
    InterviewQuestionsIngest,
    filter_questions,
    redact_gaps,
    scrub_identifiers,
)

EN = "–"
EM = "—"
DOT = "·"


def _scrub(text: str, **kw: Any) -> str:
    args: dict[str, Any] = {"name": None, "email": None, "phone": None}
    args.update(kw)
    return scrub_identifiers(text, **args)


def _gap() -> Gap:
    return Gap(index=2, requirement="Budget ownership", status="missing")


def _filter(question: str, **kw: Any) -> tuple[list[str], int]:
    args: dict[str, Any] = {"name": None, "email": None, "phone": None}
    args.update(kw)
    ingest = InterviewQuestionsIngest.model_validate(
        {"questions": [{"requirement_index": 2, "question": question}]}
    )
    kept, dropped = filter_questions(ingest, [_gap()], **args)
    return [k.question for k in kept], dropped


# -- G3: common-word name tokens --------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Tell me about an occasion when a deadline slipped.",
        "Describe an engineering team you led.",
        "How would you do that again?",
    ],
)
def test_a_short_name_token_alone_is_not_redacted(text: str) -> None:
    assert _scrub(text, name="An Tran") == text


def test_the_full_name_sequence_is_still_scrubbed_for_an_tran() -> None:
    out = _scrub("An Tran led the migration.", name="An Tran")
    assert "Tran" not in out
    assert "An Tran" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize("text", ["Tran led the migration.", "Ask Tran about it."])
def test_an_uncommon_token_is_still_scrubbed_alone(text: str) -> None:
    out = _scrub(text, name="An Tran")
    assert "Tran" not in out
    assert "[redacted]" in out


def test_the_full_name_match_is_case_and_whitespace_tolerant() -> None:
    out = _scrub("then an  TRAN signed off", name="An Tran")
    assert "tran" not in out.lower()


@pytest.mark.parametrize(
    "text",
    [
        "Will you walk me through grants you wrote?",
        "Describe a grant you were awarded.",
        "What will you do about it?",
    ],
)
def test_will_grant_common_words_alone_are_kept(text: str) -> None:
    assert _scrub(text, name="Will Grant") == text


def test_will_grant_full_name_is_scrubbed() -> None:
    out = _scrub("Will Grant owned the budget.", name="Will Grant")
    assert "Grant" not in out and "Will" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Grant Okafor", "Tell me how you secured a grant."),
        ("June Rose", "What happened in June when the rose bush died?"),
        ("Mark Page", "Please mark the page you disagree with."),
        ("Max Ray", "What is the max you would spend on one ray scan?"),
        ("Hope Joy", "I hope the joy of the sky stays."),
    ],
)
def test_each_common_word_name_token_is_kept_alone(name: str, text: str) -> None:
    assert _scrub(text, name=name) == text


@pytest.mark.parametrize(
    ("name", "text", "forbidden"),
    [
        ("Grant Okafor", "Okafor ran the audit.", "Okafor"),
        ("Grant Okafor", "Grant Okafor ran the audit.", "Okafor"),
        ("June Rose", "June Rose ran the audit.", "Rose"),
        ("Mark Page", "Mark Page ran the audit.", "Page"),
    ],
)
def test_the_uncommon_token_and_the_full_sequence_are_scrubbed(
    name: str, text: str, forbidden: str
) -> None:
    out = _scrub(text, name=name)
    assert forbidden not in out
    assert "[redacted]" in out


def test_the_requirement_heading_is_unchanged_for_an_tran() -> None:
    heading = "Experience managing an engineering team"
    [out] = redact_gaps(
        [Gap(index=1, requirement=heading, status="missing")],
        name="An Tran",
        email=None,
        phone=None,
    )
    assert out.requirement == heading


def test_a_quote_with_the_full_name_is_scrubbed_but_not_its_common_word() -> None:
    [out] = redact_gaps(
        [
            Gap(
                index=1,
                requirement="Leadership",
                status="partial",
                quote="An Tran led it, and an intern helped",
            )
        ],
        name="An Tran",
        email=None,
        phone=None,
    )
    assert out.quote is not None
    assert "Tran" not in out.quote
    assert "an intern helped" in out.quote


def test_filter_keeps_a_question_using_the_common_word_alone() -> None:
    kept, dropped = _filter("Tell me about an occasion you led.", name="An Tran")
    assert kept == ["Tell me about an occasion you led."]
    assert dropped == 0


def test_filter_drops_a_question_naming_the_full_name() -> None:
    kept, dropped = _filter("An Tran led it; how?", name="An Tran")
    assert kept == []
    assert dropped == 1


def test_filter_drops_a_question_naming_the_uncommon_token_alone() -> None:
    kept, dropped = _filter("How did Tran lead it?", name="An Tran")
    assert kept == []
    assert dropped == 1


def test_filter_keeps_will_you_walk_me_through_grants_for_will_grant() -> None:
    q = "Will you walk me through grants you wrote?"
    kept, dropped = _filter(q, name="Will Grant")
    assert kept == [q]
    assert dropped == 0


def test_filter_drops_will_grant_by_full_name() -> None:
    kept, dropped = _filter("Will Grant owned it; how?", name="Will Grant")
    assert kept == []
    assert dropped == 1


# -- G4: phone separators ---------------------------------------------------


_SEPARATED = [
    f"604{EN}555{EN}1234",
    f"604{EM}555{EM}1234",
    "604/555/1234",
    f"604{DOT}555{DOT}1234",
]


@pytest.mark.parametrize("written", _SEPARATED)
def test_the_candidates_phone_is_redacted_with_any_separator(written: str) -> None:
    out = _scrub(f"You can call {written} any time.", phone="604-555-1234")
    assert "1234" not in out
    assert "555" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize("written", _SEPARATED)
def test_another_persons_phone_is_redacted_with_any_separator(written: str) -> None:
    out = _scrub(f"Her referee is on {written}.", phone=None)
    assert "1234" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize("written", _SEPARATED)
def test_another_persons_phone_is_redacted_beside_a_different_candidate_phone(
    written: str,
) -> None:
    out = _scrub(f"Her referee is on {written}.", phone="778-555-0100")
    assert "1234" not in out


@pytest.mark.parametrize("written", _SEPARATED)
def test_filter_drops_a_question_echoing_a_phone_with_any_separator(
    written: str,
) -> None:
    kept, dropped = _filter(
        f"Can we ring {written} about budgets?", phone="604-555-1234"
    )
    assert kept == []
    assert dropped == 1


@pytest.mark.parametrize("written", _SEPARATED)
def test_filter_drops_any_ten_digit_phone_with_any_separator_when_none_on_file(
    written: str,
) -> None:
    kept, dropped = _filter(f"Can we ring {written} about budgets?")
    assert kept == []
    assert dropped == 1


@pytest.mark.parametrize(
    "benign",
    [
        f"Ran 40 servers from 2016{EN}2019 on Python 3.11.4",
        f"Led the team from 2016{EM}2019 and 2020{EN}2022",
        "Revenue went 2016/2019 in the draft",
        f"Cut deploy time by 35{DOT}12 percent in 2020",
    ],
)
def test_short_digit_runs_with_the_new_separators_are_untouched(benign: str) -> None:
    assert _scrub(benign, phone="604-555-1234") == benign
    assert _scrub(benign) == benign
    kept, dropped = _filter(benign, phone="604-555-1234")
    assert kept == [benign]
    assert dropped == 0
