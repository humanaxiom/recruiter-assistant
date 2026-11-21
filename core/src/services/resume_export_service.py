"""Export a job's résumés and cover letters as a ZIP: as-is and anonymized.

Pure, synchronous and DB-free (the worker runs it in a thread and owns every
read and write).

**The anonymized copy is REBUILT, never edited.** Every earlier design redacted
the original PDF in place and kept finding identity in channels nobody had
thought of (vector-outline names, ``/ActualText``, XMP on form XObjects, UTF-16
layer names, a second script, letter-spaced headers). So nothing from the
original file survives here at all: the copy is a NEW PDF rendered from the
résumé's extracted text, after that text was redacted and verified. The layout
is lost, on purpose. The pipeline (:func:`redact_pdf`):

1. **Extract** the text in reading order. A page that is mostly image, a file
   with no text layer and anything unreadable or encrypted is refused.
2. **Redact** (:func:`redact_text`): the candidate's name must be findable (the
   vouch); then the name, its tokens and run-ons (``janedoe``, ``jdoe``,
   ``jane.doe``), the stored e-mail / phone / location, employers and schools,
   e-mail addresses, phone numbers, domains and URLs, @handles and postal codes
   become the visible marker :data:`REDACTION_MARKER`, line for line. A second
   script (Han, Cyrillic, Arabic, ...) that is left refuses the file.
3. **Verify** (:func:`verify_text`) the redacted text on its own.
4. **Render** (:func:`render_text_pdf`) a fresh PDF: one non-embedded Helvetica,
   a fixed page size, no images, links, annotations, outline, layers, XMP,
   marked content or drawing, and an ``/Info`` of Producer and Creator only.
   The base-14 Helvetica draws anything outside Latin-1 as a middle dot, so the
   text is first mapped by :func:`renderable_text` (dashes to ``-``, curly
   quotes to straight ones, a few letters folded, ``?`` for what has no
   mapping): that single mapping is the only one applied, so the text that was
   verified is the text that is drawn.
5. **Verify the rendered bytes**: extract the new PDF's own text and run
   :func:`verify_text` again. Exactly those bytes are returned. Anything that
   cannot be vouched for raises :class:`NotAnonymizedError` -- fail closed.

All matching is on normalized text (:func:`normalize_text`): NFKC, case-folded,
accent-stripped, letters with no decomposition (o-slash, ae, eth, thorn, l-stroke
...) folded to ASCII, with apostrophe and hyphen variants unified.

Entry names and the manifest carry only ``applicant-NNN`` numbers -- never a
name, an address or an uploaded filename.
"""

from __future__ import annotations

import datetime
import functools
import io
import itertools
import re
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import fitz  # type: ignore[import-untyped]  # PyMuPDF ships no stubs

from src.settings import get_settings

#: A page whose images cover more than this fraction of its area is refused: a
#: scan or an image résumé keeps the identity in pixels no text pass can verify.
MAX_IMAGE_AREA_FRACTION = 0.5

#: What replaces every redacted span in the rebuilt copy.
REDACTION_MARKER = "[redacted]"

#: The fixed, generic document properties of every rebuilt PDF.
PDF_PRODUCER = "Resume export (anonymized copy)"
PDF_CREATOR = "Recruiter Assistant"
#: US Letter, in points.
PDF_PAGE_SIZE = (612.0, 792.0)

#: The manifest note for an applicant who typed a cover letter into the app.
COVER_TYPED_NOTE = "cover letter typed in the app; not exported"

_MIN_TOKEN_LEN = 2
_MIN_PHONE_DIGITS_BARE = 10
_MIN_PHONE_DIGITS_SEPARATED = 7
_MIN_RUN_ON_LEN = 4
_MIN_SQUASHED_FULL = 4
_MIN_SQUASHED_PART = 4

_BIG = fitz.Rect(-20000, -20000, 20000, 20000)
# No MediaBox clipping: text that sits outside the page is still text.
_UNCLIPPED = fitz.TEXT_PRESERVE_WHITESPACE

# Includes the modifier letters typed for an apostrophe (U+02BB, 02BD, 02BE, 02BF,
# 02B9, 02C8 and A78C).
_APOSTROPHES = frozenset("‘’‛ʼ´`′ʻʽʾʿʹˈꞌ")
_DASHES = frozenset("‐‑‒–—―−")

#: Letters Unicode gives no decomposition, so accent-stripping leaves them
#: alone: o-slash, ae, eth, d-stroke, thorn, l-stroke, l-middle-dot, dotless i,
#: oe, h-stroke, t-stroke. Keyed by the CASEFOLDED letter.
_FOLD = {
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
    "ə": "a",
    "ɓ": "b",
    "ɗ": "d",
    "ƙ": "k",
    "ƴ": "y",
    "ŋ": "n",
    "ɛ": "e",
    "ɔ": "o",
}

