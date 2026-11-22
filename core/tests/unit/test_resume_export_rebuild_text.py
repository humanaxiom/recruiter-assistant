"""RED -- round 3: the anonymized copy is REBUILT from redacted text (text level).

Two audits and two reviews kept finding identity inside "anonymized" PDFs that
were edited in place (vector-outline names, ``/ActualText``, XMP on form
XObjects, UTF-16 OCG names, second-script names, letter-spaced headers).
User decision: the copy is a NEW PDF built from the extracted text, after the
text was redacted and verified. This file pins the TEXT half of that pipeline;
``test_resume_export_rebuild_pdf.py`` pins the PDF half.

Names the coder must provide in ``src.services.resume_export_service``::

    REDACTION_MARKER = "[redacted]"
    redact_text(text: str, terms: list[str]) -> str
        # terms[0] is the FULL name (the collect_terms convention). Pure.
        # Lines are kept 1:1 and in order; ordinary lines come back unchanged
        # (same case, same spacing); every redaction is REDACTION_MARKER.
        # Raises NotAnonymizedError when the name/surname is not in the text
        # (the vouch) or when a second script survives (see below). May drop or
        # refuse a letter-spaced name, but never return it.
    verify_text(text: str, terms: list[str]) -> None
        # Raises NotAnonymizedError when ANY term, name token, e-mail, phone,
        # URL/domain, @handle or postal code is still in the text, or when the
        # text with spaces and separators removed still holds the full name
        # (>= 5 chars) or the surname (>= 4 chars). The squashed match only
        # counts when it starts and ends on a token boundary of the original
        # text, so ``Ann Ford`` (surname "ford") does not eat ``Stanford``.

Second script: after redaction, any remaining Han, Hangul, Hiragana, Katakana,
Cyrillic, Arabic, Hebrew, Devanagari or Thai character refuses the file. A
stored name written in such a script is REDACTED (not refused), and what is left
of that script after that still refuses.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

NAME = "Jane Doe"
BODY = "Analyst with SQL skills"


def _terms(name: str = NAME, **kw: Any) -> list[str]:
    from src.services.resume_export_service import collect_terms

    terms = collect_terms(name, None, **kw)
    assert terms is not None
    return terms


def _redact(text: str, terms: list[str] | None = None) -> str:
    from src.services.resume_export_service import redact_text

    return str(redact_text(text, _terms() if terms is None else terms))


def _verify(text: str, terms: list[str] | None = None) -> None:
    from src.services.resume_export_service import verify_text

    verify_text(text, _terms() if terms is None else terms)


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


# ------------------------------------------------------------------- marker


def test_the_marker_is_a_module_constant() -> None:
    from src.services.resume_export_service import REDACTION_MARKER

    assert REDACTION_MARKER == "[redacted]"


def test_the_name_becomes_a_visible_marker_and_ordinary_text_survives() -> None:
    out = _redact(f"Prepared by {NAME} for review\n{BODY}")

    lines = out.split("\n")
    assert len(lines) == 2
    assert re.fullmatch(r"Prepared by (?:\[redacted\] ?)+for review", lines[0])
    assert lines[1] == BODY


def test_lines_stay_one_to_one_and_in_order() -> None:
    src = ["First line SQL", f"{NAME}", "Third line Python", "Fourth line R"]

    out = _redact("\n".join(src)).split("\n")

    assert len(out) == len(src)
    assert [out[0], out[2], out[3]] == [src[0], src[2], src[3]]
    assert "[redacted]" in out[1]


def test_whole_word_matching_keeps_stanford_when_the_name_is_ann_ford() -> None:
    terms = _terms("Ann Ford")

    out = _redact("Ann Ford\nPlanned annual budgets at Stanford", terms).split("\n")

    assert "[redacted]" in out[0]
    assert out[1] == "Planned annual budgets at Stanford"


def test_a_name_split_over_two_lines_is_redacted() -> None:
    out = _redact(f"JANE\nDOE\n{BODY}")

    assert not re.search(r"(?i)\b(jane|doe)\b", out)
    assert out.split("\n")[-1] == BODY


def test_a_two_letter_surname_is_redacted_without_eating_other_words() -> None:
    terms = _terms("Wei Li")

    out = _redact("LI, WEI\nReliable analyst; improved policies", terms).split("\n")

    assert not re.search(r"(?i)\b(li|wei)\b", out[0])
    assert out[1] == "Reliable analyst; improved policies"


def test_accent_and_case_variants_of_the_stored_name_are_redacted() -> None:
    terms = _terms("Jose Garcia")

    out = _redact(f"JOSÉ GARCÍA\n{BODY}", terms)

    assert "garc" not in out.casefold()
    assert out.split("\n")[-1] == BODY


def test_stored_email_phone_and_location_are_used_as_terms() -> None:
    terms = _terms(email="jd@home.example", phone="+1 604 555 0142")
    terms.append("Burnaby, BC")

    out = _redact(f"{NAME}\nLives in Burnaby, BC\n{BODY}", terms)

    assert "burnaby" not in out.casefold()
    assert out.split("\n")[-1] == BODY


# ----------------------------------------------------------------- the vouch


def test_a_name_absent_from_the_text_is_refused() -> None:
    from src.services.resume_export_service import NotAnonymizedError

    with pytest.raises(NotAnonymizedError):
        _redact("Experienced analyst\ncontact@example.invalid")


def test_the_surname_alone_vouches_for_the_text() -> None:
    out = _redact(f"Reference: Doe, J.\n{BODY}")

    assert not re.search(r"(?i)\bdoe\b", out)
    assert out.split("\n")[-1] == BODY


@pytest.mark.parametrize("text", ["", "   \n  "])
def test_empty_text_is_refused(text: str) -> None:
    from src.services.resume_export_service import NotAnonymizedError

    with pytest.raises(NotAnonymizedError):
        _redact(text)


# ---------------------------------------------------------- second script

SCRIPTS = {
    "han": "杜简",
    "hangul": "제인",
    "hiragana": "じぇん",
    "katakana": "ジェーン",
    "cyrillic": "Джейн",
    "arabic": "جين",
    "hebrew": "גיין",
    "devanagari": "जेन",
    "thai": "เจน",
}


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_a_second_script_the_name_does_not_contain_refuses_the_file(
    script: str,
) -> None:
    from src.services.resume_export_service import NotAnonymizedError

    text = f"{NAME} ({SCRIPTS[script]}) Hangzhou\n{BODY}"

    with pytest.raises(NotAnonymizedError):
        _redact(text)


def test_accented_latin_is_not_a_second_script() -> None:
    out = _redact(f"{NAME}\nJosé García, Zürich, São Paulo\n{BODY}")

    assert "José García, Zürich, São Paulo" in out


def test_a_stored_name_in_another_script_is_redacted_not_refused() -> None:
    han = SCRIPTS["han"]
    terms = _terms(han)

    out = _redact(f"{han}\n{BODY}", terms)

    assert han not in out
    assert "[redacted]" in out.split("\n")[0]
    assert out.split("\n")[-1] == BODY


def test_other_han_text_next_to_a_stored_han_name_still_refuses() -> None:
    from src.services.resume_export_service import NotAnonymizedError

    han = SCRIPTS["han"]

    with pytest.raises(NotAnonymizedError):
        _redact(f"{han} 杭州\n{BODY}", _terms(han))


# ---------------------------------------------- personal web and contact data

SHAPES = [
    ("Portfolio: janedoe.ca", ("janedoe",)),
    ("Code: gitlab.com/jdoe88", ("gitlab", "jdoe88")),
    ("See https://x.y/z for details", ("x.y", "https")),
    ("Site www.mysite.org", ("mysite",)),
    ("Short link bit.ly/3xyz", ("bit.ly", "3xyz")),
    ("Blog: https://medium.com/@recruit_me", ("medium", "recruit_me")),
    ("Slack @recruit_me", ("recruit_me",)),
    ("Mastodon @RecruitMe42", ("recruitme42",)),
    ("4521 Kingsway Ave, Burnaby BC V5H 2A9", ("v5h", "2a9")),
    ("Postal code V5H2A9", ("v5h2a9", "v5h")),
    ("Reach me at jane.doe@example.com", ("example.com", "@example")),
]


@pytest.mark.parametrize(("line", "leaks"), SHAPES)
def test_domains_urls_handles_and_postal_codes_are_redacted(
    line: str, leaks: tuple[str, ...]
) -> None:
    out = _redact(f"{NAME}\n{line}\n{BODY}")

    lowered = out.casefold()
    for leak in leaks:
        assert leak not in lowered, leak
    assert "[redacted]" in out
    assert out.split("\n")[-1] == BODY


RUN_ONS = [
    ("Jane Doe", "janedoe"),
    ("Jane Doe", "JaneDoe"),
    ("Jane Doe", "JANEDOE"),
    ("Jane Doe", "jdoe"),
    ("Jane Doe", "JDoe"),
    ("Jane Doe", "jane.doe"),
    ("Jane Doe", "jane_doe"),
    ("Jane Doe", "Jane_Doe"),
    ("Jane Doe", "doej"),
    ("Jane Doe", "DoeJ"),
    ("Testy McTestface", "testymctestface"),
    ("Testy McTestface", "tmctestface"),
    ("Testy McTestface", "testy.mctestface"),
    ("Testy McTestface", "testy_mctestface"),
    ("Testy McTestface", "mctestfacet"),
    ("Wei Li", "weili"),
    ("Wei Li", "wei.li"),
]


@pytest.mark.parametrize(("name", "form"), RUN_ONS)
def test_run_ons_derived_from_the_stored_name_are_redacted(
    name: str, form: str
) -> None:
    out = _redact(f"{name}\nSlack: {form}\n{BODY}", _terms(name))

    assert form.casefold() not in out.casefold()
    assert out.split("\n")[-1] == BODY


GUARDS = [
    "Ph.D. in Statistics",
    "Python 3.11 and Node.js",
    "Tools, e.g. pandas, i.e. numpy.",
    "U.S. citizen, version 2.4.1",
    "Reduced costs by 12.5% and managed $1.2M",
]


@pytest.mark.parametrize("line", GUARDS)
def test_ordinary_dotted_text_is_not_mangled(line: str) -> None:
    out = _redact(f"{NAME}\n{line}").split("\n")

    assert out[1] == line


def test_every_redacted_shape_at_once_passes_verification() -> None:
    text = "\n".join(
        [
            NAME,
            "jane.doe@example.com  604-555-0142",
            "https://x.y/z  janedoe.ca  @recruit_me  V5H 2A9",
            BODY,
        ]
    )

    out = _redact(text)

    _verify(out)
    assert out.split("\n")[-1] == BODY


# ------------------------------------------------------- letter-spaced names

LETTER_SPACED = [
    "J A N E   D O E",
    "J.A.N.E  D.O.E",
    "j a n e d o e",
    "J-A-N-E  D-O-E",
]


@pytest.mark.parametrize("header", LETTER_SPACED)
def test_a_letter_spaced_name_is_dropped_or_the_file_is_refused(header: str) -> None:
    from src.services.resume_export_service import NotAnonymizedError

    text = f"{header}\n{BODY}\nContact jane.doe@example.com"

    try:
        out = _redact(text)
    except NotAnonymizedError:
        return  # refusing is an acceptable answer; returning the name is not
    assert "janedoe" not in _squash(out)
    _verify(out)


# --------------------------------------------------------------- verify_text

CLEAN = [
    BODY,
    "[redacted] [redacted]",
    "Ph.D. in Statistics",
    "Python 3.11",
    "2018 - 2021 Analyst",
    "Stanford University",
    "Reduced costs by 12.5%",
    "I do enjoy it",
]


@pytest.mark.parametrize("text", CLEAN)
def test_verify_accepts_clean_text(text: str) -> None:
    _verify(f"{text}\n{BODY}")


SURVIVORS = [
    "Jane",
    "Doe",
    "jane.doe@example.com",
    "604-555-0142",
    "(604) 555-0142",
    "https://x.y/z",
    "janedoe.ca",
    "gitlab.com/jdoe88",
    "@recruit_me",
    "V5H 2A9",
    "V5H2A9",
    "J A N E   D O E",
    "J-A-N-E_D-O-E",
    "J.A.N.E.D.O.E",
    "j a n e d o e",
    "janedoe",
    "Jane_Doe",
]


@pytest.mark.parametrize("leak", SURVIVORS)
def test_verify_refuses_any_survivor(leak: str) -> None:
    from src.services.resume_export_service import NotAnonymizedError

    with pytest.raises(NotAnonymizedError):
        _verify(f"{BODY}\n{leak}")


@pytest.mark.parametrize(
    "leak",
    ["M C T E S T F A C E", "Mc Test face", "M-c-T-e-s-t-f-a-c-e", "McTestface"],
)
def test_verify_refuses_a_squashed_surname_of_four_or_more_letters(
    leak: str,
) -> None:
    from src.services.resume_export_service import NotAnonymizedError

    with pytest.raises(NotAnonymizedError):
        _verify(f"{BODY}\n{leak}", _terms("Testy McTestface"))


def test_verify_refuses_a_squashed_full_name_of_five_letters() -> None:
    from src.services.resume_export_service import NotAnonymizedError

    with pytest.raises(NotAnonymizedError):
        _verify(f"{BODY}\nW E I L I", _terms("Wei Li"))


@pytest.mark.parametrize("text", ["Stanford", "I can afford it", "Afford-able"])
def test_a_squashed_match_must_start_on_a_token_boundary(text: str) -> None:
    _verify(f"{BODY}\n{text}", _terms("Ann Ford"))


def test_verify_still_refuses_the_whole_word_when_it_is_there() -> None:
    from src.services.resume_export_service import NotAnonymizedError

    with pytest.raises(NotAnonymizedError):
        _verify(f"{BODY}\nAnn Ford", _terms("Ann Ford"))


def test_verify_refuses_a_stored_location_term() -> None:
    from src.services.resume_export_service import NotAnonymizedError

    terms = [*_terms(), "Burnaby, BC"]

    with pytest.raises(NotAnonymizedError):
        _verify(f"{BODY}\nLives in Burnaby, BC", terms)
