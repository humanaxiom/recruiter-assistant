"""Export a job's résumés and cover letters as a ZIP: as-is and anonymized.

Pure, synchronous and DB-free (the worker runs it in a thread and owns every
read and write). The anonymized copy is made in four steps, and the last one
does not trust the first three:

1. **Strip.** Every channel that can carry identity without being page text is
   removed from the document itself: links and every other annotation, form
   fields, embedded files, the outline, the metadata, the CropBox (so text
   outside it is no longer "outside the page"), and every image. An image is
   pixels no text pass can verify, so a page that is mostly image is refused.
2. **Vouch.** The candidate's full name, or failing that the surname, must be
   findable in the ORIGINAL text. A verification that only checks the term list
   cannot notice a name the list never held.
3. **Redact.** Terms are located with PyMuPDF search, accepted only where they
   are WHOLE words (so ``Ann Ford`` never eats ``Stanford``), and removed with
   redaction annotations that are applied -- the text leaves the content stream,
   it is not merely covered. E-mail addresses, phone numbers and profile URLs
   are redacted by shape.
4. **Verify the shipped bytes.** The output is re-opened and checked on its own:
   nothing left of any channel from step 1, no term, e-mail, phone or profile
   URL in the unclipped text. Anything that cannot be vouched for raises
   :class:`NotAnonymizedError` -- fail closed, never ship an unverifiable file.

All matching is on normalized text (:func:`normalize_text`): NFKC, case-folded,
accent-stripped, with apostrophe and hyphen variants unified.

Entry names and the manifest carry only ``applicant-NNN`` numbers -- never a
name, an address or an uploaded filename.
"""

from __future__ import annotations

import bisect
import functools
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from typing import Any

import fitz  # type: ignore[import-untyped]  # PyMuPDF ships no stubs

from src.settings import get_settings

#: A page whose images cover more than this fraction of its area is refused: a
#: scan or an image résumé keeps the identity in pixels no text pass can verify.
MAX_IMAGE_AREA_FRACTION = 0.5

_MIN_TOKEN_LEN = 2
_MIN_PHONE_DIGITS_BARE = 10
_MIN_PHONE_DIGITS_SEPARATED = 7
_MIN_RAW_NEEDLE_LEN = 6

_BIG = fitz.Rect(-20000, -20000, 20000, 20000)
# No MediaBox clipping: the verification must see text that sits outside the page.
_UNCLIPPED = fitz.TEXT_PRESERVE_WHITESPACE

_APOSTROPHES = frozenset("‘’‛ʼ´`′")
_DASHES = frozenset("‐‑‒–—―−")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# U+00B7 (middle dot) is what PyMuPDF writes for a dash its base-14 font lacks.
_PHONE_RE = re.compile(r"(?<![\w])\+?\(?\d[\d()./ \-·]{5,}\d(?![\w])")
_URL_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)*"
    r"(?:linkedin\.com/(?:in|pub|company|profile)|github\.com)/[^\s]*"
)
_YEAR_RANGE_RE = re.compile(r"(?:19|20)\d{2}\s*[-/ ]\s*(?:19|20)\d{2}")
_DATE_RE = re.compile(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}")
_SEPARATORS = frozenset(" -./()·")

_CATALOG_KEEP = frozenset({"Type", "Pages"})
_PAGE_STRIP_KEYS = (
    "Annots",
    "AA",
    "Metadata",
    "PieceInfo",
    "Thumb",
    "B",
    "StructParents",
    "LastModified",
)
_CROP_KEYS = ("CropBox", "TrimBox", "BleedBox", "ArtBox")


class NotAnonymizedError(Exception):
    """A redacted copy could not be vouched for."""


# ---------------------------------------------------------------- normalization


