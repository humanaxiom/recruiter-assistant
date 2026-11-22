"""RED -- round 4: names the anonymizer's folding and splitting missed.

Pinned, all through ``redact_text`` / ``verify_text`` and the FULL
``redact_pdf`` pipeline on a real PDF (no new public names):

* Letters with no Unicode decomposition fold to ASCII for terms AND text:
  o-slash->o, ae->ae, eth/d-stroke->d, thorn->th, l-stroke/l-middle-dot->l,
  dotless i->i, oe->oe, h-stroke->h, t-stroke->t (``normalize_text`` does it).
  A stored ``Soren`` must find ``Soren`` with a slash through the o, and back.
* A name is also split on hyphens and apostrophes; every part of 2+ letters is a
  term (``Smith-Jones`` -> Smith, Jones; ``O'Brien`` -> Brien, not ``O``), with
  run-ons (``mjones``, ``jonesmary``, ``mary.jones``) and squashed needles
  (``J O N E S``) built per part.
* Run-ons match next to digits and underscores (``janedoe88``, ``JaneDoe2024``,
  ``jdoe_99``, ``88janedoe``) but ``SQL2019`` and ``Q3 2024`` stay.
* A letter-spaced FIRST name of 4+ letters alone (``J A N E``) is redacted or
  the file is refused -- never shipped.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import pytest

from tests.unit.resume_export_pdfs import build_pdf, extract

BODY = "Analyst with SQL skills"
FOLD = {
    "ø": "o",
    "æ": "ae",
    "ð": "d",
    "đ": "d",
    "þ": "th",
    "ł": "l",
    "ŀ": "l",
    "ı": "i",
    "œ": "oe",
    "ħ": "h",
    "ŧ": "t",
}
_UPPER = {k.upper(): v for k, v in FOLD.items() if k != "ı"}


def _fold(text: str) -> str:
    """The test's own fold, independent of the service: casefold, the table,
    accents off, everything but letters and digits dropped."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", text.casefold()):
        if unicodedata.combining(ch):
            continue
        out.append(FOLD.get(ch, ch))
    return re.sub(r"[\W_]+", "", "".join(out))


def _terms(name: str, bare: bool = False) -> list[str]:
    from src.services.resume_export_service import collect_terms

    if bare:
        return [name]
    terms = collect_terms(name, None)
    assert terms is not None
    return terms


def _redact(text: str, name: str, bare: bool = False) -> str:
    from src.services.resume_export_service import redact_text

    return str(redact_text(text, _terms(name, bare)))


def _pipeline(lines: list[str], name: str, extra: bytes = b"") -> str:
    from src.services.resume_export_service import redact_pdf

    out = redact_pdf(build_pdf(lines, extra=extra), _terms(name))
    return extract(out)


def _redacted_or_refused(lines: list[str], name: str, gone: str, **kw: Any) -> None:
    from src.services.resume_export_service import NotAnonymizedError

    try:
        text = _pipeline(lines, name, **kw)
    except NotAnonymizedError:
        return
    assert gone not in _fold(text), text


# ------------------------------------------------------ letters that do not fold


@pytest.mark.parametrize(("raw", "expected"), [*FOLD.items(), *_UPPER.items()])
def test_normalize_text_folds_letters_with_no_decomposition(
    raw: str, expected: str
) -> None:
    from src.services.resume_export_service import normalize_text

    assert normalize_text(raw) == expected


FOLD_PAIRS = [
    ("Soren Kierkegaard", "Søren Kierkegaard"),
    ("Søren Kierkegaard", "Soren Kierkegaard"),
    ("Lukasz Nowak", "Łukasz Nowak"),
    ("Łukasz Nowak", "Lukasz Nowak"),
    ("Gudrun Sigurdsson", "Guðrún Sigurdsson"),
    ("Guðrún Sigurdsson", "Gudrun Sigurdsson"),
    ("Thor Odinsson", "Þór Odinsson"),
    ("Þór Odinsson", "Thor Odinsson"),
    ("Aesa Berg", "Æsa Berg"),
    ("Æsa Berg", "Aesa Berg"),
    ("Dorde Petrovic", "Đorđe Petrović"),
    ("Đorđe Petrović", "Dorde Petrovic"),
    ("Hamrun Zammit", "Ħamrun Zammit"),
    ("Oeuvre Faure", "Œuvre Faure"),
]


@pytest.mark.parametrize(("stored", "document"), FOLD_PAIRS)
def test_redact_text_finds_the_other_spelling(stored: str, document: str) -> None:
    out = _redact(f"{document}\n{BODY}", stored)

    words = out.split()
    tail = BODY.split()
    assert words[-len(tail) :] == tail, out
    assert set(words[: -len(tail)]) == {"[redacted]"}, out