#: Letters that have a second, spelled-out form in a name: a-umlaut is also
#: "ae", o-umlaut and o-slash "oe", u-umlaut "ue", a-ring "aa". Index 0 is the
#: plain letter, index 1 the digraph.
_UMLAUT_FORMS = {
    "ä": ("a", "ae"),
    "ö": ("o", "oe"),
    "ü": ("u", "ue"),
    "ø": ("o", "oe"),
    "å": ("a", "aa"),
}
_DIGRAPH_RE = re.compile("ae|oe|ue|aa")
#: The digraphs that spell an umlaut (not ``aa``): the only ones the vouch reads back.
_UMLAUT_DIGRAPH_RE = re.compile("ae|oe|ue")
#: Past this many variable spots in one name the variants are only the two
#: extremes, so a pathological name cannot make thousands of patterns.
_MAX_UMLAUT_SLOTS = 4
_MAX_DIGRAPH_SLOTS = 3
#: A token that only exists in a derived spelling must be this long: ``Joe``
#: collapses to ``jo``, which is an ordinary word and not a name.
_MIN_VARIANT_TOKEN_LEN = 3

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# U+00B7 (middle dot) is what a base-14 font writes for a dash it lacks.
_PHONE_RE = re.compile(r"(?<![\w])\+?\(?\d[\d()./ \-·]{5,}\d(?![\w])")
# "2015-2019", "2019 - 2021", "09/2019 - 04/2021": a span of years, not a phone.
_YEAR_RANGE_RE = re.compile(
    r"(?:(\d{1,2})/)?((?:19|20)\d{2})\s*[-/ ]\s*(?:(\d{1,2})/)?((?:19|20)\d{2})"
)
# A number written right after one of these is a phone number, whatever its shape.
# The text is normalized (accent-folded, case-folded) before it is matched, so
# tel / tél, teléfono / telefono and móvil / movil are each one entry. The label
# must be ADJACENT to the number: it ends the text before it, with at most a
# "no." / "number" / "#" and one ":" "." or "-" between. A lone "M" or "T" only
# counts when a ":" follows it.
_PHONE_LABELS = (
    "tel|telephone|telefon|telefono|phones?|cell|cellphone|cel|celular|mobile|"
    "movil|mob|handy|fax|ph|whatsapp|tlf"
)
_PHONE_LABEL_RE = re.compile(
    rf"(?<![a-z])(?:(?:{_PHONE_LABELS})(?![a-z])"
    r"(?:\s*(?:no\b\.?|number(?![a-z])|#))?\s*[:.\-]?"
    r"|[mt]\s*:)\s*$"
)
_DATE_RE = re.compile(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}")
_SEPARATORS = frozenset(" -./()·")

_SCHEME_URL_RE = re.compile(r"(?:https?|ftp)://[^\s]+|www\.[^\s]+")
_TLDS = (
    "com|org|net|edu|gov|io|ca|co|ai|dev|app|info|biz|us|uk|ly|xyz|tech|me|site|"
    "online|page|blog|cv"
)
_DOMAIN_RE = re.compile(
    rf"(?<![\w@])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:{_TLDS})(?![a-z0-9])"
    r"(?:[/?#][^\s]*)?"
)
#: Tech names that are shaped like a domain and are not one.
_NOT_DOMAINS = frozenset({"asp.net", "vb.net", "f.net", "c.net"})
_TRAILING_PUNCT = ".,;:!?)]}'\""
_HANDLE_RE = re.compile(r"(?<![\w@.])@[a-z0-9_]{2,}(?:\.[a-z0-9_]+)*")
# Canadian postal code; the letters D, F, I, O, Q, U never occur, and W and Z
# never lead.
_POSTAL_RE = re.compile(
    r"(?<![a-z0-9])[abceghj-nprstvxy]\d[abceghj-nprstv-z][ -]?"
    r"\d[abceghj-nprstv-z]\d(?![a-z0-9])"
)

# A script a Latin-script résumé has no business holding after redaction: it is
# where a name written in another script hides (and the Latin-only rebuild
# could not show it anyway).
_OTHER_SCRIPT_RE = re.compile(
    "["
    "Ѐ-ԯᲀ-᲏"  # Cyrillic
    "֐-׿יִ-ﭏ"  # Hebrew
    "؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿"  # Arabic
    "ऀ-ॿ"  # Devanagari
    "฀-๿"  # Thai
    "Ͱ-Ͽἀ-῿"  # Greek, Greek Extended
    "԰-֏"  # Armenian
    "Ⴀ-ჿᲐ-Ჿⴀ-⴯"  # Georgian
    "ሀ-᏿ⶀ-⷟"  # Ethiopic
    "ঀ-૿"  # Bengali (the block range also takes Gurmukhi and Gujarati)
    "஀-௿"  # Tamil
    "Ꙁ-ꚟ"  # Cyrillic Extended-B
    "ᄀ-ᇿ㄰-㆏가-힯"  # Hangul
    "぀-ヿㇰ-ㇿｦ-ﾟ"  # Hiragana, Katakana
    "㐀-䶿一-鿿豈-﫿\U00020000-\U0002fa1f"  # Han
    "]"
)

