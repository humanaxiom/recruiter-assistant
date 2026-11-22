"""RED -- round 9.

Round 8 made ``_phones`` strip the "(" ")" around a match before counting digits.
That stripped the separator the digit floor relied on, so two shapes regressed or
were never covered:

1. A bracketed, unseparated number of 7-9 digits ("Mobile: (612345678)",
   "Tel (91234567)", "(5550199)") is below the bare floor once the brackets are
   stripped, and ships.
2. A LABELLED number with no separators ("Mobile: 612345678") is a phone
   "whatever its shape" (the comment on ``_PHONE_LABELS``) but the floor is applied
   to it as if it were unlabelled.

The floor for a labelled number is 7 digits (the same as a separated local
number). "Tel: 123456" has 6 digits and is deliberately NOT pinned as redacted:
six bare digits after a label are as likely a postcode or extension as a phone,
and 7 is the repo's existing local-number floor, so the guard pins that it
survives.
"""

from __future__ import annotations

import re

import pytest

BODY = "Analyst with SQL skills"


def _terms(name: str) -> list[str]:
    from src.services.resume_export_service import collect_terms

    terms = collect_terms(name, {})
    assert terms is not None
    return terms


def _phone_out(line: str) -> str:
    from src.services.resume_export_service import redact_text, verify_text

    terms = _terms("Jane Doe")
    out = redact_text(f"Jane Doe\n{BODY}\n{line}", terms)
    verify_text(out, terms)
    return out


# ------------------------------------------- bracketed, unseparated phone numbers


@pytest.mark.parametrize(
    ("line", "digits"),
    [
        ("Mobile: (612345678)", "612345678"),
        ("Tel (91234567)", "91234567"),
        ("(5550199)", "5550199"),
    ],
)
def test_a_bracketed_unseparated_number_is_redacted(line: str, digits: str) -> None:
    out = _phone_out(line)
    assert digits not in out, out


# --------------------------------------------- labelled, unseparated phone numbers


@pytest.mark.parametrize(
    ("line", "digits"),
    [
        ("Mobile: 612345678", "612345678"),
        ("Tel: 91234567", "91234567"),
        ("Phone: 5550199", "5550199"),
    ],
)
def test_a_labelled_unseparated_number_of_seven_digits_is_redacted(
    line: str, digits: str
) -> None:
    out = _phone_out(line)
    assert digits not in out, out


# -------------------------------------------------------------------------- guards


@pytest.mark.parametrize(
    ("line", "span"),
    [
        ("Analyst, Telus (2015-2019)", "(2015-2019)"),
        ("Telus (2015-2019)", "(2015-2019)"),
        ("Acme Corp (2016 - 2019)", "(2016 - 2019)"),
        ("Graduated (01/02/2019)", "(01/02/2019)"),
        ("(01/02/2019)", "(01/02/2019)"),
    ],
)
def test_guard_bracketed_dates_keep_their_text(line: str, span: str) -> None:
    out = _phone_out(line)
    assert line in out, out
    assert span in out


def test_guard_a_labelled_six_digit_number_is_below_the_floor() -> None:
    out = _phone_out("Tel: 123456")
    assert re.search(r"123456", out), out


def test_guard_an_unlabelled_unseparated_seven_digit_number_is_not_a_phone() -> None:
    """Only the label lifts the floor; a bare 7-digit run is an id or a count."""
    out = _phone_out("Processed 5550199 records")
    assert "5550199" in out, out
