"""RED -- round 3: the anonymized copy is a NEW, text-only PDF (PDF level).

User decision: the anonymized copy is no longer the original PDF redacted in
place. It is built from scratch from the redacted, verified text. Nothing from
the original file carries over: no images, fonts, layers, metadata or structure.
Accepted cost: the layout is lost.

The text half is pinned in ``test_resume_export_rebuild_text.py``. Names the
coder must provide in ``src.services.resume_export_service`` (besides the
existing ``redact_pdf``, ``NotAnonymizedError``, ``collect_terms``, ...)::

    REDACTION_MARKER = "[redacted]"
    PDF_PRODUCER: str   # the fixed generic /Producer of every rebuilt PDF
    PDF_CREATOR: str    # the fixed generic /Creator
    PDF_PAGE_SIZE: tuple[float, float] = (612.0, 792.0)   # US Letter, points
    render_text_pdf(text: str) -> bytes
        # a fresh PDF: ONE built-in font (BaseFont Helvetica, not embedded),
        # fixed page size, no rotation, long lines wrapped, pages added as
        # needed, text in the given order; no images, annotations, links,
        # embedded files, outline, OCGs, XMP, /ActualText, Form XObjects, path
        # drawing or marked content; /Info holds ONLY Producer and Creator.

``redact_pdf(pdf, terms)`` is: extract text (reading order) -> ``redact_text``
-> ``verify_text`` on the result -> ``render_text_pdf`` -> extract the rendered
PDF's text -> ``verify_text`` again -> return exactly the rendered bytes. The
module-level names are looked up at call time so tests can substitute them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import fitz  # type: ignore[import-untyped]
import pytest

NAME = "Jane Doe"
BODY = "Experienced analyst with SQL skills."
IDENTITY = ("jane", "doe")

_FORBIDDEN_KEYS = (
    "/Annots",
    "/ActualText",
    "/OCProperties",
    "/OCGs",
    "/Metadata",
    "/Outlines",
    "/Names",
    "/EmbeddedFile",
    "/AcroForm",
    "/StructTreeRoot",
    "/OpenAction",
    "/URI",
    "/JavaScript",
    "/Thumb",
    "/PieceInfo",
)
_FORBIDDEN_OPS = frozenset(
    {
        "re",
        "m",
        "l",
        "c",
        "v",
        "y",
        "h",
        "f",
        "F",
        "f*",
        "B",
        "B*",
        "b",
        "b*",
        "S",
        "s",
        "n",
        "W",
        "W*",
        "sh",
        "Do",
        "BI",
        "ID",
        "EI",
        "BDC",
        "BMC",
        "EMC",
        "DP",
        "MP",
    }
)


# ------------------------------------------------------------------- helpers


def _terms(name: str = NAME) -> list[str]:
    from src.services.resume_export_service import collect_terms

    terms = collect_terms(name, None)
    assert terms is not None
    return terms


def _finish(doc: Any) -> bytes:
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _plain_pdf(lines: list[str]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(lines):
        page.insert_text((72, 72 + i * 16), line, fontsize=11)
    return _finish(doc)


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return " ".join(str(p.get_text()) for p in doc)
    finally:
        doc.close()


def _flat(pdf: bytes) -> str:
    return re.sub(r"\s+", " ", _text(pdf)).casefold()


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


def _pix(side: int = 16) -> Any:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, side, side), False)
    pix.clear_with(180)
    return pix


def _append(doc: Any, page: Any, data: bytes) -> None:
    """Add a content stream to the page, after the existing ones."""
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, data, new=True)
    kind, val = doc.xref_get_key(page.xref, "Contents")
    if kind == "xref":
        doc.xref_set_key(page.xref, "Contents", f"[{val} {xref} 0 R]")
    else:
        doc.xref_set_key(page.xref, "Contents", f"[{val[1:-1]} {xref} 0 R]")


_STRING_RE = re.compile(rb"\(((?:\\.|[^\\()])*)\)|<([0-9A-Fa-f\s]+)>", re.S)
_OCTAL_RE = re.compile(rb"\\([0-7]{1,3})")
_ESCAPE_RE = re.compile(rb"\\(.)", re.S)


def _unescape(literal: bytes) -> bytes:
    literal = _OCTAL_RE.sub(lambda m: bytes([int(m.group(1), 8) & 255]), literal)
    return _ESCAPE_RE.sub(rb"\1", literal)


def _decoded(raw: bytes) -> list[str]:
    """One byte string read as latin-1 / PDFDocEncoding and as UTF-16."""
    body = raw[2:] if raw[:2] == b"\xfe\xff" else raw
    forms = [
        raw.decode("latin-1"),
        body.decode("utf-16-be", "ignore"),
        raw.decode("utf-16-le", "ignore"),
    ]
    return [f.casefold() for f in forms]


def _forms(blob: bytes) -> list[str]:
    """Every reading of a blob: as is, plus each PDF string in it decoded as
    PDFDocEncoding, UTF-16 and hex."""
    forms = _decoded(blob)
    for literal, hexed in _STRING_RE.findall(blob):
        if hexed:
            digits = re.sub(rb"\s", b"", hexed)
            if len(digits) % 2:
                digits += b"0"
            forms.extend(_decoded(bytes.fromhex(digits.decode())))
        else:
            forms.extend(_decoded(_unescape(literal)))
    return forms


def _blobs(pdf: bytes) -> list[bytes]:
    """Every object's text and every DECOMPRESSED stream, trailer included."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    blobs: list[bytes] = []
    try:
        for xref in [*range(1, doc.xref_length()), -1]:
            try:
                blobs.append(str(doc.xref_object(xref)).encode("latin-1", "ignore"))
                if xref > 0 and doc.xref_is_stream(xref):
                    blobs.append(doc.xref_stream(xref) or b"")
            except Exception:  # noqa: BLE001 - an unreadable object is skipped
                continue
    finally:
        doc.close()
    return blobs


