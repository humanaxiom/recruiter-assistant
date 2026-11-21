"""RED -- round 6: leaks and over-trust found in the round-5 fixes.

Pinned through ``redact_text`` / ``normalize_text`` / ``renderable_text`` and the
FULL ``redact_pdf`` pipeline on real PDFs.

* W  the vouch (``_require_name_found``) may pass only on the stored spelling
  and its umlaut-to-digraph expansions, never on a variant made by COLLAPSING a
  digraph (``haas`` -> ``has``, ``aarts`` -> ``arts``, ``laan`` -> ``lan``).
* renderer/normaliser drift: every letter ``_FOLD`` folds and every apostrophe
  ``_APOSTROPHES`` unifies must render as what the normaliser reads.
* phone label adjacency: a label only exempts a year span when it is ADJACENT;
  a label word inside prose or a name must not strip the dates around it.
* X  mixed spellings inside ONE token (``Muellerhofer`` for ``Müllerhöfer``).
* R  digits glued to a run-on handle go with it.
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
    """Must SHIP (not refuse) with none of ``gone`` (case-insensitive) in it."""
    shipped = _ship(lines, name)
    assert shipped is not None, "refused where redaction was expected"
    low = shipped.casefold()
    for g in gone:
        assert g.casefold() not in low, (g, shipped)


# ------------------------------------------------------------------------ W


@pytest.mark.parametrize(
    ("stored", "lines"),
    [
        (
            "Jan Haas",
            [
                "Peter Hase",
                "peter.hase@example.com",
                "Analyst who has led SQL work.",
            ],
        ),
        (
            "Anna Aarts",
            ["Maria Lopez", "Bachelor of Arts, 2018", f"{BODY}."],
        ),
        ("Tom Laan", ["Wei Zhang", "Built LAN and WAN networks."]),
        (
            "Kari Boe",
            [
                "Kari Bø",
                f"{BODY}.",
                "Publications: Bø, K. (2021). Data pipelines.",
                "Sincerely, K. Bø",
            ],
        ),
    ],
)
def test_w_the_vouch_never_passes_on_a_collapsed_variant(
    stored: str, lines: list[str]
) -> None:
    assert _ship(lines, stored) is None


@pytest.mark.parametrize(
    ("stored", "doc"),
    [("Mueller", "Müller"), ("Müller", "Mueller"), ("Bär", "Baer")],
)
def test_w_guard_the_round5_m1_pairs_still_ship_redacted(stored: str, doc: str) -> None:
    name = f"Jane {stored}"
    email = f"jane.{doc.lower()}@example.com"
    lines = [f"Jane {doc}", email, BODY, f"Sincerely, {doc}"]
    _redacted(lines, name, [doc])


@pytest.mark.parametrize("surname", ["Haas", "Aarts", "Laan"])
def test_w_guard_the_stored_spelling_itself_still_vouches(surname: str) -> None:
    name = f"Jan {surname}"
    shipped = _ship([name, BODY, f"Sincerely, {name}"], name)
    assert shipped is not None
    assert surname.casefold() not in shipped.casefold()


# ------------------------------------------------- renderer / normaliser drift


def _fold_letters() -> list[str]:
    from src.services.resume_export_service import _FOLD

    out: list[str] = []
    for key in _FOLD:
        out.append(key)
        if key.upper() != key and len(key.upper()) == 1:
            out.append(key.upper())
    return out


def _apostrophes() -> list[str]:
    from src.services.resume_export_service import _APOSTROPHES

    return sorted(_APOSTROPHES)


@pytest.mark.parametrize("ch", _fold_letters() + _apostrophes())
def test_drift_every_folded_letter_and_apostrophe_renders_and_agrees(ch: str) -> None:
    from src.services.resume_export_service import normalize_text, renderable_text

    drawn = renderable_text(ch)
    assert drawn != "?", f"U+{ord(ch):04X} is drawn as a question mark"
    assert normalize_text(drawn) == normalize_text(ch), (ch, drawn)


def test_drift_the_tables_are_not_empty() -> None:
    assert len(_fold_letters()) >= 19
    assert len(_apostrophes()) >= 10


def test_drift_place_names_ship_in_plain_letters() -> None:
    from src.services.resume_export_service import normalize_text

    places = [
        "Politechnika Wrocławska",
        "Łódź",
        "Kadıköy",
        "Đại học",
        "Hawaiʻi",
        "Ħamrun",
    ]
    lines = ["Jane Doe", BODY, *[f"Studied at {p}" for p in places]]
    shipped = _ship(lines, "Jane Doe")
    assert shipped is not None, "refused: every letter here has a plain form"
    assert "?" not in shipped
    norm = normalize_text(shipped)
    for p in places:
        assert normalize_text(p) in norm, (p, shipped)
    assert "doe" not in norm


# --------------------------------------------------------- phone label adjacency


def _phone_out(line: str) -> str:
    from src.services.resume_export_service import redact_text, verify_text

    terms = _terms("Jane Doe")
    out = redact_text(f"Jane Doe\n{BODY}\n{line}", terms)
    verify_text(out, terms)
    return out


@pytest.mark.parametrize(
    ("line", "span"),
    [
        ("Senior Mobile Developer, Acme 2015-2019", "2015-2019"),
        ("Cell & Molecular Biology Lab, 2016-2018", "2016-2018"),
        ("Tel Aviv University 2010-2014", "2010-2014"),
        ("Mobile, AL 2012-2014", "2012-2014"),
        ("Built mobile apps 2015-2019", "2015-2019"),
        ("Phone available on request. Analyst 2015-2019", "2015-2019"),
    ],
)
def test_label_in_prose_or_a_title_keeps_the_dates(line: str, span: str) -> None:
    out = _phone_out(line)
    assert line in out, out
    assert span in out


@pytest.mark.parametrize(
    "line",
    [
        "Telefon: 2015 2019",
        "Teléfono: 2015 2019",
        "Móvil: 2015 2019",
        "Handy: 2015 2019",
        "Mob: 2015 2019",
        "M: 2015 2019",
        "T: 2015 2019",
        "Ph: 2015 2019",
        "WhatsApp: 2015 2019",
        "Tél: 2015 2019",
        "Phone:\n2015 2019",
    ],
)
def test_adjacent_label_makes_a_year_span_a_phone_number(line: str) -> None:
    out = _phone_out(line)
    assert not re.search(r"\d{4}", out), out


# --------------------------------------------------------------------------- X


@pytest.mark.parametrize(
    ("name", "mixed"),
    [
        ("Jürgen Müllerhöfer Bäcker", "Muellerhöfer"),
        ("Jürgen Müllerhöfer Bäcker", "Muellerhofer"),
        ("Jürgen Müllerhöfer Bäcker", "Müllerhoefer"),
        ("Jürgen Müllerhöfer Bäcker-Lüth", "Muellerhofer"),
        ("Joel Michael Samuel Muellerhoefer", "Müllerhöfer"),
        ("Joel Michael Samuel Muellerhoefer", "Muellerhöfer"),
    ],
)
def test_x_mixed_spelling_inside_one_token_is_redacted(name: str, mixed: str) -> None:
    lines = [name, BODY, f"Ref: Dr. {mixed} supervised the thesis."]
    _redacted(lines, name, [mixed, "Muellerhofer", "Mullerhofer"])


@pytest.mark.parametrize(
    "name", ["Jürgen Müllerhöfer Bäcker", "Joel Michael Samuel Muellerhoefer"]
)
def test_x_guard_the_exact_stored_spelling_is_redacted(name: str) -> None:
    surname = name.split()[2]
    _redacted([name, BODY, f"Signed: {surname}"], name, [surname])


# --------------------------------------------------------------------------- R


def _text_out(line: str) -> str:
    return _phone_out(line).casefold()


def test_r_github_handle_with_run_on_digits_is_removed() -> None:
    out = _text_out("GitHub: janedoe88")
    assert "janedoe" not in out
    assert "88" not in out


def test_r_skype_handle_with_dotted_digits_is_removed() -> None:
    out = _text_out("Skype: janedoe.92")
    assert "janedoe" not in out


def test_r_underscore_joined_handle_with_digits_is_removed() -> None:
    out = _text_out("jane_doe_1990")
    assert "jane_doe" not in out
    assert "1990" not in out


def test_r_guard_a_plain_year_next_to_nothing_survives() -> None:
    assert "Graduated 2015-2019" in _phone_out("Graduated 2015-2019")