_LINE_BREAKS = re.compile("\r\n|[\r\x0b\x0c\x85  ]")


class NotAnonymizedError(Exception):
    """A redacted copy could not be vouched for."""


# ---------------------------------------------------------------- normalization


@functools.lru_cache(maxsize=8192)
def _norm_char(ch: str) -> str:
    if ch in _APOSTROPHES:
        return "'"
    if ch in _DASHES:
        return "-"
    # Before NFKC: U+013F (capital L with middle dot) would decompose to "l·".
    if (letter := _FOLD.get(ch.casefold())) is not None:
        return letter
    folded = unicodedata.normalize("NFKC", ch).casefold()
    out: list[str] = []
    for c in unicodedata.normalize("NFD", folded):
        if unicodedata.combining(c) or unicodedata.category(c) == "Cf":
            continue
        if c in _FOLD:
            c = _FOLD[c]
        elif c in _APOSTROPHES:
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


# ----------------------------------------------------------------------- shapes


def _plausible_year_range(span: str) -> bool:
    """``span`` is a span of years a career could hold: months 1-12, the first
    year not after the second, the second not past next year."""
    m = _YEAR_RANGE_RE.fullmatch(span)
    if m is None:
        return False
    for month in (m.group(1), m.group(3)):
        if month is not None and not 1 <= int(month) <= 12:
            return False
    return int(m.group(2)) <= int(m.group(4)) <= datetime.date.today().year + 1


def _phones(norm_line: str) -> list[re.Match[str]]:
    found: list[re.Match[str]] = []
    for m in _PHONE_RE.finditer(norm_line):
        span = m.group(0).strip()
        digits = sum(c.isdigit() for c in span)
        separated = any(c in _SEPARATORS for c in span)
        labelled = _PHONE_LABEL_RE.search(norm_line[: m.start()]) is not None
        if not labelled and (_plausible_year_range(span) or _DATE_RE.fullmatch(span)):
            continue
        bare = digits >= _MIN_PHONE_DIGITS_BARE
        local = separated and digits >= _MIN_PHONE_DIGITS_SEPARATED
        if bare or local:
            found.append(m)
    return found


def _trimmed(norm_line: str, begin: int, end: int) -> tuple[int, int]:
    """``[begin, end)`` without the sentence punctuation that trails a URL."""
    while end > begin + 1 and norm_line[end - 1] in _TRAILING_PUNCT:
        end -= 1
    return begin, end


def _shape_spans(norm_line: str) -> list[tuple[int, int]]:
    """Spans of one normalized line that are contact or web data by SHAPE:
    e-mail, URL, domain, @handle, postal code and phone number."""
    spans = [m.span() for m in _EMAIL_RE.finditer(norm_line)]
    spans.extend(
        _trimmed(norm_line, *m.span()) for m in _SCHEME_URL_RE.finditer(norm_line)
    )
    for m in _DOMAIN_RE.finditer(norm_line):
        if m.group(0) not in _NOT_DOMAINS:
            spans.append(_trimmed(norm_line, *m.span()))
    spans.extend(m.span() for m in _HANDLE_RE.finditer(norm_line))
    spans.extend(m.span() for m in _POSTAL_RE.finditer(norm_line))
    spans.extend(m.span() for m in _phones(norm_line))
    return spans


# -------------------------------------------------------------------- the terms


def _expand_umlauts(raw: str) -> list[str]:
    """The normalized spellings of ``raw`` with every umlaut-like letter written
    both as itself and as its digraph (``Müller`` -> muller, mueller)."""
    text = unicodedata.normalize("NFC", raw).casefold()
    slots = [i for i, c in enumerate(text) if c in _UMLAUT_FORMS]
    if not slots:
        return [normalize_text(text)]
    picks: Iterable[tuple[int, ...]]
    if len(slots) > _MAX_UMLAUT_SLOTS:
        picks = [(0,) * len(slots), (1,) * len(slots)]
    else:
        picks = itertools.product((0, 1), repeat=len(slots))
    out: list[str] = []
    for pick in picks:
        chars = list(text)
        for i, which in zip(slots, pick, strict=True):
            chars[i] = _UMLAUT_FORMS[text[i]][which]
        out.append(normalize_text("".join(chars)))
    return list(dict.fromkeys(out))