@functools.lru_cache(maxsize=8192)
def _norm_char(ch: str) -> str:
    if ch in _APOSTROPHES:
        return "'"
    if ch in _DASHES:
        return "-"
    folded = unicodedata.normalize("NFKC", ch).casefold()
    out: list[str] = []
    for c in unicodedata.normalize("NFD", folded):
        if unicodedata.combining(c) or unicodedata.category(c) == "Cf":
            continue
        if c in _APOSTROPHES:
            c = "'"
        elif c in _DASHES:
            c = "-"
        elif c.isspace():
            c = " "
        out.append(c)
    return "".join(out)


def _norm_map(text: str) -> tuple[str, list[int]]:
    """Normalized text plus, for every normalized character, the index of the
    original character it came from. Whitespace is collapsed and stripped."""
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(text):
        for c in _norm_char(ch):
            if c == " " and (not chars or chars[-1] == " "):
                continue
            chars.append(c)
            origin.append(i)
    while chars and chars[-1] == " ":
        chars.pop()
        origin.pop()
    return "".join(chars), origin


def normalize_text(text: str) -> str:
    """NFKC, casefold, accent-strip, unify apostrophe and hyphen variants,
    collapse whitespace and strip."""
    return _norm_map(text)[0]


def _term_re(norm_term: str) -> re.Pattern[str]:
    """A normalized term as a WHOLE-word pattern (letters and digits delimit)."""
    return re.compile(r"(?<![^\W_])" + re.escape(norm_term) + r"(?![^\W_])")


def _phones(norm_line: str) -> list[re.Match[str]]:
    found: list[re.Match[str]] = []
    for m in _PHONE_RE.finditer(norm_line):
        span = m.group(0).strip()
        digits = sum(c.isdigit() for c in span)
        separated = any(c in _SEPARATORS for c in span)
        if _YEAR_RANGE_RE.fullmatch(span) or _DATE_RE.fullmatch(span):
            continue
        bare = digits >= _MIN_PHONE_DIGITS_BARE
        local = separated and digits >= _MIN_PHONE_DIGITS_SEPARATED
        if bare or local:
            found.append(m)
    return found


def _shape_spans(norm_line: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in _EMAIL_RE.finditer(norm_line)]
    spans.extend(m.span() for m in _URL_RE.finditer(norm_line))
    spans.extend(m.span() for m in _phones(norm_line))
    return spans


def _has_shape(norm_line: str) -> bool:
    return bool(
        _EMAIL_RE.search(norm_line) or _URL_RE.search(norm_line) or _phones(norm_line)
    )


# ------------------------------------------------------------------- the PDF


def _raw_text(page: Any) -> str:
    return str(page.get_text("text", clip=_BIG, flags=_UNCLIPPED))


def _shrink(rect: Any) -> Any:
    """A hair smaller than the glyph box, so a redaction never clips the glyphs
    of a neighbouring word or line (any glyph the rect touches is removed)."""
    r = fitz.Rect(rect)
    dy = r.height * 0.15
    dx = 0.3 if r.width > 1.0 else 0.0
    return fitz.Rect(r.x0 + dx, r.y0 + dy, r.x1 - dx, r.y1 - dy)


def _drop_keys(doc: Any, xref: int, keys: tuple[str, ...]) -> None:
    present = set(doc.xref_get_keys(xref))
    for key in keys:
        if key in present:
            doc.xref_set_key(xref, key, "null")


def _strip_channels(doc: Any) -> None:
    """Remove every non-text identity channel from the document structure."""
    catalog = doc.pdf_catalog()
    for key in doc.xref_get_keys(catalog):
        if key not in _CATALOG_KEEP:
            doc.xref_set_key(catalog, key, "null")
    for pno in range(doc.page_count):
        xref = doc.page_xref(pno)
        _drop_keys(doc, xref, _PAGE_STRIP_KEYS)
        # The boxes may be inherited from the page tree, so walk up.
        node, hops = xref, 0
        while node and hops < 32:
            _drop_keys(doc, node, _CROP_KEYS)
            kind, value = doc.xref_get_key(node, "Parent")
            node = int(value.split()[0]) if kind == "xref" else 0
            hops += 1
    doc.set_metadata({})
    doc.del_xml_metadata()


