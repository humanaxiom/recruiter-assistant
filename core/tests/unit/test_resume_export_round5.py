"""RED -- round 5: leaks a security audit found in the anonymized export.

Pinned through ``redact_text`` / ``verify_text`` / ``normalize_text`` and the
FULL ``redact_pdf`` pipeline on real PDFs. Every leak case ends REFUSED or with
the identifier absent from the shipped text.

* M1 transliteration variants (u-umlaut / ue, o-slash / oe, ...), both ways.
* M2 a letter the Latin-only renderer draws as ``?`` hides from verify while a
  reader still knows the name: (a) fail closed on any unrenderable LETTER, (b)
  fold the schwa and the Hausa/Azeri letters so those names redact.
* M3 letter-spaced SHORT names (``L I   N A``).
* M4 a name token glued to digits (``mctestface88``, ``doe2024``).
* m1 more apostrophe code points.
* m2 the year-range / date exemption of the phone rule is too wide.
* render width: long accented rows stay inside the page.
"""

from __future__ import annotations

import datetime
import re
import unicodedata

import fitz  # type: ignore[import-untyped]
import pytest

from tests.unit.resume_export_pdfs import build_pdf, extract

BODY = "Analyst with SQL skills"


def _terms(name: str, **kw: str) -> list[str]:
    from src.services.resume_export_service import collect_terms

    terms = collect_terms(name, {}, **kw)
    assert terms is not None
    return terms


def _ship(lines: list[str], name: str, **kw: str) -> str | None:
    """The text redact_pdf ships for ``lines``, or None when it refuses."""
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    try:
        return extract(redact_pdf(build_pdf(lines), _terms(name, **kw)))
    except NotAnonymizedError:
        return None


def _hidden(lines: list[str], name: str, gone: list[str], **kw: str) -> None:
    """REFUSED, or shipped with none of ``gone`` (case-insensitive) in it."""
    shipped = _ship(lines, name, **kw)
    if shipped is None:
        return
    low = shipped.casefold()
    for g in gone:
        assert g.casefold() not in low, (g, shipped)


# ----------------------------------------------------------------- M1


def _digraph_cases() -> list[tuple[str, str]]:
    pairs = [
        ("Bärg", "Baerg"),
        ("Köhn", "Koehn"),
        ("Müller", "Mueller"),
        ("Brøndal", "Broendal"),
        ("Håkon", "Haakon"),
        ("Næss", "Naess"),
        ("Køhn", "Koehn"),
    ]
    return [*pairs, *[(b, a) for a, b in pairs]]


@pytest.mark.parametrize(("stored", "doc"), _digraph_cases())
def test_m1_transliteration_variant_of_the_surname_does_not_ship(
    stored: str, doc: str
) -> None:
    name = f"Jane {stored}"
    lines = [name, BODY, f"Ref: Mr. {doc} led the team", f"Sincerely, {doc}"]
    _hidden(lines, name, [doc])


@pytest.mark.parametrize(
    ("name", "lines", "gone"),
    [
        (
            "Jane Mueller",
            ["Jane Müller", "jane.mueller@example.com", BODY, "Sincerely, Jane Müller"],
            ["müller", "mueller", "muller"],
        ),
        (
            "Jane Müller",
            ["Jane Müller", BODY, "Sincerely, Mueller"],
            ["mueller", "müller"],
        ),
        (
            "Soren Joergensen",
            [
                "Søren Jørgensen",
                "soren.joergensen@example.com",
                BODY,
                "Søren Jørgensen",
            ],
            ["søren", "jørgensen", "joergensen"],
        ),
        (
            "Jane Mueller",
            ["Jane Mueller", BODY, "Sincerely, MÜLLER"],
            ["müller", "muller"],
        ),
    ],
)
def test_m1_audit_cases(name: str, lines: list[str], gone: list[str]) -> None:
    _hidden(lines, name, gone)


def test_m1_variant_in_the_document_is_redacted_not_just_refused() -> None:
    """The variant spelling is a name: it must come out as the marker."""
    lines = ["Jane Mueller", BODY, "Sincerely, Jane Müller"]
    shipped = _ship(lines, "Jane Mueller")
    assert shipped is not None
    assert "ller" not in shipped.casefold()
    assert "[redacted]" in shipped


