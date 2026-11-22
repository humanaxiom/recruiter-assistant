"""RED -- round 4: what the text-only renderer does to characters.

PyMuPDF's base-14 Helvetica draws every character outside Latin-1 as a middle
dot (U+00B7). So ``2019-2021`` written with an en dash came out of the rebuilt
copy as ``2019.2021``, the SECOND verify took that for a phone number, and a
perfectly good resume was refused. The same holds for curly quotes, bullets,
ellipsis, the euro sign, the trademark sign and the cp1252 letters that Latin-1
lacks (S/Z/Y with caron or diaeresis, OE).

Names the coder must provide in ``src.services.resume_export_service``::

    renderable_text(text: str) -> str
        # The text exactly as the renderer will show it, and the ONLY mapping
        # render_text_pdf applies. Newlines kept; other whitespace and control
        # characters become one space; format (Cf) characters are dropped.
        # Every other character becomes Latin-1 text that Helvetica really
        # draws -- never a middle dot (except a source U+00B7):
        #   hyphens and dashes U+2010..U+2015 -> "-"
        #   U+2018 U+2019 -> "'"       U+201C U+201D -> '"'
        #   U+2022 (bullet) -> "-"     U+2026 (ellipsis) -> "..."
        #   U+20AC -> "EUR"            U+2122 -> "TM"
        #   S/Z/Y/OE with caron or diaeresis fold to an ASCII base
        # A character with no mapping at all becomes "?".

``render_text_pdf`` and ``redact_pdf`` must show ``renderable_text`` of their
input, so what is verified is what is drawn.

Also pinned here: the second-script list grows to Greek, Armenian, Georgian,
Ethiopic, Bengali, Tamil and Cyrillic Extended-B.
"""

from __future__ import annotations

import importlib
import re

import pytest

from tests.unit.resume_export_pdfs import build_pdf, extract

NAME = "Jane Doe"
MARKER = "[redacted]"


def _terms(name: str = NAME) -> list[str]:
    from src.services.resume_export_service import collect_terms

    terms = collect_terms(name, None)
    assert terms is not None
    return terms


def _renderable(text: str) -> str:
    module = importlib.import_module("src.services.resume_export_service")

    return str(module.renderable_text(text))


def _shipped(lines: list[str], name: str = NAME) -> str:
    from src.services.resume_export_service import redact_pdf

    out = redact_pdf(build_pdf(lines), _terms(name))
    return " ".join(extract(out).split())


# ------------------------------------------------- the pipeline ships them

SAMPLES = [
    ("2015–2019", "2015-2019", 1),
    ("2019 – 2021", "2019 - 2021", 1),
    ("2019 — 2021", "2019 - 2021", 1),
    ("09/2019 – 04/2021", "09/2019 - 04/2021", 1),
    ("Jane’s", "[redacted]'s", 2),
    ("“quoted”", '"quoted"', 1),
    ("• SQL", "- SQL", 1),
    ("Ellipsis…", "Ellipsis...", 1),
    ("€500", "EUR500", 1),
    ("Trademark™", "TrademarkTM", 1),
    ("Škoda", "Skoda", 1),
    ("Œuvre", "oeuvre", 1),
]


@pytest.mark.parametrize(("sample", "expected", "markers"), SAMPLES)
def test_a_resume_with_non_latin1_punctuation_ships_readably(
    sample: str, expected: str, markers: int
) -> None:
    text = _shipped([NAME, f"Analyst, {sample}, built dashboards"])

    assert "·" not in text
    assert expected.casefold() in text.casefold()
    assert text.count(MARKER) == markers, text
    assert "built dashboards" in text


@pytest.mark.parametrize(
    "sample", ["2015–2019", "2019 – 2021", "2019 — 2021", "09/2019 – 04/2021"]
)
def test_a_year_range_is_never_taken_for_a_phone_number(sample: str) -> None:
    from src.services.resume_export_service import redact_text

    out = redact_text(f"{NAME}\nAnalyst, {sample}\nSQL", _terms())

    assert out.split("\n")[1] == f"Analyst, {sample}"


def test_a_pdf_whose_source_draws_the_dash_as_winansi_0x96_ships() -> None:
    from src.services.resume_export_service import redact_pdf

    raw = build_pdf(
        [NAME],
        extra=b"BT /F 11 Tf 1 0 0 1 72 650 Tm (Led 2015\\226 2019) Tj ET",
    )
    assert "2015–2019" in extract(raw).replace(" ", "")

    out = redact_pdf(raw, _terms())

    text = " ".join(extract(out).split())
    assert "2015-2019" in text.replace(" ", "")
    assert "·" not in text


def test_several_samples_in_one_document_all_ship() -> None:
    lines = [NAME] + [f"Item {i}: {s}" for i, (s, _e, _m) in enumerate(SAMPLES[:4])]

    text = _shipped(lines)

    assert "·" not in text
    assert text.count("2019") >= 4


# ------------------------------------------------------------ the mapping