def _open_stripped(pdf_bytes: bytes) -> Any:
    """A fresh document (re-opened, so nothing stale is cached) with every
    non-text channel stripped. Raises for unreadable or encrypted input."""
    try:
        first = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise NotAnonymizedError("file is not a readable PDF") from exc
    try:
        if first.needs_pass or first.is_encrypted:
            raise NotAnonymizedError("encrypted PDF")
        if not first.is_pdf or first.page_count == 0:
            raise NotAnonymizedError("file is not a readable PDF")
        _strip_channels(first)
        stripped: bytes = first.tobytes(garbage=3)
    except NotAnonymizedError:
        raise
    except Exception as exc:
        raise NotAnonymizedError("could not strip the document") from exc
    finally:
        first.close()
    try:
        return fitz.open(stream=stripped, filetype="pdf")
    except Exception as exc:
        raise NotAnonymizedError("stripped document unreadable") from exc


def _image_boxes(page: Any) -> list[Any]:
    boxes: list[Any] = []
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"])
        r.intersect(page.rect)
        if not r.is_empty and r.get_area() > 0:
            boxes.append(r)
    return boxes


def _refuse_image_heavy(doc: Any) -> None:
    for page in doc:
        page_area = page.rect.get_area()
        if page_area <= 0:
            continue
        covered = sum(r.get_area() for r in _image_boxes(page))
        if covered / page_area > MAX_IMAGE_AREA_FRACTION:
            raise NotAnonymizedError("page is mostly image; identity is in pixels")


def _name_forms(full_name: str) -> tuple[str, str]:
    """``(normalized full name, normalized surname)`` -- the surname being the
    last whitespace token of the full name."""
    full = normalize_text(full_name)
    return full, full.rsplit(" ", 1)[-1]


def _require_name_found(norm_raw_text: str, full: str, surname: str) -> None:
    if not full:
        raise NotAnonymizedError("no candidate name to vouch for")
    for name in (full, surname):
        if _term_re(name).search(norm_raw_text):
            return
    raise NotAnonymizedError("the candidate's name is not in the document text")


def _search_variants(original: str) -> list[str]:
    """Spellings of one matched span that MuPDF search may find: as extracted,
    NFKC (ligatures expanded), accent-stripped, and with typographic hyphens
    and apostrophes made plain."""
    collapsed = " ".join(original.split())
    nfkc = unicodedata.normalize("NFKC", collapsed)
    plain = "".join(
        c for c in unicodedata.normalize("NFD", nfkc) if not unicodedata.combining(c)
    )
    tidy = "".join(
        "'" if c in _APOSTROPHES else "-" if c in _DASHES else c for c in plain
    )
    return list(dict.fromkeys(v for v in (collapsed, nfkc, plain, tidy) if v))


@dataclass
class _Flow:
    """A page's words as one normalized string, so a term can be matched across
    word and line breaks and mapped back to the words it covers."""

    rects: list[Any]
    raws: list[str]
    origins: list[list[int]]
    starts: list[int]
    text: str

    def word_at(self, pos: int) -> int:
        return bisect.bisect_right(self.starts, pos) - 1

    def original(self, begin: int, end: int) -> str:
        """The original characters behind normalized ``[begin, end)``."""
        first, last = self.word_at(begin), self.word_at(end - 1)
        o_first = self.origins[first][begin - self.starts[first]]
        o_last = self.origins[last][end - 1 - self.starts[last]]
        if first == last:
            return self.raws[first][o_first : o_last + 1]
        parts = [self.raws[first][o_first:], *self.raws[first + 1 : last]]
        parts.append(self.raws[last][: o_last + 1])
        return " ".join(parts)


def _build_flow(words: list[Any]) -> _Flow:
    rects: list[Any] = []
    raws: list[str] = []
    origins: list[list[int]] = []
    starts: list[int] = []
    pieces: list[str] = []
    pos = 0
    for w in words:
        raw = str(w[4])
        norm, origin = _norm_map(raw)
        if not norm:
            continue
        rects.append(fitz.Rect(w[0], w[1], w[2], w[3]))
        raws.append(raw)
        origins.append(origin)
        starts.append(pos)
        pieces.append(norm)
        pos += len(norm) + 1
    return _Flow(rects, raws, origins, starts, " ".join(pieces))