def _has(pdf: bytes, pattern: bytes) -> bool:
    return any(re.search(pattern, blob) for blob in _blobs(pdf))


def _assert_no_identity(pdf: bytes, tokens: tuple[str, ...] = IDENTITY) -> None:
    for blob in _blobs(pdf):
        for form in _forms(blob):
            for token in tokens:
                assert token not in form, token


def _operators(page: Any) -> set[str]:
    data = bytes(page.read_contents())
    data = re.sub(rb"\((?:\\.|[^\\()])*\)", b" ", data, flags=re.S)
    data = re.sub(rb"<[0-9A-Fa-f\s]*>", b" ", data)
    data = re.sub(rb"/[^\s/\[\]<>()]+", b" ", data)
    return {t.decode() for t in re.findall(rb"[A-Za-z][A-Za-z*]*|['\"]", data)}


def _assert_fresh(pdf: bytes) -> None:
    """The output carries only text the anonymizer wrote."""
    from src.services import resume_export_service as svc

    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        assert doc.is_pdf
        assert doc.page_count >= 1
        assert doc.embfile_count() == 0
        assert doc.get_toc() == []
        assert not doc.get_ocgs()
        assert set(doc.xref_get_keys(doc.pdf_catalog())) <= {"Type", "Pages"}
        for page in doc:
            assert page.rect == fitz.Rect(0, 0, *svc.PDF_PAGE_SIZE)
            assert page.rotation == 0
            assert page.get_links() == []
            assert list(page.annots() or []) == []
            assert page.get_images(full=True) == []
            assert page.get_image_info() == []
            assert page.get_drawings() == []
            assert _operators(page).isdisjoint(_FORBIDDEN_OPS)
        fonts = {f[3] for n in range(doc.page_count) for f in doc.get_page_fonts(n)}
        assert fonts == {"Helvetica"}
        for xref in range(1, doc.xref_length()):
            obj = str(doc.xref_object(xref))
            for key in _FORBIDDEN_KEYS:
                assert key not in obj, (xref, key)
            assert not re.search(r"/Subtype\s*/(?:Form|Image)", obj), xref
            assert not re.search(r"/XObject\b", obj), xref
        meta = doc.metadata
        for key in ("title", "author", "subject", "keywords"):
            assert not meta[key], key
        assert not meta["creationDate"]
        assert not meta["modDate"]
        assert meta["producer"] == svc.PDF_PRODUCER
        assert meta["creator"] == svc.PDF_CREATOR
        kind, value = doc.xref_get_key(-1, "Info")
        if kind == "xref":
            info = int(value.split()[0])
            assert set(doc.xref_get_keys(info)) <= {"Producer", "Creator"}
    finally:
        doc.close()