def _vouch_forms(token: str) -> list[str]:
    """The spellings a token may VOUCH on: the stored one first, then its umlaut
    expansions (``Müller`` -> mueller) and the umlaut digraphs of an already
    spelled-out token read back as the letter (``Mueller`` -> muller, the
    spelling a document that kept the umlaut shows). ``aa`` is deliberately not
    read back (``Haas`` is not ``Has``), and a read-back token under
    :data:`_MIN_VARIANT_TOKEN_LEN` letters is dropped (``Boe`` is not ``Bo``):
    both are ordinary words in someone else's résumé."""
    forms: dict[str, None] = {normalize_text(token): None}
    for expanded in _expand_umlauts(token):
        forms.setdefault(expanded, None)
        spots = [m.span() for m in _UMLAUT_DIGRAPH_RE.finditer(expanded)]
        if not spots or len(spots) > _MAX_DIGRAPH_SLOTS:
            continue
        for pick in itertools.product((0, 1), repeat=len(spots)):
            pieces: list[str] = []
            pos = 0
            for (begin, end), cut in zip(spots, pick, strict=True):
                pieces.append(expanded[pos:begin])
                pieces.append(expanded[begin] if cut else expanded[begin:end])
                pos = end
            pieces.append(expanded[pos:])
            form = "".join(pieces)
            if len(form) >= _MIN_VARIANT_TOKEN_LEN:
                forms.setdefault(form, None)
    return list(forms)


def _collapse_digraphs(norm: str) -> list[str]:
    """``norm`` with each ae / oe / ue / aa kept or cut to its first letter
    (``mueller`` -> muller)."""
    spots = [m.span() for m in _DIGRAPH_RE.finditer(norm)]
    if not spots:
        return [norm]
    picks: Iterable[tuple[int, ...]]
    if len(spots) > _MAX_DIGRAPH_SLOTS:
        picks = [(0,) * len(spots), (1,) * len(spots)]
    else:
        picks = itertools.product((0, 1), repeat=len(spots))
    out: list[str] = []
    for pick in picks:
        pieces: list[str] = []
        pos = 0
        for (begin, end), collapse in zip(spots, pick, strict=True):
            pieces.append(norm[pos:begin])
            pieces.append(norm[begin] if collapse else norm[begin:end])
            pos = end
        pieces.append(norm[pos:])
        out.append("".join(pieces))
    return out


#: Past this many whole-name spellings the product of the per-token variants is
#: cut off (every token is still matched on its own).
_MAX_NAME_VARIANTS = 256


def _token_variants(token: str) -> list[str]:
    """The normalized token FIRST, then the other ways it is spelled by
    transliteration: each umlaut letter plain or as its digraph, and each digraph
    kept or cut to one letter. Built per token, so a name that mixes spellings
    inside one token (``Muellerhofer`` for ``Müllerhöfer``) is covered whatever
    the other tokens hold. Generated from the stored name only, never by
    rewriting the document, so unrelated words are untouched."""
    found: dict[str, None] = {normalize_text(token): None}
    for expanded in _expand_umlauts(token):
        for variant in _collapse_digraphs(expanded):
            if variant:
                found.setdefault(variant, None)
    return list(found)


def _joined(per_token: list[list[str]], base: str) -> list[str]:
    """``base`` first, then every combination of one spelling per token."""
    found: dict[str, None] = {base: None}
    for combo in itertools.islice(itertools.product(*per_token), _MAX_NAME_VARIANTS):
        found.setdefault(" ".join(combo), None)
    return list(found)


_NAME_SPLIT = re.compile(r"[\s,.]+")
_PART_SPLIT = re.compile(r"[-']+")


def _parts(token: str) -> list[str]:
    """A name token cut at hyphens and apostrophes, keeping parts of two or more
    letters: ``smith-jones`` -> smith, jones; ``o'brien`` -> brien."""
    return [p for p in _PART_SPLIT.split(token) if len(p) >= _MIN_TOKEN_LEN]


def _variants(token: str) -> list[str]:
    """The ways a name token can be written as one word: each part on its own
    and the whole token glued together (``obrien``, ``smithjones``)."""
    out = _parts(token)
    glued = _PART_SPLIT.sub("", token)
    if len(glued) >= _MIN_TOKEN_LEN:
        out.append(glued)
    return list(dict.fromkeys(out))


def _run_ons(firsts: list[str], lasts: list[str]) -> list[str]:
    """Spellings a name takes when its parts are glued together or initialled:
    ``janedoe``, ``jane.doe``, ``jane_doe``, ``jdoe``, ``doej``... built per
    first-name part and per surname part, so ``Mary Smith-Jones`` also yields
    ``mjones`` and ``jonesmary``. ``firsts`` and ``lasts`` are the spellings of
    the first and of the last name token."""
    forms: list[str] = []
    for first in dict.fromkeys(v for t in firsts for v in _variants(t)):
        for last in dict.fromkeys(v for t in lasts for v in _variants(t)):
            for sep in ("", ".", "_", "-"):
                forms.append(first + sep + last)
                forms.append(last + sep + first)
                forms.append(first[0] + sep + last)
                forms.append(last + sep + first[0])
    return [f for f in dict.fromkeys(forms) if len(f) >= _MIN_RUN_ON_LEN]


def _run_on_re(norm_form: str) -> re.Pattern[str]:
    """A run-on as a pattern that stops at LETTERS only and takes the digits and
    underscores glued to it along, so ``janedoe88`` and ``jdoe_99`` go whole while
    ``Jonesboro`` does not."""
    return re.compile(
        r"(?<![^\W\d_])[\d_]*" + re.escape(norm_form) + r"[\d_]*(?![^\W\d_])"
    )