def _word_span(flow: _Flow, begin: int, end: int) -> range:
    return range(flow.word_at(begin), flow.word_at(end - 1) + 1)


def _redact_terms(page: Any, flow: _Flow, patterns: list[re.Pattern[str]]) -> list[Any]:
    """Rects to redact for every whole-word term match that PyMuPDF search can
    locate. A match search cannot locate is left alone -- the verification then
    refuses the file, which is the point."""
    out: list[Any] = []
    for pattern in patterns:
        for m in pattern.finditer(flow.text):
            covered = _word_span(flow, m.start(), m.end())
            target = fitz.Rect(flow.rects[covered[0]])
            for i in covered:
                target |= flow.rects[i]
            window = fitz.Rect(target.x0 - 1, target.y0, target.x1 + 1, target.y1)
            for variant in _search_variants(flow.original(m.start(), m.end())):
                hits = [
                    fitz.Rect(h)
                    for h in page.search_for(variant)
                    if fitz.Rect(h).intersects(window)
                ]
                if hits:
                    out.extend(hits)
                    out.extend(flow.rects[i] for i in covered)
                    break
    return out


def _redact_shapes(words: list[Any]) -> list[Any]:
    """Word rects covering every e-mail, phone number and profile URL."""
    lines: dict[tuple[int, int], list[Any]] = {}
    for w in words:
        lines.setdefault((int(w[5]), int(w[6])), []).append(w)
    out: list[Any] = []
    for line_words in lines.values():
        flow = _build_flow(line_words)
        for begin, end in _shape_spans(flow.text):
            for i in _word_span(flow, begin, end):
                out.append(flow.rects[i])
    return out


def _redact_page(page: Any, patterns: list[re.Pattern[str]]) -> None:
    words = page.get_text("words")
    flow = _build_flow(words)
    rects = _redact_terms(page, flow, patterns)
    rects.extend(_redact_shapes(words))
    for rect in rects:
        page.add_redact_annot(_shrink(rect), fill=(0, 0, 0))
    for box in _image_boxes(page):
        page.add_redact_annot(box, fill=(1, 1, 1))
    if page.first_annot is not None:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE)
    page.clean_contents()


def _prune_images(doc: Any) -> None:
    """Drop any image still listed in a page's resources (redaction removed the
    drawing operators; this removes the objects so nothing is carried)."""
    for pno in range(doc.page_count):
        xref = doc.page_xref(pno)
        for image in doc.get_page_images(pno, full=True):
            name = image[7]
            try:
                doc.xref_set_key(xref, f"Resources/XObject/{name}", "null")
            except Exception:  # noqa: BLE001 - verification below is the gate
                continue


def _survives(raw_text: str, patterns: list[re.Pattern[str]]) -> bool:
    flat = normalize_text(raw_text)
    if any(p.search(flat) for p in patterns):
        return True
    for line in raw_text.splitlines():
        if _has_shape(normalize_text(line)):
            return True
    return False


def _verify(
    out: bytes,
    page_count: int,
    patterns: list[re.Pattern[str]],
    raw_needles: list[bytes],
) -> None:
    """Check the bytes that will actually ship, on their own terms."""
    try:
        check = fitz.open(stream=out, filetype="pdf")
    except Exception as exc:
        raise NotAnonymizedError("redacted output unreadable") from exc
    try:
        if check.page_count != page_count:
            raise NotAnonymizedError("page count changed")
        if check.embfile_count() != 0 or check.get_toc():
            raise NotAnonymizedError("embedded files or outline survived")
        for page in check:
            if page.get_links() or list(page.annots() or []):
                raise NotAnonymizedError("links or annotations survived")
            if page.get_images(full=True):
                raise NotAnonymizedError("images survived")
            if _survives(_raw_text(page), patterns):
                raise NotAnonymizedError("identifying text survived redaction")
    except NotAnonymizedError:
        raise
    except Exception as exc:
        raise NotAnonymizedError("redacted output could not be verified") from exc
    finally:
        check.close()
    lowered = out.lower()
    if any(needle in lowered for needle in raw_needles):
        raise NotAnonymizedError("identifying text survived in the file bytes")


