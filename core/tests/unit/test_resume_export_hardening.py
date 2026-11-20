"""RED -- slice H3: the export anonymizer is stripped, then verified on its own.

Review of slice H found the anonymized copies could still carry identity in
places the redaction pass never looked (links, annotations, embedded files,
bookmarks, images, text outside the CropBox) and could be "verified" only
against the same term list that drove the redaction. Contract pinned here for
``src.services.resume_export_service`` (additive to ``test_resume_export_
service.py``, which stays green):

* ``redact_pdf(pdf, terms)`` -- ``terms[0]`` is the candidate's FULL name (the
  ``collect_terms`` convention); the surname is its last whitespace token. The
  output has NO links, NO annotations, NO embedded files, NO outline and NO
  images, or the call raises ``NotAnonymizedError``.
* ``MAX_IMAGE_AREA_FRACTION`` (module constant, ``0 < x < 1``) -- a page whose
  images cover MORE than this fraction of its area is refused (scan / image
  résumé: the identity is in pixels no text pass can verify).
* Before redacting, the full name -- or failing that the surname token -- must
  be findable in the ORIGINAL text (after normalization), else refuse: a
  verification that only checks the term list cannot notice a name the list
  never held.
* ``normalize_text(s)`` -- NFKC, casefold, accent-stripping, apostrophe and
  hyphen variants unified, whitespace collapsed and stripped. The survivor
  check runs on normalized text, and terms/page text are compared normalized.
* Name tokens match as WHOLE WORDS (``Ann Ford`` must not eat
  ``Planned annual budgets at Stanford``); two-letter tokens (``Li``) count.
* Phones: 7-digit local numbers and ``/``, en-dash, NBSP separated numbers;
  ``linkedin.com/in/...`` and ``github.com/...`` profile URLs are redacted
  by shape, not by term.
* ``collect_terms(name, parsed, *, email=None, phone=None)`` also returns the
  stored email, phone and ``parsed["candidate"]["location"]``.
* Text outside the CropBox (but inside the MediaBox) is redacted too.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import fitz  # type: ignore[import-untyped]
import pytest

NAME = "Testy McTestface"
EMAIL = "testy.mctestface@example.invalid"
PHONE = "604-555-0142"
EMPLOYER = "Zorblax Analytics"
TERMS = [NAME, "Testy", "McTestface", EMPLOYER]
BODY = [
    NAME,
    f"{EMAIL}  {PHONE}",
    "Experienced in recruiting and operations.",
    f"Worked at {EMPLOYER} with stakeholders.",
]
BIG = fitz.Rect(-5000, -5000, 5000, 5000)
RAW = fitz.TEXT_PRESERVE_WHITESPACE  # no MediaBox/CropBox clipping


def _finish(doc: Any) -> bytes:
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _page_with(lines: list[str], *, start: float = 72) -> tuple[Any, Any]:
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((72, start + i * 16), line, fontsize=11)
    return doc, page


def _pix(side: int = 16) -> Any:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, side, side), False)
    pix.clear_with(180)
    return pix


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return " ".join(str(p.get_text()) for p in doc)
    finally:
        doc.close()


def _service_norm(text: str) -> str:
    from src.services.resume_export_service import normalize_text

    return str(normalize_text(text))


def _norm(text: str) -> str:
    """The test's OWN normalization, so a missing ``normalize_text`` does not
    mask whether the redaction behaviour itself is right."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = "".join(
        c for c in unicodedata.normalize("NFD", t) if not unicodedata.combining(c)
    )
    t = re.sub("[\u2018\u2019\u02bc\u00b4`]", "'", t)
    t = re.sub("[\u2010\u2011\u2012\u2013\u2014]", "-", t)
    return re.sub(r"\s+", " ", t).strip()