def _base(extra: Callable[[Any, Any], None]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 700), f"Sincerely, {NAME}", fontsize=11)
    page.insert_text((72, 100), BODY, fontsize=11)
    extra(doc, page)
    return _finish(doc)


def _email_only_base(extra: Callable[[Any, Any], None]) -> bytes:
    """The only other place the name occurs is an e-mail address."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), BODY, fontsize=11)
    page.insert_text((72, 130), "Contact: jane.doe@example.com", fontsize=11)
    extra(doc, page)
    return _finish(doc)


# -------------------------------------------------- the auditors' attack PDFs


def _vector_header(_doc: Any, page: Any) -> None:
    """Probe D: the name drawn as vector outlines, not as text."""
    src = fitz.open()
    sp = src.new_page(width=300, height=60)
    sp.insert_text((10, 40), "JANE DOE", fontsize=28)
    svg = sp.get_svg_image(text_as_path=True)
    vdoc = fitz.open(stream=svg.encode(), filetype="svg")
    vpdf = fitz.open("pdf", vdoc.convert_to_pdf())
    page.show_pdf_page(fitz.Rect(72, 150, 372, 210), vpdf, 0)


def _actualtext_over_glyphs(doc: Any, page: Any) -> None:
    """Probe I: glyphs say the name, /ActualText says something else."""
    font = page.get_fonts()[0][4]
    stream = f"/Span <</ActualText (Applicant)>> BDC BT /{font} 28 Tf 72 600 Td"
    _append(doc, page, f"{stream} (JANE DOE) Tj ET EMC".encode())


def _actualtext_holds_the_name(doc: Any, page: Any) -> None:
    """The reverse: the glyphs are harmless, /ActualText carries the name."""
    font = page.get_fonts()[0][4]
    stream = f"/Span <</ActualText (Jane Doe)>> BDC BT /{font} 28 Tf 72 600 Td"
    _append(doc, page, f"{stream} (XXXX) Tj ET EMC".encode())


def _xmp_on_a_form_xobject(doc: Any, page: Any) -> None:
    """Probe K5: compressed-able XMP hung on a Form XObject."""
    src = fitz.open()
    sp = src.new_page(width=100, height=40)
    sp.draw_rect(fitz.Rect(5, 5, 95, 35), color=(0, 0, 1))
    page.show_pdf_page(fitz.Rect(72, 150, 172, 190), src, 0)
    forms = [x for x in range(1, doc.xref_length()) if "/Form" in doc.xref_object(x)]
    meta = doc.get_new_xref()
    doc.update_object(meta, "<</Type/Metadata/Subtype/XML>>")
    xmp = (
        b"<x:xmpmeta><dc:creator>Jane Doe</dc:creator>"
        b"<dc:email>jane.doe@example.com</dc:email></x:xmpmeta>"
    )
    doc.update_stream(meta, xmp, new=True)
    for form in forms:
        doc.xref_set_key(form, "Metadata", f"{meta} 0 R")


def _utf16_ocg_name(doc: Any, page: Any) -> None:
    """Probe K2: an optional-content group whose /Name is UTF-16 hex."""
    ocg = doc.add_ocg("Jane Doe - photo", on=True)
    doc.xref_set_key(ocg, "Name", "<FEFF004A0061006E006500200044006F0065>")
    page.insert_text((72, 320), "Profile", fontsize=11, oc=ocg)


def _text_in_a_form(_doc: Any, page: Any) -> None:
    src = fitz.open()
    sp = src.new_page(width=300, height=60)
    sp.insert_text((10, 40), NAME, fontsize=24)
    page.show_pdf_page(fitz.Rect(72, 150, 372, 210), src, 0)


def _unreferenced_form(doc: Any, page: Any) -> None:
    form = doc.get_new_xref()
    doc.update_object(form, "<</Type/XObject/Subtype/Form/BBox[0 0 10 10]>>")
    doc.update_stream(form, b"BT /F1 12 Tf (Jane Doe 604-555-0142) Tj ET", new=True)
    kind, val = doc.xref_get_key(page.xref, "Resources")
    res = int(val.split()[0]) if kind == "xref" else page.xref
    doc.xref_set_key(res, "XObject", f"<</Fx9 {form} 0 R>>")


def _banner_image(_doc: Any, page: Any) -> None:
    tmp = fitz.open()
    tp = tmp.new_page(width=200, height=40)
    tp.insert_text((5, 30), "JANE DOE", fontsize=24)
    page.insert_image(fitz.Rect(72, 150, 372, 210), pixmap=tp.get_pixmap(dpi=144))


FORM = rb"/Subtype\s*/Form"

ATTACKS: list[tuple[str, Callable[[Any, Any], None], bytes, tuple[str, ...]]] = [
    ("vector-outline-header", _vector_header, FORM, ()),
    ("actualtext-over-glyphs", _actualtext_over_glyphs, rb"ActualText", ()),
    ("actualtext-holds-name", _actualtext_holds_the_name, rb"ActualText", ()),
    ("xmp-on-form", _xmp_on_a_form_xobject, rb"jane\.doe@example", ("example.com",)),
    ("utf16-ocg-name", _utf16_ocg_name, rb"OCProperties", ()),
    ("text-in-form", _text_in_a_form, FORM, ()),
    ("unreferenced-form", _unreferenced_form, FORM, ("604-555",)),
    ("banner-image", _banner_image, rb"/Subtype\s*/Image", ()),
]


@pytest.mark.parametrize(
    ("builder", "channel", "extra"),
    [pytest.param(b, c, e, id=i) for i, b, c, e in ATTACKS],
)
def test_an_attack_pdf_yields_a_fresh_text_only_copy_without_the_name(
    builder: Callable[[Any, Any], None], channel: bytes, extra: tuple[str, ...]
) -> None:
    from src.services.resume_export_service import REDACTION_MARKER, redact_pdf

    src = _base(builder)
    assert _has(src, channel), "fixture lost the attack channel"

    out = redact_pdf(src, _terms())

    _assert_fresh(out)
    _assert_no_identity(out, (*IDENTITY, *extra))
    assert "experienced analyst with sql skills" in _flat(out)
    assert REDACTION_MARKER in _text(out)


def test_the_vector_outline_header_leaves_no_drawing_behind() -> None:
    from src.services.resume_export_service import redact_pdf

    src = _base(_vector_header)
    probe = fitz.open(stream=src, filetype="pdf")
    try:
        assert probe[0].get_drawings(), "fixture drew nothing"
    finally:
        probe.close()

    out = redact_pdf(src, _terms())

    doc = fitz.open(stream=out, filetype="pdf")
    try:
        assert [p.get_drawings() for p in doc] == [[] for _ in doc]
        for page in doc:
            assert _operators(page).isdisjoint(_FORBIDDEN_OPS)
    finally:
        doc.close()


def _kitchen_sink() -> bytes:
    """Every channel at once, on a small, cropped page."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((20, 40), NAME, fontsize=14, fontname="tiro")
    page.insert_text((20, 70), BODY, fontsize=9, fontname="cour")
    page.insert_text((20, 90), "Contact: jane.doe@example.com", fontsize=9)
    page.insert_link(
        {
            "kind": fitz.LINK_URI,
            "from": fitz.Rect(20, 80, 200, 95),
            "uri": "https://www.linkedin.com/in/jane-doe",
        }
    )
    page.add_text_annot(fitz.Point(200, 200), "Jane Doe 604-555-0142")
    page.insert_image(fitz.Rect(220, 20, 260, 60), pixmap=_pix())
    layer = doc.add_ocg("Jane Doe layer", on=True)
    page.insert_text((20, 120), "Layered text", fontsize=9, oc=layer)
    doc.embfile_add("source.txt", b"Jane Doe 604-555-0142", filename="s.txt")
    doc.set_toc([[1, "Jane Doe - Resume", 1]])
    doc.set_metadata(
        {
            "title": "Jane Doe CV",
            "author": "Jane Doe",
            "subject": "Application of Jane Doe",
            "keywords": "jane, doe",
            "creator": "Word for Jane",
            "producer": "Acrobat for Jane",
        }
    )
    doc.set_xml_metadata("<x:xmpmeta>Jane Doe jane.doe@example.com</x:xmpmeta>")
    page.set_cropbox(fitz.Rect(0, 0, 250, 350))
    return _finish(doc)


