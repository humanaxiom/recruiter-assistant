"""RED -- interview-question generator, review round 1: F6 (scrub) and F7
(requirement scrub), pure layer.

* F6a. A phone stored with a country code (``+1 604 555 1234``) must be
  redacted from a quote written ``604-555-1234`` or ``(604) 555 1234`` -- the
  comparison is on the LAST 10 DIGITS, both directions.
* F6b. ANY email-shaped or phone-shaped string is redacted, not just the
  candidate's own (a second email, a referee's number). The patterns are
  ``src.services.redaction``'s, not new regexes. Short digit runs (years,
  version numbers) are untouched.
* F6c. Names compare after NFKC + accent folding on BOTH sides: stored
  ``José Núñez`` redacts ``Jose Nunez`` and an NFD-composed ``José``.
* ``filter_questions`` applies the same matching rules.
* F7. ``redact_gaps`` also scrubs the REQUIREMENT text, and ``gaps_hash`` over
  the scrubbed gaps is stable.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from src.services.interview_questions import (
    Gap,
    InterviewQuestionsIngest,
    filter_questions,
    gaps_hash,
    redact_gaps,
    scrub_identifiers,
)

_SRC = Path(__file__).resolve().parents[2] / "src"

STORED_PHONE_CC = "+1 604 555 1234"


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _scrub(text: str, **kw: Any) -> str:
    args: dict[str, Any] = {"name": None, "email": None, "phone": None}
    args.update(kw)
    return scrub_identifiers(text, **args)


# -- F6a: phone with a country code ----------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "604-555-1234",
        "(604) 555 1234",
        "604.555.1234",
        "6045551234",
        "+1 604 555 1234",
        "1-604-555-1234",
    ],
)
def test_a_phone_stored_with_a_country_code_is_redacted_in_every_local_format(
    written: str,
) -> None:
    out = _scrub(f"Reach me on {written} any time", phone=STORED_PHONE_CC)
    assert "555" not in out
    assert "1234" not in out
    assert out.startswith("Reach me on ")
    assert out.endswith(" any time")


@pytest.mark.parametrize("stored", ["604-555-1234", "(604) 555-1234", "6045551234"])
def test_a_local_stored_phone_is_redacted_from_a_country_code_quote(
    stored: str,
) -> None:
    out = _scrub("call +1 604 555 1234 now", phone=stored)
    assert "555" not in out
    assert "1234" not in out


def test_a_short_stored_phone_still_matches_by_its_digits() -> None:
    out = _scrub("call 555-0192 today", phone="555-0192")
    assert "0192" not in out
    assert out.startswith("call ")


# -- F6b: any email / phone shaped string -----------------------------------


def test_another_persons_email_is_redacted_even_with_a_candidate_email() -> None:
    out = _scrub(
        "Referee bob.jones@corp.example, me at jane@example.test",
        email="jane@example.test",
    )
    assert "bob.jones" not in out
    assert "corp.example" not in out
    assert "jane@example.test" not in out


def test_any_email_is_redacted_when_the_candidate_has_none_on_file() -> None:
    out = _scrub("write to bob.jones@corp.example please")
    assert "bob.jones" not in out
    assert "corp.example" not in out
    assert out.startswith("write to ")


def test_another_persons_phone_is_redacted_when_the_candidate_has_none() -> None:
    out = _scrub("referee 778-555-0100 knows my work")
    assert "0100" not in out
    assert "555" not in out
    assert out.endswith(" knows my work")


def test_another_persons_phone_is_redacted_beside_the_candidates() -> None:
    out = _scrub("mine 604-555-1234, referee (778) 555 0100", phone=STORED_PHONE_CC)
    assert "1234" not in out
    assert "0100" not in out


@pytest.mark.parametrize(
    "benign",
    [
        "Ran 40 servers from 2016-2019 on Python 3.11.4",
        "Cut deploy time by 35% across 12 teams in 2020",
    ],
)
def test_short_digit_runs_are_not_mistaken_for_phones(benign: str) -> None:
    assert _scrub(benign, phone=STORED_PHONE_CC) == benign


def test_redaction_reuses_the_blind_redactors_patterns() -> None:
    source = (_SRC / "services" / "interview_questions.py").read_text(encoding="utf-8")
    assert re.search(
        r"^(from|import) src[.]services[.]redaction",
        source,
        re.M,
    )


# -- F6c: names fold accents and normalise ----------------------------------

_NAMES = ["José Núñez", "Jose Nunez", "José Nũez"]
_QUOTES = [
    "Jose Nunez led the platform team",
    "JOSÉ NÚÑEZ led the platform team",
    "José Nũez led the platform team",
    "José led the platform team",
    "led by Núñez on the platform team",
]


@pytest.mark.parametrize("stored", _NAMES)
@pytest.mark.parametrize("quote", _QUOTES)
def test_names_match_across_accents_case_and_unicode_form(
    stored: str, quote: str
) -> None:
    out = _scrub(quote, name=stored)
    folded = _fold(out)
    assert "jose" not in folded
    assert "nunez" not in folded
    assert "platform team" in folded


def test_a_name_token_inside_a_longer_word_is_left_alone() -> None:
    out = _scrub("Josephine mentored three juniors", name="José Núñez")
    assert "Josephine" in out


# -- the output filter uses the same rules ----------------------------------


def _gap(quote: str | None = None) -> Gap:
    return Gap(index=2, requirement="Budget ownership", status="missing", quote=quote)


def _ingest(*questions: str) -> InterviewQuestionsIngest:
    return InterviewQuestionsIngest.model_validate(
        {"questions": [{"requirement_index": 2, "question": q} for q in questions]}
    )


@pytest.mark.parametrize(
    ("question", "phone"),
    [
        ("Can we reach you on 6045551234 about budgets?", "+16045551234"),
        ("Can we reach you on (604) 555-1234 about budgets?", "+1 604 555 1234"),
        ("Can we reach you on 604.555.1234 about budgets?", "+1 (604) 555-1234"),
        ("Can we reach you on +1 604 555 1234 about budgets?", "604-555-1234"),
    ],
)
def test_filter_drops_a_question_echoing_the_phone_by_its_last_ten_digits(
    question: str, phone: str
) -> None:
    kept, dropped = filter_questions(
        _ingest(question, "Describe a budget you owned."),
        [_gap()],
        name=None,
        email=None,
        phone=phone,
    )
    assert [k.question for k in kept] == ["Describe a budget you owned."]
    assert dropped == 1


@pytest.mark.parametrize(
    "question",
    [
        "Jose, how did you own a budget?",
        "José, how did you own a budget?",
        "José, how did you own a budget?",
        "How did NUNEZ own a budget?",
    ],
)
def test_filter_drops_a_question_echoing_the_name_across_accents(
    question: str,
) -> None:
    kept, dropped = filter_questions(
        _ingest(question), [_gap()], name="José Núñez", email=None, phone=None
    )
    assert kept == []
    assert dropped == 1


@pytest.mark.parametrize(
    "question",
    [
        "Should we write to bob.jones@corp.example about budgets?",
        "Should we ring 778-555-0100 about budgets?",
    ],
)
def test_filter_drops_any_email_or_phone_shaped_string(question: str) -> None:
    kept, dropped = filter_questions(
        _ingest(question), [_gap()], name=None, email=None, phone=None
    )
    assert kept == []
    assert dropped == 1


def test_filter_keeps_a_clean_question_with_years_in_it() -> None:
    kept, dropped = filter_questions(
        _ingest("Between 2016-2019 how did you own a budget?"),
        [_gap()],
        name="José Núñez",
        email="a@example.test",
        phone=STORED_PHONE_CC,
    )
    assert len(kept) == 1
    assert dropped == 0


# -- F7: the requirement text is scrubbed too -------------------------------


def _req_gap(requirement: str) -> Gap:
    return Gap(index=3, requirement=requirement, status="missing")


@pytest.mark.parametrize(
    ("requirement", "forbidden"),
    [
        ("Budget ownership for Jane Smith's team", ("jane", "smith")),
        ("Report to jane.smith@example.test", ("jane.smith", "example.test")),
        ("Call 604-555-0192 for the on-call rota", ("0192",)),
        ("Mentoring like José Núñez does", ("jose", "nunez")),
    ],
)
def test_redact_gaps_scrubs_the_requirement_text(
    requirement: str, forbidden: tuple[str, ...]
) -> None:
    [out] = redact_gaps(
        [_req_gap(requirement)],
        name="Jane Smith" if "Jane" in requirement else "José Núñez",
        email="jane.smith@example.test",
        phone="604-555-0192",
    )
    folded = _fold(out.requirement)
    for token in forbidden:
        assert token not in folded, token
    assert (out.index, out.status) == (3, "missing")


def test_redact_gaps_leaves_a_clean_requirement_byte_identical() -> None:
    [out] = redact_gaps(
        [_req_gap("5 years of Kubernetes operations")],
        name="Jane Smith",
        email="jane@example.test",
        phone="604-555-0192",
    )
    assert out.requirement == "5 years of Kubernetes operations"


def test_gaps_hash_over_scrubbed_gaps_is_stable_and_identifier_free() -> None:
    gaps = [_req_gap("Budget ownership for Jane Smith's team")]
    kw: dict[str, Any] = {"name": "Jane Smith", "email": None, "phone": None}
    first = gaps_hash(redact_gaps(gaps, **kw))
    assert first == gaps_hash(redact_gaps(gaps, **kw))
    assert first != gaps_hash(gaps)
