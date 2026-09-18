"""Combined-Taleo-export detection + cover-letter-text predicate.

A Taleo "combined export" PDF concatenates MANY applicants' résumés (and often
cover letters) into one file. Uploaded as-is, today's pipeline ingests the
WHOLE document as a single résumé and parses every page — cover letters
included — as that one applicant.

``looks_like_combined_export`` is a cheap, pre-parse heuristic the upload
route (``src.api.routes.resumes``) runs on every expanded PDF part so a
combined export is refused with actionable guidance instead of silently
mis-ingested. It is deliberately conservative (page-count floor + counting
distinct PAGES that each contribute a not-yet-seen email, not merely "any
email" or "any distinct email anywhere in the document") and NEVER raises —
a detector that crashes the upload route on a bad PDF would be worse than
not detecting at all; the real parse step downstream still surfaces that
failure properly.

**Per-page, not per-email (reviewer finding, 2026-09-18).** An earlier version
counted every distinct email address found anywhere in the scanned band, which
refused a genuinely single-applicant CV whose references page happens to list
several referees' emails (three distinct addresses on ONE page, zero other
applicants). The count that matters is how many DIFFERENT PAGES introduce a
new (not-yet-seen) email — only the page's FIRST email match is looked at, so
a references page contributes at most one to the count (via whichever email
reads first on that page, almost always the CV owner's own repeated contact
line, which is already seen and so doesn't count at all).

``is_cover_letter_text`` is the pure text predicate the upload route uses to
decide whether an orphaned cover-named file is ACTUALLY cover-letter-shaped
(vs. just named like one) before refusing it outright — see
``bulk_ingest_service.pair_applicants``'s ``is_cover_content`` kwarg. The
patterns mirror ``core/scripts/split_taleo_pdf.py``'s ``_is_cover`` (the
splitter's own cover-letter signal), so the same document reads the same way
in both tools.
"""

from __future__ import annotations

import re

import fitz  # type: ignore[import-untyped]

_MIME_PDF = "application/pdf"

# A normal résumé (+ optional single cover letter) is a handful of pages;
# nobody legitimately uploads a 3-6 page combined export. The floor applies
# BEFORE the email-count check.
_PAGE_FLOOR = 6

# Only the header of each page is scanned (cheap, and avoids picking up
# stray emails mentioned deep in a résumé's body/references section).
_HEADER_CHARS = 400

# Security audit F1 (2026-09-18): bound the scan to the first
# ``_MAX_SCAN_PAGES`` pages. Mirrors ``core/src/pipeline/parsing/extract.py``'s
# ``_MAX_PDF_PAGES`` precedent and its comment: this module is a TRUST
# BOUNDARY (it runs on every expanded PDF part before any auth/size gate the
# real parse enforces downstream), so it has to be safe STANDALONE against an
# attacker-crafted page count, not merely fast on a normal résumé. A combined
# export legitimately trips detection within the first handful of pages
# (``_PAGE_FLOOR`` is 6), so 300 pages is far more than needed to classify
# one and only bounds the worst case.
_MAX_SCAN_PAGES = 300

# Fraction of a page's height scanned per page, as a clip rect — the email
# signal lives in the top-of-page contact block, and clipping avoids
# extracting each page's FULL text (a résumé's body/references section can be
# large) only to slice it down to ``_HEADER_CHARS`` afterwards.
_HEADER_BAND_FRACTION = 0.25

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Mirrors core/scripts/split_taleo_pdf.py's `_COVER` regex exactly, so a
# document reads the same way whether the splitter or this predicate looks
# at it.
_COVER_RE = re.compile(
    r"\b(dear\b|to whom it may concern|i am writing|hiring manager|"
    r"sincerely|kind regards|best regards|yours (?:truly|sincerely))",
    re.IGNORECASE,
)


def looks_like_combined_export(data: bytes, mime: str) -> str | None:
    """Return a human reason string when ``data`` looks like an unsplit
    combined Taleo export, else ``None``. Never raises.

    Verdict "combined" when >= 3 distinct PAGES each contribute a
    not-yet-seen email in the first lines of the page (first 400 chars of a
    top-band clip, first email match on that page only — see the module
    docstring for why it's per-page rather than a raw distinct-email count),
    OR >= 2 such pages AND >= 8 total pages — and only once ``page_count >
    6``. The reason string never includes a filename (this function doesn't
    even take one).
    """
    if mime != _MIME_PDF:
        return None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return None
    try:
        try:
            needs_pass = doc.needs_pass
        except Exception:
            return None
        if needs_pass:
            return None
        try:
            page_count = int(getattr(doc, "page_count", 0) or 0)
        except Exception:
            return None
        if page_count <= _PAGE_FLOOR:
            return None

        # Per-page, not per-email (see module docstring): a page counts
        # toward the verdict only when its FIRST email match (reading order,
        # within the top-band clip) hasn't been seen on an earlier page. A
        # page with several emails in its band (e.g. a references section)
        # still contributes at most one — and typically zero, since the
        # page's own first line is usually the same owner contact repeated
        # from earlier pages.
        seen: set[str] = set()
        contributing_pages = 0
        try:
            scan_pages = min(page_count, _MAX_SCAN_PAGES)
            for i in range(scan_pages):
                page = doc[i]
                clip = fitz.Rect(
                    0, 0, page.rect.width, page.rect.height * _HEADER_BAND_FRACTION
                )
                head = (page.get_text("text", clip=clip) or "")[:_HEADER_CHARS]
                match = _EMAIL_RE.search(head)
                if match is None:
                    continue
                first_email = match.group(0).lower()
                if first_email not in seen:
                    contributing_pages += 1
                    seen.add(first_email)
        except Exception:
            return None

        combined = contributing_pages >= 3 or (
            contributing_pages >= 2 and page_count >= 8
        )
        if not combined:
            return None
        return (
            f"this PDF has {page_count} pages and {contributing_pages} distinct "
            "applicant emails in the first lines of its pages — it looks like a "
            "combined Taleo export, not one résumé; split it first with "
            "scripts/split-taleo.sh and upload the per-applicant files"
        )
    finally:
        try:
            doc.close()
        except Exception:
            pass


def is_cover_letter_text(text: str) -> bool:
    """True when ``text`` reads as a cover letter (salutation/sign-off
    patterns), false otherwise. Pure regex — mirrors the splitter's
    ``_is_cover``."""
    return bool(_COVER_RE.search(text))


__all__ = ["is_cover_letter_text", "looks_like_combined_export"]