def test_nothing_from_a_kitchen_sink_original_carries_over() -> None:
    from src.services.resume_export_service import redact_pdf

    out = redact_pdf(_kitchen_sink(), _terms())

    _assert_fresh(out)
    _assert_no_identity(out, (*IDENTITY, "example.com", "604-555", "linkedin"))
    flat = _flat(out)
    assert "experienced analyst with sql skills" in flat
    assert "layered text" in flat


def test_the_output_page_is_the_fixed_size_whatever_the_original_was() -> None:
    from src.services.resume_export_service import PDF_PAGE_SIZE, redact_pdf

    out = redact_pdf(_kitchen_sink(), _terms())

    doc = fitz.open(stream=out, filetype="pdf")
    try:
        assert all(p.rect == fitz.Rect(0, 0, *PDF_PAGE_SIZE) for p in doc)
    finally:
        doc.close()


# --------------------------------------------------------- render_text_pdf


def test_the_generic_producer_and_creator_are_plain_strings() -> None:
    from src.services.resume_export_service import PDF_CREATOR, PDF_PRODUCER

    for value in (PDF_PRODUCER, PDF_CREATOR):
        assert isinstance(value, str)
        assert value.strip()
        assert "pymupdf" not in value.casefold()


def test_render_text_pdf_makes_a_fresh_text_only_pdf() -> None:
    from src.services.resume_export_service import render_text_pdf

    out = render_text_pdf("[redacted]\nAnalyst with SQL skills")

    assert out.startswith(b"%PDF")
    _assert_fresh(out)
    # get_text() ends every line with "\n", so _flat always ends in a space.
    assert _flat(out).strip() == "[redacted] analyst with sql skills"