def _words(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", _norm(text)))


def _assert_stripped(pdf: bytes) -> None:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        assert doc.embfile_count() == 0
        assert doc.get_toc() == []
        for page in doc:
            assert page.get_links() == []
            assert list(page.annots() or []) == []
            assert page.get_images(full=True) == []
    finally:
        doc.close()
    raw = pdf.lower()
    for leak in (b"mctestface", b"604-555", b"example.invalid", b"linkedin"):
        assert leak not in raw, leak


# ------------------------------------------------ A1: strip every channel

VECTORS = [
    "uri_link",
    "mailto_link",
    "text_annot",
    "freetext_annot",
    "embedded_file",
    "bookmark",
    "icon_image",
]


def _vector_pdf(vector: str) -> bytes:
    doc, page = _page_with(BODY)
    vectors = VECTORS if vector == "all" else [vector]
    for v in vectors:
        if v == "uri_link":
            page.insert_link(
                {
                    "kind": fitz.LINK_URI,
                    "from": fitz.Rect(72, 94, 300, 108),  # over plain text
                    "uri": "https://www.linkedin.com/in/testy-mctestface",
                }
            )
        elif v == "mailto_link":
            page.insert_link(
                {
                    "kind": fitz.LINK_URI,
                    "from": fitz.Rect(400, 600, 500, 620),  # over no text
                    "uri": f"mailto:{EMAIL}",
                }
            )
        elif v == "text_annot":
            page.add_text_annot(fitz.Point(400, 300), f"{NAME} {PHONE}")
        elif v == "freetext_annot":
            page.add_freetext_annot(
                fitz.Rect(300, 400, 560, 440), f"{NAME} call {PHONE}", fontsize=10
            )
        elif v == "embedded_file":
            doc.embfile_add("source.txt", f"{NAME} {PHONE}".encode(), filename="s.txt")
        elif v == "bookmark":
            doc.set_toc([[1, f"{NAME} - Resume", 1]])
        elif v == "icon_image":
            page.insert_image(fitz.Rect(450, 60, 490, 100), pixmap=_pix())
    return _finish(doc)


@pytest.mark.parametrize("vector", [*VECTORS, "all"])
def test_every_identity_channel_is_stripped_or_the_copy_is_refused(
    vector: str,
) -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    src = _vector_pdf(vector)
    try:
        out = redact_pdf(src, TERMS)
    except NotAnonymizedError:
        return  # refusing is an acceptable answer; shipping the channel is not
    _assert_stripped(out)
    assert "mctestface" not in _norm(_text(out))
    assert "recruiting" in _norm(_text(out))


# ------------------------------------------- A2: image-heavy pages refused


def _scanned_pdf(*, ocr_layer: bool, fraction: float, lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(
        fitz.Rect(0, 0, page.rect.width, page.rect.height * fraction), pixmap=_pix(60)
    )
    if ocr_layer:
        for i, line in enumerate(lines):
            page.insert_text((72, 100 + i * 16), line, fontsize=11, render_mode=3)
    return _finish(doc)


def test_the_image_threshold_is_a_sane_module_constant() -> None:
    from src.services.resume_export_service import MAX_IMAGE_AREA_FRACTION

    assert isinstance(MAX_IMAGE_AREA_FRACTION, float)
    assert 0.0 < MAX_IMAGE_AREA_FRACTION < 1.0


@pytest.mark.parametrize("ocr_layer", [True, False])
def test_a_scanned_page_is_refused_with_or_without_an_ocr_layer(
    ocr_layer: bool,
) -> None:
    from src.services.resume_export_service import (
        MAX_IMAGE_AREA_FRACTION,
        NotAnonymizedError,
        redact_pdf,
    )

    fraction = (1.0 + MAX_IMAGE_AREA_FRACTION) / 2
    src = _scanned_pdf(ocr_layer=ocr_layer, fraction=fraction, lines=BODY)

    with pytest.raises(NotAnonymizedError):
        redact_pdf(src, TERMS)


def test_a_name_that_lives_only_in_a_large_image_is_refused() -> None:
    from src.services.resume_export_service import (
        MAX_IMAGE_AREA_FRACTION,
        NotAnonymizedError,
        redact_pdf,
    )

    fraction = (1.0 + MAX_IMAGE_AREA_FRACTION) / 2
    # Ordinary body text elsewhere on the page; the name is not in it at all.
    body = ["Experienced in recruiting and operations.", f"Worked at {EMPLOYER}."]
    src = _scanned_pdf(ocr_layer=True, fraction=fraction, lines=body)

    with pytest.raises(NotAnonymizedError):
        redact_pdf(src, TERMS)


# --------------------------------- A3: the name must be found, independently


def test_a_name_absent_from_the_original_text_is_refused() -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    doc, _page = _page_with(
        [
            "contact@example.invalid  604-555-0142",
            "Experienced in recruiting.",
            f"At {EMPLOYER}.",
        ]
    )

    with pytest.raises(NotAnonymizedError):
        redact_pdf(_finish(doc), TERMS)


def test_the_surname_alone_is_enough_to_vouch_for_the_copy() -> None:
    from src.services.resume_export_service import redact_pdf

    doc, _page = _page_with(
        ["Reference: McTestface, T.", f"{EMAIL}", "Experienced in recruiting."]
    )

    out = redact_pdf(_finish(doc), TERMS)

    assert "mctestface" not in _norm(_text(out))
    assert "recruiting" in _norm(_text(out))


# --------------------------------------------------- A4: normalization


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("O’Brien", "o'brien"),
        ("O‘Brien", "o'brien"),
        ("OʼBrien", "o'brien"),
        ("Anne‑Marie", "anne-marie"),
        ("Anne‐Marie", "anne-marie"),
        ("Anne–Marie", "anne-marie"),
        ("José", "jose"),
        ("José", "jose"),
        ("Ariﬁn Oﬀor", "arifin offor"),
        ("Straße", "strasse"),
        ("  A  \n B  ", "a b"),
    ],
)
def test_normalize_text_unifies_the_variants(raw: str, expected: str) -> None:
    assert _service_norm(raw) == expected