@pytest.mark.parametrize(("stored", "document"), FOLD_PAIRS)
def test_verify_text_refuses_the_other_spelling(stored: str, document: str) -> None:
    from src.services.resume_export_service import NotAnonymizedError, verify_text

    with pytest.raises(NotAnonymizedError):
        verify_text(f"Contact {document} today", _terms(stored))


@pytest.mark.parametrize(("stored", "document"), FOLD_PAIRS)
def test_the_full_pipeline_leaves_nothing_of_the_name(
    stored: str, document: str
) -> None:
    text = _pipeline([document, BODY], stored)

    words = text.split()
    tail = BODY.split()
    assert words[-len(tail) :] == tail, text
    assert set(words[: -len(tail)]) == {"[redacted]"}, text


def test_a_stored_name_with_a_slashed_o_finds_the_first_name_alone() -> None:
    text = _pipeline(["Kierkegaard", "Signed: Soren", BODY], "Søren Kierkegaard")

    assert "soren" not in _fold(text)
    assert "kierkegaard" not in _fold(text)


def test_a_plain_stored_name_finds_the_slashed_first_name_alone() -> None:
    text = _pipeline(["Kierkegaard", "Signed: Søren", BODY], "Soren Kierkegaard")

    assert "soren" not in _fold(text)


# ------------------------------------------------------------ compound surnames

MARY = "Mary Smith-Jones"


@pytest.mark.parametrize("bare", [False, True])
@pytest.mark.parametrize(
    ("line", "gone"),
    [
        ("As Dr. Jones (my former supervisor) can attest", "jones"),
        ("Jones, M. and Lee, K. (2021)", "jones"),
        ("Reference: Smith", "smith"),
        ("Smith said so", "smith"),
    ],
)
def test_one_part_of_a_hyphenated_surname_alone_is_redacted(
    line: str, gone: str, bare: bool
) -> None:
    out = _redact(f"{MARY}\n{line}\n{BODY}", MARY, bare)

    assert gone not in _fold(out.split("\n", 1)[1]), out
    assert out.split("\n")[-1] == BODY


@pytest.mark.parametrize(
    ("line", "gone"),
    [
        ("As Dr. Jones (my former supervisor) can attest", "jones"),
        ("Jones, M. and Lee, K. (2021)", "jones"),
        ("Reference: Smith", "smith"),
    ],
)
def test_the_pipeline_redacts_one_part_of_a_hyphenated_surname(
    line: str, gone: str
) -> None:
    text = _pipeline([MARY, line, BODY], MARY)

    assert gone not in _fold(text), text
    assert "SQL" in text


def test_verify_text_refuses_a_lone_surname_part() -> None:
    from src.services.resume_export_service import NotAnonymizedError, verify_text

    with pytest.raises(NotAnonymizedError):
        verify_text("As Dr. Jones can attest", _terms(MARY))


@pytest.mark.parametrize("bare", [False, True])
def test_the_brien_of_o_brien_alone_is_redacted(bare: bool) -> None:
    name = "Sean O'Brien"

    out = _redact(f"{name}\nMr Brien led it\n{BODY}", name, bare)

    assert "brien" not in _fold(out)
    assert out.split("\n")[-1] == BODY


def test_the_pipeline_redacts_brien_alone_and_a_curly_apostrophe_name() -> None:
    text = _pipeline(["Sean O’Brien", "Mr Brien led it", BODY], "Sean O'Brien")

    assert "brien" not in _fold(text)


def test_a_one_letter_part_is_not_a_term() -> None:
    out = _redact(f"Sean O'Brien\nOption O is cheaper\n{BODY}", "Sean O'Brien")

    assert "Option O is cheaper" in out


@pytest.mark.parametrize("bare", [False, True])
def test_every_part_of_a_hyphenated_first_name_is_redacted(bare: bool) -> None:
    name = "Anne-Marie Dupont"

    out = _redact(f"{name}\nMarie ran the team\nAnne too\n{BODY}", name, bare)

    assert "marie" not in _fold(out)
    assert "anne" not in _fold(out)


@pytest.mark.parametrize(
    "run_on",
    [
        "MaryJones",
        "mary.jones",
        "mary_smith",
        "marysmith",
        "mjones",
        "msmith",
        "m.jones",
        "m_smith",
        "jonesmary",
        "jones.m",
        "smith_m",
        "jonesm",
    ],
)
def test_run_ons_are_built_per_surname_part(run_on: str) -> None:
    out = _redact(f"{MARY}\nGitHub {run_on} portfolio\n{BODY}", MARY)

    assert _fold(run_on) not in _fold(out), out
    assert "portfolio" in out