def test_render_text_pdf_wraps_long_lines_and_loses_no_word() -> None:
    from src.services.resume_export_service import render_text_pdf

    words = [f"w{n:03d}" for n in range(1, 401)]

    doc = fitz.open(stream=render_text_pdf(" ".join(words)), filetype="pdf")
    try:
        seen: list[str] = []
        for page in doc:
            for w in page.get_text("words"):
                assert page.rect.contains(fitz.Rect(w[:4])), w[4]
                seen.append(w[4])
    finally:
        doc.close()
    assert seen == words


def test_render_text_pdf_paginates_in_order() -> None:
    from src.services.resume_export_service import render_text_pdf

    lines = [f"line {n:03d} ordinary text" for n in range(1, 201)]

    doc = fitz.open(stream=render_text_pdf("\n".join(lines)), filetype="pdf")
    try:
        assert doc.page_count >= 2
        seen = [w[4] for page in doc for w in page.get_text("words")]
    finally:
        doc.close()
    assert seen == [w for line in lines for w in line.split()]


def test_render_text_pdf_survives_bullets_dashes_and_accents() -> None:
    from src.services.resume_export_service import render_text_pdf

    out = render_text_pdf("• Built dashboards – 2018–2021 café")

    flat = _flat(out)
    assert "built dashboards" in flat
    assert "2018" in flat
    assert "caf" in flat


