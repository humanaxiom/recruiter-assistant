"""Combined-Taleo-export detection + cover-letter-text predicate.

A Taleo "combined export" PDF concatenates MANY applicants' résumés (and often
cover letters) into one file. Uploaded as-is, today's pipeline ingests the
WHOLE document as a single résumé and parses every page — cover letters
included — as that one applicant.

``looks_like_combined_export`` is a cheap, pre-parse heuristic the upload
route (``src.api.routes.resumes``) runs on every expanded PDF part so a
combined export is refused with actionable guidance instead of silently
mis-ingested. It is deliberately conservative (page-count floor + a DISTINCT
email count, not merely "any email") and NEVER raises — a detector that
crashes the upload route on a bad PDF would be worse than not detecting at
all; the real parse step downstream still surfaces that failure properly.

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

    Verdict "combined" when the page headers (first 400 chars of each page)
    carry >= 3 DISTINCT email addresses, OR >= 2 distinct emails AND >= 8
    pages — and only once ``page_count > 6``. The reason string never
    includes a filename (this function doesn't even take one).
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

        emails: set[str] = set()
        try:
            for page in doc:
                head = (page.get_text("text") or "")[:_HEADER_CHARS]
                for m in _EMAIL_RE.finditer(head):
                    emails.add(m.group(0).lower())
        except Exception:
            return None

        n_emails = len(emails)
        combined = n_emails >= 3 or (n_emails >= 2 and page_count >= 8)
        if not combined:
            return None
        return (
            f"this PDF has {page_count} pages and {n_emails} distinct "
            "applicant emails in its page headers — it looks like a combined "
            "Taleo export, not one résumé; split it first with "
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
