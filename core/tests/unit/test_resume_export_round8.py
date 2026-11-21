"""RED -- round 8.

1. A read-back form may vouch only when the stored name has 2+ tokens. A one-word
   stored name ("Baer") read back as "bar" vouched on any document with "Bar".
2. A parenthetical between a phone label and a number counts only when its
   content is a phone word ("(mobile)", "(home/work)"), not "(iOS)".
3. A year range in brackets with no label ("Telus (2015-2019)") keeps its dates:
   the phone pattern swallows the "(", which defeated the date exemptions.

Not pinned either way: "Mobile | 2016-2019", "Mobile > 2016-2019" and
"Mobile = 2016-2019" (a bare word plus a symbol is ambiguous; round 7 pins
"Phone | 2015 2019" as redacted).
"""

from __future__ import annotations

import re

import pytest

from tests.unit.resume_export_pdfs import build_pdf, extract

BODY = "Analyst with SQL skills"


def _terms(name: str) -> list[str]:
    from src.services.resume_export_service import collect_terms

    terms = collect_terms(name, {})
    assert terms is not None
    return terms


def _ship(lines: list[str], name: str) -> str | None:
    """The text redact_pdf ships for ``lines``, or None when it refuses."""
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    try:
        return extract(redact_pdf(build_pdf(lines), _terms(name)))
    except NotAnonymizedError:
        return None


# ----------------------------------------------- one-word stored name, read-back


@pytest.mark.parametrize(
    ("stored", "lines"),
    [
        pytest.param(
            "Baer",
            ["Peter Klein", "Called to the Bar of BC."],
            id="baer-bar",
        ),
        pytest.param(
            "Loew",
            ["Peter Klein", "Built low-latency systems."],
            id="loew-low",
        ),
        pytest.param("Huet", ["Peter Klein", "Pizza Hut"], id="huet-hut"),
        pytest.param("Paez", ["Peter Klein", "La Paz"], id="paez-paz"),
    ],
)
def test_a_one_word_stored_name_is_not_vouched_by_its_read_back(
    stored: str, lines: list[str]
) -> None:
    assert _ship(lines, stored) is None


@pytest.mark.parametrize("doc", ["Baer", "Bär"])
def test_guard_a_one_word_stored_name_still_vouches_on_its_own_spellings(
    doc: str,
) -> None:
    shipped = _ship([doc, BODY], "Baer")
    assert shipped is not None, "refused where redaction was expected"
    assert doc.casefold() not in shipped.casefold(), shipped


# ------------------------------------------------------------------ phone words


def _phone_out(line: str) -> str:
    from src.services.resume_export_service import redact_text, verify_text

    terms = _terms("Jane Doe")
    out = redact_text(f"Jane Doe\n{BODY}\n{line}", terms)
    verify_text(out, terms)
    return out


@pytest.mark.parametrize(
    ("line", "span"),
    [
        ("Mobile (iOS) 2015-2019", "2015-2019"),
        ("Mobile (iOS/Android) 2016 - 2019", "2016 - 2019"),
        ("Cell (Biology) 09/2017 - 04/2019", "09/2017 - 04/2019"),
    ],
)
def test_a_non_phone_word_parenthetical_after_a_label_keeps_the_dates(
    line: str, span: str
) -> None:
    out = _phone_out(line)
    assert line in out, out
    assert span in out


@pytest.mark.parametrize(
    "line",
    [
        "Phone (mobile): 2015 2019",
        "Tel. (Home): 2015 2019",
        "Phone (mobile): 55-51-2345",
        "Phone (home/work): 2015 2019",
    ],
)
def test_a_phone_word_parenthetical_after_a_label_still_makes_a_phone(
    line: str,
) -> None:
    out = _phone_out(line)
    assert not re.search(r"\d{4}", out), out


# --------------------------------------------------- bracketed, unlabelled dates


@pytest.mark.parametrize(
    ("line", "span"),
    [
        ("Analyst, Telus (2015-2019)", "(2015-2019)"),
        ("Acme Corp (2016 - 2019)", "(2016 - 2019)"),
        ("Research Assistant (09/2017 - 04/2019)", "(09/2017 - 04/2019)"),
    ],
)
def test_a_bracketed_year_range_keeps_its_dates_and_brackets(
    line: str, span: str
) -> None:
    out = _phone_out(line)
    assert line in out, out
    assert span in out


@pytest.mark.parametrize(
    "line",
    [
        "(604) 555-0199",
        "Tel: (604) 555-0199",
        "Phone (2015-2019) role",
    ],
)
def test_a_bracketed_phone_number_is_still_redacted(line: str) -> None:
    out = _phone_out(line)
    assert not re.search(r"555|0199|2015", out), out