# ------------------------------------------------- the pipeline's behaviour


def test_the_copy_is_a_pdf_with_marker_and_ordinary_text() -> None:
    from src.services.resume_export_service import REDACTION_MARKER, redact_pdf

    src = _plain_pdf(
        [
            "Ann Ford",
            "Planned annual budgets at Stanford",
            "Analyst with SQL skills",
        ]
    )

    out = redact_pdf(src, _terms("Ann Ford"))

    assert out.startswith(b"%PDF")
    flat = _flat(out)
    assert "planned annual budgets at stanford" in flat
    assert "analyst with sql skills" in flat
    assert "ann ford" not in flat
    assert REDACTION_MARKER in _text(out)


def test_the_output_keeps_reading_order_not_drawing_order() -> None:
    from src.services.resume_export_service import REDACTION_MARKER, redact_pdf

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 300), "Third line Python", fontsize=11)
    page.insert_text((72, 72), "First line SQL", fontsize=11)
    page.insert_text((72, 150), f"Second {NAME} line", fontsize=11)

    flat = _flat(redact_pdf(_finish(doc), _terms()))

    first = flat.index("first line sql")
    middle = flat.index(REDACTION_MARKER)
    last = flat.index("third line python")
    assert first < middle < last


def test_a_long_original_keeps_every_ordinary_word() -> None:
    from src.services.resume_export_service import REDACTION_MARKER, redact_pdf

    doc = fitz.open()
    for _ in range(2):
        page = doc.new_page()
        for i in range(40):
            page.insert_text((72, 40 + i * 16), f"w{i:03d} ordinary words", fontsize=11)
    doc.new_page().insert_text((72, 72), f"{NAME} at the end", fontsize=11)

    out = redact_pdf(_finish(doc), _terms())

    seen = _flat(out).split()
    assert seen.count("ordinary") == 80
    assert seen.count("w039") == 2
    assert REDACTION_MARKER in _flat(out)


def test_personal_web_and_postal_data_in_a_pdf_is_gone() -> None:
    from src.services.resume_export_service import redact_pdf

    def site(_doc: Any, page: Any) -> None:
        page.insert_text(
            (72, 160),
            "Portfolio: janedoe.ca   X: @janedoe   gitlab.com/jdoe88",
            fontsize=11,
        )
        page.insert_text(
            (72, 180), "4521 Kingsway Ave, Burnaby BC V5H 2A9", fontsize=11
        )

    out = redact_pdf(_base(site), _terms())

    flat = _flat(out)
    for leak in ("janedoe", "gitlab", "jdoe88", "v5h", "2a9"):
        assert leak not in flat, leak
    assert "experienced analyst with sql skills" in flat


def test_a_second_script_in_the_pdf_refuses_the_file() -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    def cjk(_doc: Any, page: Any) -> None:
        page.insert_text((72, 160), "Jane Doe (杜简)  杭州", fontname="china-s")

    src = _base(cjk)
    assert "杜" in _text(src), "fixture lost the second script"

    with pytest.raises(NotAnonymizedError):
        redact_pdf(src, _terms())


# ----------------------------------------------------- letter-spaced headers


def _spaced_header(_doc: Any, page: Any) -> None:
    page.insert_text((72, 60), "J A N E   D O E", fontsize=20)


def _char_by_char_header(_doc: Any, page: Any) -> None:
    for i, ch in enumerate("JANEDOE"):
        x = 72 + i * 26 + (26 if i >= 4 else 0)
        page.insert_text((x, 60), ch, fontsize=20)