@pytest.mark.parametrize("dash", list("‐–—―"))
def test_dashes_become_an_ascii_hyphen(dash: str) -> None:
    assert _renderable(f"a{dash}b") == "a-b"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("‘", "'"),
        ("’", "'"),
        ("“", '"'),
        ("”", '"'),
        ("•", "-"),
        ("…", "..."),
        ("€", "EUR"),
        ("™", "TM"),
        ("Š", "S"),
        ("š", "s"),
        ("Ž", "Z"),
        ("ž", "z"),
        ("Ÿ", "Y"),
    ],
)
def test_the_documented_substitutions(raw: str, expected: str) -> None:
    assert _renderable(raw) == expected


@pytest.mark.parametrize("raw", ["Œ", "œ"])
def test_the_oe_ligature_folds_to_two_ascii_letters(raw: str) -> None:
    assert _renderable(raw).casefold() == "oe"


@pytest.mark.parametrize(
    "raw", ["plain ASCII 123", "café naïve Ñandú", "ÿ ß ½ ©", "line one\nline two"]
)
def test_what_helvetica_already_draws_passes_through_unchanged(raw: str) -> None:
    assert _renderable(raw) == raw


def test_whitespace_and_controls_become_one_space_and_format_chars_vanish() -> None:
    assert _renderable("a\tb\x07c d") == "a b c d"
    assert _renderable("a​b­c") == "abc"


def test_a_character_with_no_mapping_is_a_question_mark_not_a_dot() -> None:
    assert _renderable("a☃b") == "a?b"


def test_render_text_pdf_draws_the_mapped_text() -> None:
    from src.services.resume_export_service import render_text_pdf

    text = "• 2015–2019 “x” €5 Škoda…"

    drawn = " ".join(extract(render_text_pdf(text)).split())

    assert drawn == " ".join(_renderable(text).split())
    assert "·" not in drawn


# ------------------------------------------------- the broad property test

_RANGES = (
    (0x20, 0x24F),
    (0x250, 0x2FF),
    (0x2000, 0x2BFF),
    (0x3000, 0x303F),
    (0xFB00, 0xFB06),
    (0xFF01, 0xFF5E),
    (0x1F600, 0x1F64F),
)
_CODEPOINTS = [chr(c) for lo, hi in _RANGES for c in range(lo, hi + 1)]
_CHUNK = 50


def test_every_mapped_character_is_latin1_and_never_a_stray_dot() -> None:
    for ch in _CODEPOINTS:
        mapped = _renderable(ch)
        mapped.encode("latin-1")
        assert "·" not in mapped or ch == "·", f"U+{ord(ch):04X} -> {mapped!r}"
        assert not re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", mapped)


@pytest.mark.parametrize("start", range(0, len(_CODEPOINTS), _CHUNK))
def test_rendering_then_extracting_returns_exactly_the_mapped_text(
    start: int,
) -> None:
    from src.services.resume_export_service import render_text_pdf

    chunk = _CODEPOINTS[start : start + _CHUNK]
    text = " ".join(f"a{ch}b" for ch in chunk)

    drawn = extract(render_text_pdf(text)).split()

    assert drawn == _renderable(text).split()


# ------------------------------------------------------- the second script

_NEW_SCRIPTS = {
    "greek": "Αθήνα",
    "armenian": "Աննա",
    "georgian": "ანა",
    "ethiopic": "አበበ",
    "bengali": "অনন্যা",
    "tamil": "அனு",
    "cyrillic_ext_b": "Ꙁꙁ",
}


@pytest.mark.parametrize("script", sorted(_NEW_SCRIPTS))
def test_a_newly_listed_script_refuses_when_the_name_lacks_it(script: str) -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_text

    with pytest.raises(NotAnonymizedError):
        redact_text(f"{NAME} ({_NEW_SCRIPTS[script]})\nAnalyst", _terms())


@pytest.mark.parametrize("script", sorted(_NEW_SCRIPTS))
def test_verify_text_refuses_a_newly_listed_script(script: str) -> None:
    from src.services.resume_export_service import NotAnonymizedError, verify_text

    with pytest.raises(NotAnonymizedError):
        verify_text(f"Analyst {_NEW_SCRIPTS[script]}", _terms())


GREEK_NAME = "Ανδρέας Παπαδόπουλος"


def test_a_stored_greek_name_present_in_the_text_is_redacted_not_refused() -> None:
    from src.services.resume_export_service import redact_text

    out = redact_text(f"{GREEK_NAME}\nAnalyst with SQL skills", _terms(GREEK_NAME))

    assert not re.search("[Α-ω]", out), out
    assert MARKER in out.split("\n")[0]
    assert out.split("\n")[-1] == "Analyst with SQL skills"


def test_other_greek_text_next_to_a_stored_greek_name_still_refuses() -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_text

    with pytest.raises(NotAnonymizedError):
        redact_text(f"{GREEK_NAME} Αθήνα\nAnalyst", _terms(GREEK_NAME))