@pytest.mark.parametrize(
    ("name", "word"),
    [
        ("Joe Smith", "Jo"),
        ("Jane Muller", "Mullet"),
        ("Jane Aron", "Aaron"),
        ("Jane Smith", "Aaron"),
    ],
)
def test_m1_plain_names_do_not_redact_unrelated_words(name: str, word: str) -> None:
    lines = [name, BODY, f"Managed by {word} Brown and the team", f"Sincerely, {name}"]
    shipped = _ship(lines, name)
    assert shipped is not None, "an ordinary resume was refused"
    assert word in shipped


# ----------------------------------------------------------------- M2

_FOLDS_BOTH = {
    "ə": "a",
    "ɓ": "b",
    "ɗ": "d",
    "ƙ": "k",
    "ƴ": "y",
    "ŋ": "n",
    "ɛ": "e",
    "ɔ": "o",
    "Ə": "a",
    "Ɓ": "b",
    "Ɗ": "d",
    "Ƙ": "k",
    "Ƴ": "y",
    "Ŋ": "n",
    "Ɛ": "e",
    "Ɔ": "o",
}


@pytest.mark.parametrize(("raw", "expected"), sorted(_FOLDS_BOTH.items()))
def test_m2b_normalize_text_folds_these_letters(raw: str, expected: str) -> None:
    from src.services.resume_export_service import normalize_text

    assert normalize_text(f"x{raw}y") == f"x{expected}y"


def test_m2b_whole_names_normalize_alike() -> None:
    from src.services.resume_export_service import normalize_text

    assert normalize_text("Əli Məmmədov") == normalize_text("Ali Mammadov")
    assert normalize_text("Ɓello Ɗanjuma") == normalize_text("Bello Danjuma")


@pytest.mark.parametrize(
    ("name", "doc", "gone"),
    [
        ("Ali Mammadov", "Əli Məmmədov", ["?li", "m?mm?dov", "mammadov"]),
        ("Bello Danjuma", "Ɓello Ɗanjuma", ["?ello", "?anjuma", "danjuma"]),
    ],
)
def test_m2b_those_names_redact_rather_than_refuse(
    name: str, doc: str, gone: list[str]
) -> None:
    first, last = name.lower().split()
    lines = [doc, f"{first}.{last}@example.com", BODY, f"Sincerely, {doc}"]
    shipped = _ship(lines, name)
    assert shipped is not None, "folded names must redact, not refuse"
    for g in gone:
        assert g.casefold() not in shipped.casefold(), shipped
    assert "[redacted]" in shipped


@pytest.mark.parametrize(
    "name_doc", [("Ali Mammadov", "Əli Məmmədov"), ("Bello Danjuma", "Ɓello Ɗanjuma")]
)
def test_m2_audit_names_never_ship_in_any_form(name_doc: tuple[str, str]) -> None:
    name, doc = name_doc
    _hidden([doc, BODY], name, ["mammadov", "danjuma", "?li", "?ello", "m?mm"])


@pytest.mark.parametrize("letter", ["Ʃ", "ʒ", "ɸ", "Ƣ", "ƍ", "Ꝺ"])
def test_m2a_a_letter_that_would_render_as_question_mark_refuses(
    letter: str,
) -> None:
    from src.services.resume_export_service import renderable_text

    assert unicodedata.category(letter).startswith("L")
    assert renderable_text(letter) == "?", "fixture letter must be unrenderable"
    lines = ["Jane Doe", BODY, f"Fluent in the {letter} dialect", "Sincerely, Jane Doe"]
    assert _ship(lines, "Jane Doe") is None


@pytest.mark.parametrize("symbol", ["→", "✓", "★", "∑", "☺"])
def test_m2a_a_symbol_that_renders_as_question_mark_does_not_refuse(
    symbol: str,
) -> None:
    from src.services.resume_export_service import renderable_text

    assert not unicodedata.category(symbol).startswith("L")
    assert renderable_text(symbol) == "?"
    lines = ["Jane Doe", BODY, f"Skills {symbol} Python", "Sincerely, Jane Doe"]
    shipped = _ship(lines, "Jane Doe")
    assert shipped is not None, "a symbol is not a letter; do not refuse"
    assert "analyst with sql skills" in shipped.casefold()