def _tj_kerned_header(doc: Any, page: Any) -> None:
    font = page.get_fonts()[0][4]
    kern = "[(J) -900 (A) -900 (N) -900 (E) -1800 (D) -900 (O) -900 (E)] TJ"
    _append(doc, page, f"BT /{font} 20 Tf 72 740 Td {kern} ET".encode())


@pytest.mark.parametrize(
    "header",
    [_spaced_header, _char_by_char_header, _tj_kerned_header],
    ids=["spaced", "char-by-char", "tj-kerned"],
)
def test_a_letter_spaced_header_is_dropped_or_the_file_is_refused(
    header: Callable[[Any, Any], None],
) -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    src = _email_only_base(header)
    try:
        out = redact_pdf(src, _terms())
    except NotAnonymizedError:
        return  # refusing is an acceptable answer; shipping the name is not

    squashed = _squash(_text(out))
    assert "jane" not in squashed
    assert "doe" not in squashed
    _assert_no_identity(out)


# --------------------------------------------------- verification runs twice


def test_the_redacted_text_is_checked_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import resume_export_service as svc

    rendered: list[str] = []

    def unredacted(text: str, *_a: Any, **_k: Any) -> str:
        return text

    def spy(text: str, *_a: Any, **_k: Any) -> bytes:
        rendered.append(text)
        return b""

    monkeypatch.setattr(svc, "redact_text", unredacted)
    monkeypatch.setattr(svc, "render_text_pdf", spy)

    with pytest.raises(svc.NotAnonymizedError):
        svc.redact_pdf(_plain_pdf([NAME, BODY]), _terms())

    assert rendered == [], "an unverified text must never reach the renderer"


@pytest.mark.parametrize(
    "leak",
    [
        "Jane",
        "jane.doe@example.com",
        "604-555-0142",
        "janedoe.ca",
        "@recruit_me",
        "J A N E D O E",
    ],
)
def test_the_rebuilt_pdfs_own_text_is_checked_after_rendering(
    monkeypatch: pytest.MonkeyPatch, leak: str
) -> None:
    from src.services import resume_export_service as svc

    leaky = _plain_pdf([BODY, leak])

    def render(*_a: Any, **_k: Any) -> bytes:
        return leaky

    monkeypatch.setattr(svc, "render_text_pdf", render)

    with pytest.raises(svc.NotAnonymizedError):
        svc.redact_pdf(_plain_pdf([NAME, BODY]), _terms())


def test_the_bytes_that_were_verified_are_the_bytes_that_ship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import resume_export_service as svc

    clean = _plain_pdf([BODY])

    def render(*_a: Any, **_k: Any) -> bytes:
        return clean

    monkeypatch.setattr(svc, "render_text_pdf", render)

    assert svc.redact_pdf(_plain_pdf([NAME, BODY]), _terms()) == clean


def test_an_unreadable_render_is_not_anonymized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import resume_export_service as svc

    def render(*_a: Any, **_k: Any) -> bytes:
        return b"not a pdf"

    monkeypatch.setattr(svc, "render_text_pdf", render)

    with pytest.raises(svc.NotAnonymizedError):
        svc.redact_pdf(_plain_pdf([NAME, BODY]), _terms())


def test_the_redacted_text_goes_to_the_renderer_with_the_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import resume_export_service as svc

    seen: list[str] = []
    real = svc.render_text_pdf

    def spy(text: str, *a: Any, **k: Any) -> bytes:
        seen.append(text)
        return bytes(real(text, *a, **k))

    monkeypatch.setattr(svc, "render_text_pdf", spy)

    svc.redact_pdf(_plain_pdf([f"Prepared by {NAME}", BODY]), _terms())

    assert len(seen) == 1
    assert svc.REDACTION_MARKER in seen[0]
    assert not re.search(r"(?i)\b(jane|doe)\b", seen[0])
    assert BODY in seen[0].split("\n")