@pytest.mark.parametrize(
    ("in_pdf", "parsed"),
    [
        ("Sean O'Brien", "Sean O’Brien"),
        ("Anne-Marie Roy", "Anne‑Marie Roy"),
        ("José García", "José García"),
        ("Jose Garcia", "José García"),
        ("Arifin Offor", "Ariﬁn Oﬀor"),
    ],
)
def test_name_variants_in_the_pdf_and_the_parsed_name_still_match(
    in_pdf: str, parsed: str
) -> None:
    from src.services.resume_export_service import collect_terms, redact_pdf

    doc, _page = _page_with([in_pdf, "Experienced in recruiting and operations."])
    src = _finish(doc)
    assert _norm(in_pdf) in _norm(_text(src)), "fixture glyphs did not survive"
    terms = collect_terms(parsed, None)
    assert terms is not None

    out = redact_pdf(src, terms)

    survivors = _words(_text(out)) & _words(in_pdf)
    assert not survivors, survivors
    assert "recruiting" in _norm(_text(out))


def test_survivor_check_runs_on_normalized_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    doc, _page = _page_with(["Jose Garcia", "Experienced in recruiting."])
    # Nothing can be located, so nothing is redacted: the accent-folding
    # survivor check must still see "jose garcia" == "josé garcía".
    monkeypatch.setattr(fitz.Page, "search_for", lambda *_a, **_k: [])

    with pytest.raises(NotAnonymizedError):
        redact_pdf(_finish(doc), ["José García", "José", "García"])


# ------------------------------------------- A5: short names, whole words


def test_collect_terms_keeps_a_two_letter_surname() -> None:
    from src.services.resume_export_service import collect_terms

    terms = collect_terms("Wei Li", None)

    assert terms is not None
    assert "Li" in terms
    assert terms[0] == "Wei Li"


def test_a_last_name_first_header_is_redacted_without_eating_other_words() -> None:
    from src.services.resume_export_service import collect_terms, redact_pdf

    doc, _page = _page_with(
        [
            "LI, WEI",
            "Reliable analyst; improved policies and delivery.",
            "Weighted scoring models for hiring.",
        ]
    )
    terms = collect_terms("Wei Li", None)
    assert terms is not None

    out = redact_pdf(_finish(doc), terms)

    words = _words(_text(out))
    assert "li" not in words
    assert "wei" not in words
    for kept in ("reliable", "policies", "delivery", "weighted"):
        assert kept in words, kept


def test_name_tokens_match_whole_words_only() -> None:
    from src.services.resume_export_service import collect_terms, redact_pdf

    doc, _page = _page_with(
        ["Ann Ford", "Planned annual budgets at Stanford", "Experienced operator."]
    )
    terms = collect_terms("Ann Ford", None)
    assert terms is not None

    out = redact_pdf(_finish(doc), terms)

    flat = _norm(_text(out))
    assert "planned annual budgets at stanford" in flat
    assert "ann ford" not in flat
    assert not {"ann", "ford"} & _words(_text(out))


# ----------------------------------------------- A6: phones and profiles


@pytest.mark.parametrize(
    "number",
    [
        "555-1234",
        "+1 604/555/1234",
        "604–555–0142",
        "604 555 0142",
        "(604) 555-0142",
    ],
)
def test_phone_variants_are_redacted(number: str) -> None:
    from src.services.resume_export_service import collect_terms, redact_pdf

    doc, _page = _page_with([NAME, f"Phone: {number}", "Experienced operator."])
    src = _finish(doc)
    tail = re.sub(r"\D", "", number)[-7:]
    assert tail in re.sub(r"\D", "", _text(src)), "fixture lost the separators"
    terms = collect_terms(NAME, None)
    assert terms is not None

    out = redact_pdf(src, terms)

    assert tail not in re.sub(r"\D", "", _text(out))
    assert "experienced" in _norm(_text(out))