@pytest.mark.parametrize("spaced", ["J O N E S", "S M I T H", "j-o-n-e-s"])
def test_a_spaced_out_surname_part_is_redacted_or_refused(spaced: str) -> None:
    from src.services.resume_export_service import NotAnonymizedError

    try:
        out = _redact(f"{MARY}\n{spaced}\n{BODY}", MARY)
    except NotAnonymizedError:
        return
    assert _fold(spaced) not in _fold(out), out


def test_a_spaced_out_brien_is_redacted_or_refused() -> None:
    from src.services.resume_export_service import NotAnonymizedError

    try:
        out = _redact("Sean O'Brien\nB R I E N\n" + BODY, "Sean O'Brien")
    except NotAnonymizedError:
        return
    assert "brien" not in _fold(out)


@pytest.mark.parametrize("line", ["Jonesboro, Arkansas", "Smithfield Foods", "Jonesy"])
def test_a_surname_part_does_not_match_inside_a_longer_word(line: str) -> None:
    out = _redact(f"{MARY}\n{line}\n{BODY}", MARY)

    assert out.split("\n")[1] == line


# ----------------------------------------------------- run-ons next to digits

JANE = "Jane Doe"


@pytest.mark.parametrize(
    ("line", "gone"),
    [
        ("GitHub: janedoe88", "janedoe"),
        ("JaneDoe2024 portfolio", "janedoe"),
        ("jdoe_99", "jdoe"),
        ("88janedoe", "janedoe"),
        ("see doejane7 here", "doejane"),
    ],
)
def test_a_run_on_with_digits_or_an_underscore_is_redacted(
    line: str, gone: str
) -> None:
    out = _redact(f"{JANE}\n{line}\n{BODY}", JANE)

    assert gone not in _fold(out.split("\n", 1)[1]), out
    assert out.split("\n")[-1] == BODY


@pytest.mark.parametrize(
    "line", ["GitHub: janedoe88", "JaneDoe2024 portfolio", "jdoe_99", "88janedoe"]
)
def test_the_pipeline_redacts_a_run_on_with_digits(line: str) -> None:
    text = _pipeline([JANE, line, BODY], JANE)

    assert "janedoe" not in _fold(text)
    assert "jdoe" not in _fold(text)
    assert "SQL" in text


def test_verify_text_refuses_a_run_on_with_digits() -> None:
    from src.services.resume_export_service import NotAnonymizedError, verify_text

    with pytest.raises(NotAnonymizedError):
        verify_text("GitHub: janedoe88", _terms(JANE))


@pytest.mark.parametrize(
    "line", ["SQL2019", "Python3", "Q3 2024", "Janet2024 and Doerr7", "ES6 and C99"]
)
def test_digits_beside_ordinary_words_are_left_alone(line: str) -> None:
    out = _redact(f"{JANE}\n{line}\n{BODY}", JANE)

    assert out.split("\n")[1] == line


# --------------------------------------------- a letter-spaced first name alone

TJ_JANE = b"BT /F 22 Tf 1 0 0 1 72 650 Tm [(J) -400 (A) -400 (N) -400 (E)] TJ ET"


def test_typed_spaces_in_a_first_name_alone_are_redacted_or_refused() -> None:
    _redacted_or_refused([JANE, "J A N E", BODY], JANE, "jane")


def test_a_kerned_first_name_header_alone_is_redacted_or_refused() -> None:
    _redacted_or_refused([JANE, BODY], JANE, "jane", extra=TJ_JANE)


def test_the_text_level_spaced_first_name_is_redacted_or_refused() -> None:
    from src.services.resume_export_service import NotAnonymizedError

    try:
        out = _redact(f"{JANE}\nJ A N E\n{BODY}", JANE)
    except NotAnonymizedError:
        return
    assert "jane" not in _fold(out)


def test_verify_text_refuses_a_spaced_first_name() -> None:
    from src.services.resume_export_service import NotAnonymizedError, verify_text

    with pytest.raises(NotAnonymizedError):
        verify_text("Analyst\nJ A N E\nSQL", _terms(JANE))


def test_a_spaced_first_name_needs_to_start_and_end_on_token_boundaries() -> None:
    out = _redact(f"{JANE}\nRaj Anesthetic Lead\n{BODY}", JANE)

    assert out.split("\n")[1] == "Raj Anesthetic Lead"