def _name_re(norm_name: str) -> re.Pattern[str]:
    """A name (or a token of it) as a pattern that stops at LETTERS only and
    takes the digits and underscores glued to it along: ``mctestface88``,
    ``88mctestface`` and ``doe_2024`` go whole. A digit is not a word boundary
    for a name."""
    return re.compile(
        r"(?<![^\W\d_])[\d_]*" + re.escape(norm_name) + r"[\d_]*(?![^\W\d_])"
    )


def _compile_terms(
    terms: list[str],
) -> tuple[list[re.Pattern[str]], list[str], list[str]]:
    """Patterns for the terms, for the name's own tokens (and the parts of a
    hyphenated or apostrophised one) and for the name's run-ons -- for every
    transliteration variant of each token -- so neither redaction nor
    verification depends on the caller having listed them. Also the spellings
    the squashed check looks for, and the spellings the vouch may pass on: the
    stored one and its umlaut expansions only, never a collapsed digraph."""
    if not terms or not terms[0].strip():
        raise NotAnonymizedError("no candidate name to vouch for")
    stored = terms[0]
    base = normalize_text(stored)
    tokens = [t for t in _NAME_SPLIT.split(stored) if t]
    per_token = [_token_variants(t) for t in tokens]
    variants = _joined(per_token, base)
    vouch = _joined([_vouch_forms(t) for t in tokens], base)
    stored_pieces = {
        piece
        for forms in per_token
        for token in _NAME_SPLIT.split(forms[0])
        for piece in (token, *_parts(token))
    }
    names: dict[str, None] = {}
    for variant in variants:
        if variant:
            names.setdefault(variant, None)
    for forms in per_token:
        for form in forms:
            for token in _NAME_SPLIT.split(form):
                for piece in (token, *_parts(token)):
                    derived = piece not in stored_pieces
                    floor = _MIN_VARIANT_TOKEN_LEN if derived else _MIN_TOKEN_LEN
                    if len(piece) >= floor:
                        names.setdefault(piece, None)
    run_ons: dict[str, None] = {}
    if len(tokens) >= _MIN_TOKEN_LEN:
        run_ons.update(dict.fromkeys(_run_ons(per_token[0], per_token[-1])))
    patterns = [_name_re(n) for n in names]
    others: dict[str, None] = {}
    for term in terms:
        norm = normalize_text(term)
        if norm and norm not in names:
            others[norm] = None
    patterns.extend(_term_re(t) for t in others)
    patterns.extend(_run_on_re(f) for f in run_ons)
    squash = [*variants, *(form for forms in per_token for form in forms)]
    return patterns, squash, vouch


def _squash(text: str) -> str:
    return "".join(c for c in text if c.isalnum())


def _needles(full: str) -> list[str]:
    """What to look for once spaces and separators are removed: the full name
    (4+ letters) and every name token and part of it (4+ letters)."""
    found: list[str] = []
    if len(squashed := _squash(full)) >= _MIN_SQUASHED_FULL:
        found.append(squashed)
    for token in _NAME_SPLIT.split(full):
        found.extend(
            v for v in _variants(token) if len(_squash(v)) >= _MIN_SQUASHED_PART
        )
    return list(dict.fromkeys(_squash(n) for n in found))


def _squashed_spans(norm: str, fulls: list[str]) -> list[tuple[int, int]]:
    """Spans of ``norm`` where the full name or any part of it shows up with
    every space and separator removed -- ``J A N E  D O E``, ``J-A-N-E``,
    ``Mc Test face`` -- but only where it starts and ends on a token boundary,
    so ``Ann Ford`` never eats ``Stanford``."""
    needles = list(dict.fromkeys(n for full in fulls for n in _needles(full)))
    if not needles:
        return []
    # The marker is ours, not the document's: blank it, keeping every index.
    masked = norm.replace(REDACTION_MARKER, " " * len(REDACTION_MARKER))
    chars: list[str] = []
    index: list[int] = []
    for i, c in enumerate(masked):
        if c.isalnum():
            chars.append(c)
            index.append(i)
    flat = "".join(chars)
    spans: list[tuple[int, int]] = []
    for needle in needles:
        at = flat.find(needle)
        while at != -1:
            begin, last = index[at], index[at + len(needle) - 1]
            starts = begin == 0 or not masked[begin - 1].isalnum()
            ends = last + 1 == len(masked) or not masked[last + 1].isalnum()
            if starts and ends:
                spans.append((begin, last + 1))
            at = flat.find(needle, at + 1)
    return spans


def _refuse_unrenderable_letters(text: str) -> None:
    """A LETTER the renderer can only draw as ``?`` hides from every check while
    a reader still knows the word: refuse. Symbols that draw as ``?`` are fine."""
    for ch in set(text):
        if unicodedata.category(ch).startswith("L") and renderable_text(ch) == "?":
            raise NotAnonymizedError("a letter the copy cannot draw would be lost")