def test_dates_and_year_ranges_are_not_mistaken_for_phones() -> None:
    from src.services.resume_export_service import collect_terms, redact_pdf

    doc, _page = _page_with(
        [
            NAME,
            "Analyst, 2018 - 2021",
            "Manager 2015-2020",
            "Fiscal 2019/2020",
            "Experienced operator.",
        ]
    )
    terms = collect_terms(NAME, None)
    assert terms is not None

    flat = _norm(_text(redact_pdf(_finish(doc), terms)))

    for kept in ("2018 - 2021", "2015-2020", "2019/2020"):
        assert kept in flat, kept


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("linkedin.com/in/tm-4821", "tm-4821"),
        ("https://www.linkedin.com/in/tm-4821/", "tm-4821"),
        ("github.com/tmcode", "tmcode"),
        ("https://github.com/tmcode", "tmcode"),
    ],
)
def test_profile_urls_are_redacted_by_shape(url: str, secret: str) -> None:
    from src.services.resume_export_service import collect_terms, redact_pdf

    doc, _page = _page_with([NAME, f"Profile: {url}", "Experienced operator."])
    terms = collect_terms(NAME, None)
    assert terms is not None

    flat = _norm(_text(redact_pdf(_finish(doc), terms)))

    assert secret not in flat
    assert "linkedin.com/in" not in flat
    assert "github.com/" not in flat


# ---------------------------------------------------- A7: extra terms


def test_collect_terms_accepts_stored_email_phone_and_parsed_location() -> None:
    from src.services.resume_export_service import collect_terms

    parsed: dict[str, Any] = {
        "candidate": {"name": NAME, "location": "Burnaby, BC"},
        "experience": [{"company": EMPLOYER}],
    }

    terms = collect_terms(NAME, parsed, email="t.mc@sfu.example", phone="+16045550142")

    assert terms is not None
    assert terms[0] == NAME
    lowered = {t.lower() for t in terms}
    assert {"t.mc@sfu.example", "+16045550142", "burnaby, bc"} <= lowered
    assert EMPLOYER.lower() in lowered


@pytest.mark.parametrize("location", [None, "", 12, ["Burnaby"]])
def test_a_junk_location_or_missing_contact_fields_are_skipped(
    location: Any,
) -> None:
    from src.services.resume_export_service import collect_terms

    parsed: dict[str, Any] = {"candidate": {"name": NAME, "location": location}}

    terms = collect_terms(NAME, parsed, email=None, phone=None)

    assert terms is not None
    assert all(isinstance(t, str) and t.strip() for t in terms)


def test_the_parsed_location_is_redacted_from_the_copy() -> None:
    from src.services.resume_export_service import collect_terms, redact_pdf

    doc, _page = _page_with([NAME, "Burnaby, BC", "Experienced in recruiting."])
    terms = collect_terms(NAME, {"candidate": {"location": "Burnaby, BC"}})
    assert terms is not None

    out = redact_pdf(_finish(doc), terms)

    assert "burnaby" not in _norm(_text(out))
    assert "recruiting" in _norm(_text(out))


# ------------------------------------------------ A8: outside the CropBox


def test_text_outside_the_cropbox_is_not_left_behind() -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    doc, page = _page_with([NAME, "Experienced in recruiting."])
    page.insert_text((400, 300), f"Reference {NAME} {PHONE}", fontsize=11)
    page.set_cropbox(fitz.Rect(0, 0, 300, 400))
    src = _finish(doc)
    probe = fitz.open(stream=src, filetype="pdf")
    try:
        seen = probe[0].get_text(clip=BIG, flags=RAW)
        assert "555-0142" in seen, "fixture text not placed"
    finally:
        probe.close()

    try:
        out = redact_pdf(src, TERMS)
    except NotAnonymizedError:
        return
    check = fitz.open(stream=out, filetype="pdf")
    try:
        everything = " ".join(str(p.get_text(clip=BIG, flags=RAW)) for p in check)
    finally:
        check.close()
    assert "mctestface" not in _norm(everything)
    assert "555-0142" not in everything