def _compile_terms(terms: list[str]) -> tuple[list[re.Pattern[str]], str, str]:
    """Whole-word patterns for the terms AND for the name's own tokens, so the
    final check does not depend on the caller having listed the tokens."""
    if not terms or not terms[0].strip():
        raise NotAnonymizedError("no candidate name to vouch for")
    full, surname = _name_forms(terms[0])
    wanted: dict[str, None] = {}
    for term in terms:
        norm = normalize_text(term)
        if norm:
            wanted[norm] = None
    for token in re.split(r"[\s,.]+", full):
        if len(token) >= _MIN_TOKEN_LEN:
            wanted.setdefault(token, None)
    return [_term_re(t) for t in wanted], full, surname


def redact_pdf(pdf_bytes: bytes, terms: list[str]) -> bytes:
    """Return a stripped, redacted, independently verified copy of the PDF.

    ``terms[0]`` is the candidate's FULL name (the :func:`collect_terms`
    convention). Raises :class:`NotAnonymizedError` when the input is not a
    readable, unencrypted PDF with a text layer, is mostly image, never shows
    the candidate's name, or anything identifying could survive.
    """
    patterns, full, surname = _compile_terms(terms)
    doc = _open_stripped(pdf_bytes)
    try:
        _refuse_image_heavy(doc)
        raw = [_raw_text(page) for page in doc]
        original = normalize_text("\n".join(raw))
        if not original:
            raise NotAnonymizedError("no text layer to redact or verify")
        _require_name_found(original, full, surname)
        page_count = doc.page_count
        for page in doc:
            _redact_page(page, patterns)
        _prune_images(doc)
        doc.set_metadata({})
        doc.del_xml_metadata()
        out: bytes = doc.tobytes(garbage=4, deflate=True)
    except NotAnonymizedError:
        raise
    except Exception as exc:
        raise NotAnonymizedError("redaction failed") from exc
    finally:
        doc.close()
    needles = [
        n.encode("utf-8")
        for n in {full, *(normalize_text(t) for t in terms if "@" in t)}
        if len(n) >= _MIN_RAW_NEEDLE_LEN and n.isascii()
    ]
    _verify(out, page_count, patterns, needles)
    return out


# ---------------------------------------------------------------- the term list