def _refuse_other_scripts(text: str) -> None:
    if _OTHER_SCRIPT_RE.search(text):
        raise NotAnonymizedError("text in another script cannot be verified")


def _unify_line_breaks(text: str) -> str:
    return _LINE_BREAKS.sub("\n", text)


def _require_name_found(norm_text: str, variants: list[str]) -> None:
    """The vouch. ``variants`` is the stored spelling first and then only its
    umlaut expansions: a spelling made by CUTTING a digraph (``haas`` -> ``has``)
    is an ordinary word and must never vouch for a document."""
    if not norm_text:
        raise NotAnonymizedError("no text layer to redact or verify")
    if not variants[0]:
        raise NotAnonymizedError("no candidate name to vouch for")
    for i, full in enumerate(variants):
        floor = _MIN_TOKEN_LEN if i == 0 else _MIN_VARIANT_TOKEN_LEN
        for name in (full, full.rsplit(" ", 1)[-1]):
            if len(name) >= floor and _term_re(name).search(norm_text):
                return
    raise NotAnonymizedError("the candidate's name is not in the document text")


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with every span replaced by the marker, one marker per LINE the
    span touches, so the line count never changes."""
    out: list[str] = []
    pos = 0
    for begin, end in _merged(spans):
        out.append(text[pos:begin])
        pieces = text[begin:end].split("\n")
        out.append("\n".join(REDACTION_MARKER if p.strip() else p for p in pieces))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for begin, end in sorted(spans):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((begin, end))
    return merged


def redact_text(text: str, terms: list[str]) -> str:
    """The text with every identifying span replaced by :data:`REDACTION_MARKER`.

    Pure. Lines stay 1:1 and in order and ordinary text comes back untouched.
    ``terms[0]`` is the candidate's FULL name (the :func:`collect_terms`
    convention). Raises :class:`NotAnonymizedError` when neither the full name
    nor the surname is in the text (the vouch: a check that only knows the term
    list cannot notice a name the list never held), when there is no text, or
    when text in a second script is left after redaction.
    """
    patterns, squash, vouch = _compile_terms(terms)
    text = _unify_line_breaks(text)
    norm, origin = _norm_map(text)
    _require_name_found(norm, vouch)

    spans: list[tuple[int, int]] = []
    for pattern in patterns:
        for m in pattern.finditer(norm):
            spans.append((origin[m.start()], origin[m.end() - 1] + 1))
    for begin, end in _squashed_spans(norm, squash):
        spans.append((origin[begin], origin[end - 1] + 1))
    offset = 0
    for line in text.split("\n"):
        norm_line, line_origin = _norm_map(line)
        for begin, end in _shape_spans(norm_line):
            spans.append(
                (offset + line_origin[begin], offset + line_origin[end - 1] + 1)
            )
        offset += len(line) + 1

    redacted = _blank(text, spans)
    _refuse_other_scripts(redacted)
    return redacted


def verify_text(text: str, terms: list[str]) -> None:
    """Raise :class:`NotAnonymizedError` if ANY identifying thing is still in
    ``text``: a term, a name token or run-on, an e-mail, phone, URL, domain,
    @handle or postal code, the full name or surname once spaces and separators
    are removed, or a second script."""
    patterns, squash, _vouch = _compile_terms(terms)
    text = _unify_line_breaks(text)
    norm, _origin = _norm_map(text)
    if any(p.search(norm) for p in patterns):
        raise NotAnonymizedError("identifying text survived redaction")
    for line in text.split("\n"):
        if _shape_spans(_norm_map(line)[0]):
            raise NotAnonymizedError("contact or web data survived redaction")
    if _squashed_spans(norm, squash):
        raise NotAnonymizedError("the name survived with its letters spaced out")
    _refuse_other_scripts(text)


# ------------------------------------------------------------------- the PDF


def _raw_text(page: Any) -> str:
    return str(page.get_text("text", clip=_BIG, flags=_UNCLIPPED, sort=True))


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


def _open_pdf(pdf_bytes: bytes) -> Any:
    """The document, or :class:`NotAnonymizedError` for unreadable or encrypted
    input. Only its text is ever used; nothing else is carried over."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise NotAnonymizedError("file is not a readable PDF") from exc
    if doc.needs_pass or doc.is_encrypted:
        doc.close()
        raise NotAnonymizedError("encrypted PDF")
    if not doc.is_pdf or doc.page_count == 0:
        doc.close()
        raise NotAnonymizedError("file is not a readable PDF")
    return doc


def _extract_source(pdf_bytes: bytes) -> str:
    doc = _open_pdf(pdf_bytes)
    try:
        _refuse_image_heavy(doc)
        return "\n".join(_raw_text(page).rstrip("\n") for page in doc)
    finally:
        doc.close()


def _extract_rendered(pdf_bytes: bytes) -> str:
    doc = _open_pdf(pdf_bytes)
    try:
        return "\n".join(_raw_text(page).rstrip("\n") for page in doc)
    finally:
        doc.close()


# ---------------------------------------------------------------- the renderer

