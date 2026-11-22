"""RED -- round 7: a read-back spelling vouches only as the FULL name.

The vouch (``_vouch_forms`` / ``_require_name_found``) reads ``ae``/``oe``/``ue``
back as the plain letter and also accepts the surname tail alone, so stored
"Jan Baer" vouched on any document containing "bar" and someone else's resume
shipped with their name. Required: a read-back form may vouch only as the full
name (first name and surname adjacent), never as a surname-only tail. The stored
spelling and its umlaut expansions keep their behaviour, tail included.

Also pinned: a bracketed qualifier directly between a phone label and the
number (``Phone (mobile): 2015 2019``) and the separators ``::``, ``|``, ``=>``.
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


def _redacted(lines: list[str], name: str, gone: list[str]) -> None:
    shipped = _ship(lines, name)
    assert shipped is not None, "refused where redaction was expected"
    low = shipped.casefold()
    for g in gone:
        assert g.casefold() not in low, (g, shipped)


# ------------------------------------------- read-back never vouches as a tail

_MISATTRIBUTED = [
    pytest.param(
        "Jan Baer",
        [
            "Peter Klein",
            "peter.klein@example.com | 604-555-0199",
            "Called to the Bar of British Columbia, 2016.",
            "Built bar charts in Tableau.",
        ],
        id="MA1-baer-bar",
    ),
    pytest.param(
        "Ana Bues",
        ["Peter Klein", "Analyst. Designed the CAN bus logger."],
        id="MA2-bues-bus",
    ),
    pytest.param(
        "Kai Boes",
        ["Peter Klein", "Analyst. Taught at BOS academy."],
        id="MA3-boes-bos",
    ),
    pytest.param(
        "Eva Roes",
        ["Peter Klein", "Robotics engineer: ROS, ROS2, C++."],
        id="MA4-roes-ros",
    ),
    pytest.param(
        "Li Kuen",
        ["Peter Klein", "Analyst. Hobbies: Kun Khmer."],
        id="MA5-kuen-kun",
    ),
    pytest.param(
        "Max Mues",
        ["Peter Klein", "Analyst. Mus.B. (Bachelor of Music), 2014"],
        id="MA6-mues-mus",
    ),
    pytest.param(
        "Tim Hoey",
        ["Peter Klein", "Analyst. Spanish: hoy mismo."],
        id="MA7-hoey-hoy",
    ),
    pytest.param(
        "Udo Joest",
        ["Peter Jost", "Analyst with SQL skills."],
        id="MA8-joest-jost",
    ),
    pytest.param(
        "Lea Maes",
        ["Peter Klein", "MAS, Simon Fraser University, 2019"],
        id="MA9-maes-mas",
    ),
    pytest.param(
        "Ben Loew",
        ["Peter Klein", "Built low-latency trading systems."],
        id="MA10-loew-low",
    ),
    pytest.param(
        "Ida Huet",
        ["Peter Klein", "Shift lead, Pizza Hut, 2012-2014"],
        id="MA11-huet-hut",
    ),
    pytest.param(
        "Abe Toews",
        ["Peter Klein", "Dispatcher: scheduled tows for the fleet."],
        id="MA12-toews-tows",
    ),
    pytest.param(
        "Jose Saenz",
        ["Maria Sanz", "maria.sanz@example.com", "Analyst with SQL skills."],
        id="MA13-saenz-sanz",
    ),
    pytest.param(
        "Luis Paez",
        ["Peter Klein", "Volunteer, La Paz community centre."],
        id="MA14-paez-paz",
    ),
    pytest.param(
        "Ken Bauer",
        ["Anna Baur", "anna.baur@example.com"],
        id="MA15-bauer-baur",
    ),
    pytest.param(
        "Eva Daems",
        ["Peter Klein", "Engineer: tailings dams inspection."],
        id="MA16-daems-dams",
    ),
    pytest.param(
        "Ali Gael",
        ["Peter Klein", "Analyst. Pipeline throughput 500 gal/min."],
        id="MA17-gael-gal",
    ),
]


@pytest.mark.parametrize(("stored", "lines"), _MISATTRIBUTED)
def test_a_read_back_surname_tail_does_not_vouch_for_someone_elses_resume(
    stored: str, lines: list[str]
) -> None:
    assert _ship(lines, stored) is None


@pytest.mark.parametrize(
    ("stored", "doc_text"),
    [
        ("Jan Baer", "called to the bar"),
        ("Jan Baer", "jan"),
        ("Lea Maes", "mas"),
        ("Jose Saenz", "sanz"),
    ],
)
def test_require_name_found_refuses_a_read_back_tail_directly(
    stored: str, doc_text: str
) -> None:
    from src.services import resume_export_service as svc

    _, _, vouch = svc._compile_terms([stored])
    with pytest.raises(svc.NotAnonymizedError):
        svc._require_name_found(svc.normalize_text(doc_text), vouch)


# ---------------------------------------------------------------------- guards


@pytest.mark.parametrize(
    ("stored", "doc"),
    [
        ("Jane Mueller", "Jane Muller"),
        ("Jane Mueller", "Jane Müller"),
        ("Jan Baer", "Jan Bär"),
    ],
)
def test_guard_the_full_name_read_back_still_vouches_and_is_redacted(
    stored: str, doc: str
) -> None:
    first, surname = doc.split(" ")
    lines = [doc, f"{first.lower()}.{surname.lower()}@example.com", BODY]
    _redacted(lines, stored, [surname])


def test_guard_the_stored_spelling_tail_alone_still_vouches() -> None:
    _redacted(["Baer", BODY, "Sincerely, Baer"], "Jan Baer", ["Baer"])


def test_guard_the_stored_full_name_still_vouches() -> None:
    _redacted(["Jan Baer", BODY], "Jan Baer", ["Baer"])


# --------------------------------------------------------------- phone labels


def _phone_out(line: str) -> str:
    from src.services.resume_export_service import redact_text, verify_text

    terms = _terms("Jane Doe")
    out = redact_text(f"Jane Doe\n{BODY}\n{line}", terms)
    verify_text(out, terms)
    return out


@pytest.mark.parametrize(
    "line",
    [
        "Phone (mobile): 2015 2019",
        "Tel. (Home): 2015 2019",
        "Phone (mobile): 55-51-2345",
        "Phone:: 2015 2019",
        "Phone | 2015 2019",
        "Phone => 2015 2019",
    ],
)
def test_a_bracketed_or_symbol_separated_label_makes_the_number_a_phone(
    line: str,
) -> None:
    out = _phone_out(line)
    assert not re.search(r"\d{4}", out), out


@pytest.mark.parametrize(
    ("line", "span"),
    [
        ("Phone available on request. Analyst 2015-2019", "2015-2019"),
        ("Cell (molecular) biology 2015-2019", "2015-2019"),
    ],
)
def test_a_parenthetical_not_directly_before_the_number_keeps_the_dates(
    line: str, span: str
) -> None:
    out = _phone_out(line)
    assert line in out, out
    assert span in out