# ----------------------------------------------------------------- M3

_SPACED = [
    ("Li Na", "L I   N A"),
    ("Bo Wu", "B O   W U"),
]


@pytest.mark.parametrize(("name", "header"), _SPACED)
def test_m3_letter_spaced_short_name_does_not_ship(name: str, header: str) -> None:
    lines = [header, BODY, f"Sincerely, {name}"]
    shipped = _ship(lines, name)
    if shipped is None:
        return
    letters = name.replace(" ", "")
    pattern = r"\b" + r"\s?".join(letters[:2]) + r"\s+" + r"\s?".join(letters[2:])
    assert not re.search(pattern, shipped, re.I), shipped


@pytest.mark.parametrize(("name", "header"), _SPACED)
def test_m3_letter_spaced_short_name_alone_is_redacted_or_refused(
    name: str, header: str
) -> None:
    shipped = _ship([header, BODY, name], name)
    if shipped is not None:
        assert not re.search(r"\bl\s?i\s+n\s?a\b|\bb\s?o\s+w\s?u\b", shipped, re.I)


@pytest.mark.parametrize("name", ["Li Na", "Bo Wu", "Jane Doe"])
@pytest.mark.parametrize("phrase", ["A B C analysis", "Q & A", "Step A B then C"])
def test_m3_ordinary_single_letter_runs_still_ship(name: str, phrase: str) -> None:
    lines = [name, BODY, f"Ran {phrase} sessions", f"Sincerely, {name}"]
    shipped = _ship(lines, name)
    assert shipped is not None, "an ordinary resume was refused"
    assert phrase.casefold() in shipped.casefold()


# ----------------------------------------------------------------- M4


@pytest.mark.parametrize(
    ("name", "handle", "gone"),
    [
        ("Testy McTestface", "Skype: mctestface88", ["mctestface"]),
        ("Testy McTestface", "Instagram: testy1990", ["testy1990", "testy"]),
        ("Testy McTestface", "88mctestface", ["mctestface"]),
        ("Testy McTestface", "WeChat: mctestface_88", ["mctestface"]),
        ("Jane Doe", "Discord: doe2024", ["doe2024", "doe"]),
    ],
)
def test_m4_name_glued_to_digits_does_not_ship(
    name: str, handle: str, gone: list[str]
) -> None:
    lines = [name, BODY, handle, f"Sincerely, {name}"]
    _hidden(lines, name, gone)


@pytest.mark.parametrize(
    ("name", "handle"),
    [
        ("Testy McTestface", "Skype: mctestface88"),
        ("Jane Doe", "Discord: doe2024"),
    ],
)
def test_m4_the_handle_is_redacted_in_text(name: str, handle: str) -> None:
    from src.services.resume_export_service import redact_text, verify_text

    terms = _terms(name)
    out = redact_text(f"{name}\n{handle}\n", terms)
    verify_text(out, terms)
    assert "88" not in out and "2024" not in out


@pytest.mark.parametrize("word", ["SQL2019", "Python3", "Win10"])
@pytest.mark.parametrize("name", ["Jane Doe", "Testy McTestface"])
def test_m4_technology_names_with_digits_survive(name: str, word: str) -> None:
    lines = [name, BODY, f"Used {word} daily", f"Sincerely, {name}"]
    shipped = _ship(lines, name)
    assert shipped is not None
    assert word in shipped


# ----------------------------------------------------------------- m1

_APOS = ["ʻ", "ʽ", "ʾ", "ʿ", "ʹ", "ˈ", "ꞌ"]


def test_m1_code_points_are_the_ones_named() -> None:
    assert [f"{ord(c):04X}" for c in _APOS] == [
        "02BB",
        "02BD",
        "02BE",
        "02BF",
        "02B9",
        "02C8",
        "A78C",
    ]


@pytest.mark.parametrize("apos", _APOS)
def test_m1_apostrophe_variants_normalize_to_an_ascii_apostrophe(apos: str) -> None:
    from src.services.resume_export_service import normalize_text

    assert normalize_text(f"To{apos}xtayev") == "to'xtayev"