_FONT = "helv"  # the base-14 Helvetica, never embedded: it draws Latin-1 only
_FONT_SIZE = 11.0
_LEADING = 14.0
_MARGIN = 72.0

#: What the renderer shows for characters Latin-1 lacks. PyMuPDF writes every
#: other character as a middle dot (U+00B7), which is how "2019" + en dash +
#: "2021" once became a "phone number" and a good resume was refused.
_SUBSTITUTES = {
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
    "−": "-",
    "‚": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "•": "-",
    "…": "...",
    "€": "EUR",
    "™": "TM",
    "Š": "S",
    "š": "s",
    "Ž": "Z",
    "ž": "z",
    "Ÿ": "Y",
}


def _derive_substitutes() -> None:
    """Every letter ``_FOLD`` folds (both cases) and every apostrophe
    ``_APOSTROPHES`` unifies is drawn as the plain text the normaliser reads, so
    the renderer and the normaliser cannot drift apart: a letter one folds and
    the other cannot draw would turn into ``?`` and refuse a good resume."""
    for key, plain in _FOLD.items():
        _SUBSTITUTES[key] = plain
        upper = key.upper()
        if upper != key and len(upper) == 1:
            _SUBSTITUTES[upper] = plain.upper()
    for ch in _APOSTROPHES:
        _SUBSTITUTES[ch] = "'"


_derive_substitutes()


def _drawable(text: str) -> bool:
    """``text`` is Latin-1 that Helvetica draws as itself: no control character
    and no middle dot (which the source may hold, but a mapping must not add)."""
    return all(
        0x20 <= ord(c) <= 0xFF and not 0x7F <= ord(c) <= 0x9F and c != "·" for c in text
    )


def _map_char(ch: str) -> str:
    """One character that is neither whitespace, a control nor a format
    character, as the text Helvetica will draw for it."""
    if ord(ch) <= 0xFF:
        return ch
    if (sub := _SUBSTITUTES.get(ch)) is not None:
        return sub
    decomposed = unicodedata.normalize("NFKD", ch)
    base = "".join(
        _SUBSTITUTES.get(c, c) for c in decomposed if not unicodedata.combining(c)
    )
    return base if base and _drawable(base) else "?"


def renderable_text(text: str) -> str:
    """``text`` exactly as :func:`render_text_pdf` will show it -- the ONLY
    mapping the renderer applies, so what is verified is what is drawn.

    Newlines are kept; other whitespace and control characters become a space;
    format (Cf) characters are dropped; Latin-1 passes through; dashes, curly
    quotes, the bullet, the ellipsis, the euro and trademark signs and a few
    letters Latin-1 lacks become ASCII; anything else becomes ``?``. The result
    is always Latin-1."""
    out: list[str] = []
    for ch in text:
        if ch == "\n":
            out.append(ch)
        elif ch.isspace() or ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F:
            out.append(" ")
        elif unicodedata.category(ch) == "Cf":
            continue
        else:
            out.append(_map_char(ch))
    return "".join(out)


def _width(text: str) -> float:
    """The width ``text`` takes on a line. PyMuPDF under-measures a Latin-1
    letter above U+007F (a row of accented words then runs off the page, where
    the words are lost), so each of those is counted at a full em, which no
    Helvetica glyph exceeds."""
    plain = "".join(c for c in text if ord(c) < 0x80)
    measured = float(fitz.get_text_length(plain, fontname=_FONT, fontsize=_FONT_SIZE))
    return measured + (len(text) - len(plain)) * _FONT_SIZE


def _pieces(word: str, limit: float) -> list[str]:
    """A word that is wider than a line, cut into lines' worth."""
    if _width(word) <= limit:
        return [word]
    parts: list[str] = []
    current = ""
    for ch in word:
        if current and _width(current + ch) > limit:
            parts.append(current)
            current = ch
        else:
            current += ch
    parts.append(current)
    return parts


def _wrap(line: str, limit: float) -> list[str]:
    words = line.split()
    if not words:
        return [""]
    rows: list[str] = []
    current = ""
    for word in words:
        for piece in _pieces(word, limit):
            candidate = f"{current} {piece}" if current else piece
            if current and _width(candidate) > limit:
                rows.append(current)
                current = piece
            else:
                current = candidate
    rows.append(current)
    return rows


def _pin_info(doc: Any) -> None:
    """Producer and Creator only, whatever else the library may have put there."""
    doc.set_metadata({"producer": PDF_PRODUCER, "creator": PDF_CREATOR})
    # The catalog must hold only Type and Pages: the Info reference belongs in
    # the trailer. Some PyMuPDF versions leave it on the catalog too.
    catalog = doc.pdf_catalog()
    kind, value = doc.xref_get_key(-1, "Info")
    if kind != "xref":
        kind, value = doc.xref_get_key(catalog, "Info")
        if kind != "xref":
            return
        doc.xref_set_key(-1, "Info", f"{int(value.split()[0])} 0 R")
    if set(doc.xref_get_keys(catalog)) - {"Type", "Pages"}:
        # Setting a key to "null" would leave a literal null entry behind.
        pages = doc.xref_get_key(catalog, "Pages")[1]
        doc.update_object(catalog, f"<</Type/Catalog/Pages {pages}>>")
    info = int(value.split()[0])
    for key in doc.xref_get_keys(info):
        if key not in ("Producer", "Creator"):
            doc.xref_set_key(info, key, "null")