def _clean(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def collect_terms(
    candidate_name: str | None,
    parsed: dict[str, Any] | None,
    *,
    email: str | None = None,
    phone: str | None = None,
) -> list[str] | None:
    """Strings to redact for one applicant, or ``None`` when the résumé has no
    parsed name (so nothing can be anonymized reliably).

    ``terms[0]`` is the full name; then each name token (two letters count),
    the stored e-mail and phone, the parsed location, and every employer and
    school -- the same inputs ``redaction.blind_label_map`` uses."""
    name = (candidate_name or "").strip()
    if not name:
        return None
    raw: list[str] = [name]
    raw.extend(tok for tok in re.split(r"[\s,.]+", name) if len(tok) >= _MIN_TOKEN_LEN)
    body = parsed or {}
    candidate = body.get("candidate")
    held = candidate if isinstance(candidate, dict) else {}
    for value in (email, phone, held.get("email"), held.get("phone")):
        if (text := _clean(value)) is not None:
            raw.append(text)
    if (location := _clean(held.get("location"))) is not None:
        raw.append(location)
    for key, field in (("experience", "company"), ("education", "institution")):
        items = body.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and (text := _clean(item.get(field))):
                    raw.append(text)
    seen: set[str] = set()
    terms: list[str] = []
    for t in raw:
        if t.casefold() not in seen:
            seen.add(t.casefold())
            terms.append(t)
    return terms


# ---------------------------------------------------------------------- the ZIP


@dataclass(frozen=True)
class ExportApplicant:
    resume_pdf: bytes | None  # None == the stored file is missing
    cover_pdf: bytes | None
    terms: list[str] | None  # None == unparsed: cannot be anonymized
    cover_missing: bool = False  # a cover letter exists but its file is gone


@dataclass(frozen=True)
class ExportResult:
    data: bytes
    applicants: int
    anonymized: int
    not_anonymized: int


def _page_count(pdf: bytes) -> int | None:
    try:
        doc = fitz.open(stream=pdf, filetype="pdf")
    except Exception:  # noqa: BLE001 - classification only
        return None
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def _try_redact(
    pdf: bytes | None, terms: list[str] | None, max_pages: int
) -> bytes | None:
    if pdf is None or terms is None:
        return None
    pages = _page_count(pdf)
    if pages is None or pages > max_pages:
        return None
    try:
        return redact_pdf(pdf, terms)
    except NotAnonymizedError:
        return None


_WARNING = (
    "WARNING: do not forward the as-is folder to blind reviewers -- it "
    "identifies every candidate. Send them the anonymized folder only."
)


def build_export(
    applicants: list[ExportApplicant],
    *,
    blind_review: bool,
    max_pages_per_file: int | None = None,
) -> ExportResult:
    cap = (
        max_pages_per_file
        if max_pages_per_file is not None
        else get_settings().resume_export_max_pages_per_file
    )
    entries: list[tuple[str, bytes]] = []
    lines: list[str] = []
    anonymized = 0
    for n, a in enumerate(applicants, start=1):
        label = f"applicant-{n:03d}"
        resume_anon = _try_redact(a.resume_pdf, a.terms, cap)
        cover_anon = _try_redact(a.cover_pdf, a.terms, cap)
        cover_failed = a.cover_pdf is not None and cover_anon is None
        full = resume_anon is not None and not cover_failed
        if blind_review and not full:
            reason = "file missing" if a.resume_pdf is None else "not anonymized"
            lines.append(f"{label}: omitted ({reason}; blind-review job)")
            continue
        if not blind_review:
            if a.resume_pdf is not None:
                entries.append((f"as-is/{label}_resume.pdf", a.resume_pdf))
            if a.cover_pdf is not None:
                entries.append((f"as-is/{label}_cover_letter.pdf", a.cover_pdf))
        if resume_anon is not None:
            entries.append((f"anonymized/{label}_resume.pdf", resume_anon))
        if cover_anon is not None:
            entries.append((f"anonymized/{label}_cover_letter.pdf", cover_anon))
        if a.resume_pdf is None:
            lines.append(f"{label}: file missing (not anonymized)")
        elif full:
            anonymized += 1
            if a.cover_missing:
                lines.append(f"{label}: anonymized (cover letter file missing)")
            else:
                lines.append(f"{label}: anonymized")
        elif resume_anon is None:
            lines.append(f"{label}: as-is only (not anonymized)")
        else:
            lines.append(
                f"{label}: anonymized resume only (cover letter not anonymized)"
            )
    total = len(applicants)
    manifest = "\n".join(
        [
            "Resume export",
            f"applicants: {total}",
            f"anonymized: {anonymized}",
            f"not anonymized: {total - anonymized}",
            *([] if blind_review else ["", _WARNING]),
            "",
            *lines,
            "",
        ]
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("MANIFEST.txt", manifest)
        for name, data in entries:
            zf.writestr(name, data)
    return ExportResult(buf.getvalue(), total, anonymized, total - anonymized)


def build_export_zip(applicants: list[ExportApplicant], *, blind_review: bool) -> bytes:
    return build_export(applicants, blind_review=blind_review).data


__all__ = [
    "MAX_IMAGE_AREA_FRACTION",
    "ExportApplicant",
    "ExportResult",
    "NotAnonymizedError",
    "build_export",
    "build_export_zip",
    "collect_terms",
    "normalize_text",
    "redact_pdf",
]