@pytest.mark.parametrize("apos", _APOS)
def test_m1_stored_variant_apostrophe_found_with_ascii_in_document(apos: str) -> None:
    name = f"Bekzod To{apos}xtayev"
    lines = [name, BODY, "Ref: Mr. To'xtayev led the team"]
    _hidden(lines, name, ["xtayev"])


@pytest.mark.parametrize("apos", _APOS)
def test_m1_stored_ascii_apostrophe_found_with_variant_in_document(apos: str) -> None:
    lines = ["Bekzod To'xtayev", BODY, f"Ref: Mr. To{apos}xtayev led the team"]
    _hidden(lines, "Bekzod To'xtayev", ["xtayev"])


@pytest.mark.parametrize(
    ("stored", "doc"),
    [("Ilʹya Petrov", "Ilya Petrov"), ("Ilya Petrov", "Ilʹya")],
)
def test_m1_prime_modifier_in_a_given_name(stored: str, doc: str) -> None:
    lines = [stored, BODY, f"Sincerely, {doc}"]
    _hidden(lines, stored, ["ilya"])


# ----------------------------------------------------------------- m2


def _phone_out(line: str) -> str:
    from src.services.resume_export_service import redact_text, verify_text

    terms = _terms("Jane Doe")
    out = redact_text(f"Jane Doe\n{BODY}\n{line}", terms)
    verify_text(out, terms)
    return out


@pytest.mark.parametrize(
    "line",
    [
        "Tel: 2019 2088",
        "Tel: 2019-2088",
        "Tel: 01/2019-2045",
        "Tel: 1/1955 2034",
        "Tel: 55-51-2345",
        "Tel: 12.34.5678",
        "TEL: 2019 2088",
        "Phone: 2015-2019",
        "PHONE 2015 2019",
        "Cell: 09/2019 - 04/2021",
        "mobile: 2015-2019",
        "Fax: 2019-2021",
        "Work phone: +1 2019 2088",
    ],
)
def test_m2_labelled_number_is_redacted(line: str) -> None:
    out = _phone_out(line)
    assert not re.search(r"\d{4}", out), out


@pytest.mark.parametrize(
    "line",
    [
        "13/2019 - 04/2021",
        "00/2019 - 04/2021",
        "2021-2019",
        "09/2021 - 04/2019",
        f"2015-{datetime.date.today().year + 2}",
        "2019 2088",
    ],
)
def test_m2_implausible_date_ranges_are_redacted(line: str) -> None:
    out = _phone_out(line)
    assert not re.search(r"\d{4}", out), out


@pytest.mark.parametrize(
    "line",
    [
        "09/2019 – 04/2021",
        "2015–2019",
        "2015-2019",
        "2019 - 2021",
        "09/2019 - 04/2021",
        "12/2019 - 01/2020",
        f"2015-{datetime.date.today().year + 1}",
        f"2015-{datetime.date.today().year}",
        "2019-2019",
    ],
)
def test_m2_real_date_ranges_without_a_label_survive(line: str) -> None:
    out = _phone_out(line)
    assert line in out


def test_m2_a_label_on_an_earlier_line_does_not_remove_the_exemption() -> None:
    out = _phone_out("Tel:\n2015-2019")
    assert "2015-2019" in out


def test_m2_a_label_after_the_range_does_not_remove_the_exemption() -> None:
    out = _phone_out("2015-2019 (see phone interview notes)")
    assert "2015-2019" in out


def test_m2_a_label_earlier_on_the_line_beats_a_later_range() -> None:
    out = _phone_out("Employed 2015-2019, tel 2019-2088")
    assert "2088" not in out


# ----------------------------------------------------------------- render width


@pytest.mark.parametrize(
    "text",
    [
        " ".join(["Ægir Ångström"] * 40),
        " ".join(["é" * n for n in range(1, 40)]),
        "é" * 400,
    ],
)
def test_render_text_pdf_keeps_accented_words_inside_the_page(text: str) -> None:
    from src.services.resume_export_service import render_text_pdf

    doc = fitz.open(stream=render_text_pdf(text), filetype="pdf")
    try:
        seen: list[str] = []
        for page in doc:
            for w in page.get_text("words"):
                assert page.rect.contains(fitz.Rect(w[:4])), w[4]
                seen.append(w[4])
    finally:
        doc.close()
    assert "".join(seen) == "".join(text.split())