def render_text_pdf(text: str) -> bytes:
    """A NEW PDF holding only ``text``: one non-embedded Helvetica, fixed page
    size, long lines wrapped, pages added as needed, in the given order. No
    images, annotations, links, embedded files, outline, layers, XMP, forms,
    marked content or drawing; ``/Info`` holds Producer and Creator only."""
    width, height = PDF_PAGE_SIZE
    limit = width - 2 * _MARGIN
    first_baseline = _MARGIN + _FONT_SIZE
    last_baseline = height - _MARGIN
    doc = fitz.open()
    try:
        page: Any = None
        y = first_baseline
        blank_run = False
        for raw in renderable_text(text).split("\n"):
            for row in _wrap(raw, limit):
                if not row:
                    # One blank line at most, and none at the top of a page.
                    if blank_run or page is None or y == first_baseline:
                        continue
                    blank_run = True
                    y += _LEADING
                    continue
                blank_run = False
                if page is None or y > last_baseline:
                    page = doc.new_page(width=width, height=height)
                    y = first_baseline
                page.insert_text((_MARGIN, y), row, fontname=_FONT, fontsize=_FONT_SIZE)
                y += _LEADING
        if page is None:
            doc.new_page(width=width, height=height)
        _pin_info(doc)
        data: bytes = doc.tobytes(garbage=4, deflate=True)
        return data
    finally:
        doc.close()


# ----------------------------------------------------------------- the pipeline


def redact_pdf(pdf_bytes: bytes, terms: list[str]) -> bytes:
    """A NEW PDF built from the redacted, twice-verified text of ``pdf_bytes``.

    ``terms[0]`` is the candidate's FULL name (the :func:`collect_terms`
    convention). Raises :class:`NotAnonymizedError` when the input is not a
    readable, unencrypted PDF with a text layer, is mostly image, never shows
    the candidate's name, holds a second script, or anything identifying could
    survive -- in the redacted text or in the rendered PDF's own text.
    """
    try:
        source = _extract_source(pdf_bytes)
        redacted = redact_text(source, terms)
        _refuse_unrenderable_letters(redacted)
        verify_text(redacted, terms)
        rendered = render_text_pdf(redacted)
        verify_text(_extract_rendered(rendered), terms)
    except NotAnonymizedError:
        raise
    except Exception as exc:
        raise NotAnonymizedError("the anonymized copy could not be built") from exc
    return rendered


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
    cover_typed: bool = False  # a cover letter was typed in the app (no file)


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
    not_exported: int = 0,
) -> ExportResult:
    """The ZIP. ``not_exported`` is how many live applicants the caller left out
    (the worker's cap); when it is above zero the manifest says so in one line."""
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
        note = f"; {COVER_TYPED_NOTE}" if a.cover_typed else ""
        resume_anon = _try_redact(a.resume_pdf, a.terms, cap)
        cover_anon = _try_redact(a.cover_pdf, a.terms, cap)
        cover_failed = a.cover_pdf is not None and cover_anon is None
        full = resume_anon is not None and not cover_failed
        if blind_review and not full:
            reason = "file missing" if a.resume_pdf is None else "not anonymized"
            lines.append(f"{label}: omitted ({reason}; blind-review job){note}")
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
            lines.append(f"{label}: file missing (not anonymized){note}")
        elif full:
            anonymized += 1
            if a.cover_missing:
                lines.append(f"{label}: anonymized (cover letter file missing){note}")
            else:
                lines.append(f"{label}: anonymized{note}")
        elif resume_anon is None:
            lines.append(f"{label}: as-is only (not anonymized){note}")
        else:
            lines.append(
                f"{label}: anonymized resume only (cover letter not anonymized){note}"
            )
    total = len(applicants)
    manifest = "\n".join(
        [
            "Resume export",
            f"applicants: {total}",
            f"anonymized: {anonymized}",
            f"not anonymized: {total - anonymized}",
            *(
                [f"{not_exported} applicants not exported (over the cap)"]
                if not_exported > 0
                else []
            ),
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
    "COVER_TYPED_NOTE",
    "MAX_IMAGE_AREA_FRACTION",
    "PDF_CREATOR",
    "PDF_PAGE_SIZE",
    "PDF_PRODUCER",
    "REDACTION_MARKER",
    "ExportApplicant",
    "ExportResult",
    "NotAnonymizedError",
    "build_export",
    "build_export_zip",
    "collect_terms",
    "normalize_text",
    "redact_pdf",
    "redact_text",
    "render_text_pdf",
    "renderable_text",
    "verify_text",
]
