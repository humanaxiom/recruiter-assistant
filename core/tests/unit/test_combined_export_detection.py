"""Unit tests for ``src.services.combined_export`` (NEW module — does not
exist yet, so the whole file fails at collection; RED half of the TDD cycle).

A Taleo "combined export" PDF concatenates MANY applicants' résumés (and
often cover letters) into one file. Uploaded as-is, today's pipeline ingests
the WHOLE document as a single résumé and parses every page — cover letters
included — as that one applicant. ``looks_like_combined_export`` is a cheap,
pre-parse heuristic the upload route runs on every PDF part so a combined
export is refused with actionable guidance instead of silently mis-ingested.

**Contract pinned here** (see the route wiring in
``test_route_resumes.py``/``resumes.py`` for how the reason string is used):

* ``looks_like_combined_export(data: bytes, mime: str) -> str | None``
  - PDF only: a non-PDF mime returns ``None`` WITHOUT ever opening ``data``
    as a PDF (so garbage bytes under a non-PDF mime never raise).
  - ``page_count <= 6`` -> ``None`` (a normal résumé + cover letter is a
    handful of pages; nobody legitimately uploads a 3-6 page combined export).
  - Verdict "combined" (a non-``None`` reason string) when the page headers
    (first 400 chars of each page) carry >= 3 DISTINCT email addresses, OR
    >= 2 distinct emails AND >= 8 pages.
  - The SAME email repeated on every page (a genuine single applicant whose
    résumé happens to be long) must NOT trip detection — this is the
    false-positive guard the "DISTINCT" in the spec exists for.
  - Password-protected / corrupt / zero-text PDFs -> ``None``, NEVER raise —
    a detector that crashes the upload route on a bad PDF would be worse
    than not detecting at all (the real parse step downstream still surfaces
    the failure properly).
  - The reason string contains the page count, the distinct-email
    ("applicant") count, the phrase "split it first", and the script name
    ``scripts/split-taleo.sh`` — and NEVER a filename (the function doesn't
    even take one).

* ``is_cover_letter_text(text: str) -> bool`` — the pure text predicate the
  upload route uses to decide whether an orphaned cover-named file is
  ACTUALLY cover-letter-shaped (vs. just named like one), fires on
  salutation/sign-off patterns ("Dear …", "Sincerely", "I am writing to
  apply").
"""

from __future__ import annotations

from io import BytesIO

import fitz  # type: ignore[import-untyped]

from src.services.combined_export import (
    is_cover_letter_text,
    looks_like_combined_export,
)

_MIME_PDF = "application/pdf"
_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_TXT = "text/plain"


def _insert_lines(page: fitz.Page, lines: list[str], *, y0: float = 72, dy: float = 14) -> None:
    for i, line in enumerate(lines):
        if line:
            page.insert_text((72, y0 + i * dy), line, fontsize=10)


def _make_pdf(pages: list[list[str]]) -> bytes:
    """One page per entry; each entry is a list of text lines placed near the
    top of the page (well within the first 400 chars a header-scan reads)."""
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        _insert_lines(page, lines)
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _header_page(email: str, i: int) -> list[str]:
    return [
        f"Applicant {i}",
        f"applicant{i}@example.invalid" if email is None else email,
        "604-555-0100",
        "RESUME",
    ]


def _make_encrypted_pdf_bytes(n_pages: int = 12) -> bytes:
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    buf = BytesIO()
    doc.save(
        buf,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
        permissions=int(fitz.PDF_PERM_PRINT),
    )
    doc.close()
    return buf.getvalue()


# ── looks_like_combined_export: the "combined" verdict ──────────────────────


def test_twelve_pages_three_distinct_emails_is_combined() -> None:
    emails = [
        "pat.example@example.invalid",
        "sam.example@example.invalid",
        "alex.example@example.invalid",
    ]
    pages = [_header_page(emails[i % 3], i) for i in range(12)]
    data = _make_pdf(pages)
    reason = looks_like_combined_export(data, _MIME_PDF)
    assert reason is not None
    assert "12" in reason
    assert "3" in reason
    assert "split it first" in reason
    assert "scripts/split-taleo.sh" in reason


def test_reason_never_contains_a_filename() -> None:
    """The function takes no filename at all; assert the reason doesn't leak
    one anyway (defensive — a future refactor must not thread one in)."""
    emails = [f"person{i}@example.invalid" for i in range(4)]
    pages = [_header_page(emails[i % 4], i) for i in range(9)]
    data = _make_pdf(pages)
    reason = looks_like_combined_export(data, _MIME_PDF)
    assert reason is not None
    assert ".pdf" not in reason.lower()


def test_eight_pages_two_distinct_emails_is_combined_at_the_boundary() -> None:
    emails = ["pat.example@example.invalid", "sam.example@example.invalid"]
    pages = [_header_page(emails[i % 2], i) for i in range(8)]
    data = _make_pdf(pages)
    assert looks_like_combined_export(data, _MIME_PDF) is not None


# ── looks_like_combined_export: the false-positive guard ────────────────────


def test_eight_page_single_applicant_same_email_every_page_is_none() -> None:
    """A genuinely long single résumé whose contact block repeats the SAME
    email on every page must never be flagged combined — this is the whole
    point of counting DISTINCT emails."""
    email = "pat.example@example.invalid"
    pages = [_header_page(email, 0) for _ in range(8)]
    data = _make_pdf(pages)
    assert looks_like_combined_export(data, _MIME_PDF) is None


def test_seven_pages_two_distinct_emails_is_below_the_boundary_none() -> None:
    emails = ["pat.example@example.invalid", "sam.example@example.invalid"]
    pages = [_header_page(emails[i % 2], i) for i in range(7)]
    data = _make_pdf(pages)
    assert looks_like_combined_export(data, _MIME_PDF) is None


# ── looks_like_combined_export: page-count floor ─────────────────────────────


def test_two_page_pdf_with_two_emails_is_none() -> None:
    emails = ["pat.example@example.invalid", "sam.example@example.invalid"]
    pages = [_header_page(emails[i], i) for i in range(2)]
    data = _make_pdf(pages)
    assert looks_like_combined_export(data, _MIME_PDF) is None


def test_six_page_pdf_with_three_emails_is_still_none_below_the_page_floor() -> None:
    """Page-count floor applies FIRST — three distinct emails on a 6-page
    document (e.g. three separate short cover notes) is not enough alone."""
    emails = [
        "pat.example@example.invalid",
        "sam.example@example.invalid",
        "alex.example@example.invalid",
    ]
    pages = [_header_page(emails[i % 3], i) for i in range(6)]
    data = _make_pdf(pages)
    assert looks_like_combined_export(data, _MIME_PDF) is None


# ── looks_like_combined_export: never raise ──────────────────────────────────


def test_encrypted_pdf_is_none_not_raise() -> None:
    data = _make_encrypted_pdf_bytes()
    assert looks_like_combined_export(data, _MIME_PDF) is None


def test_zero_text_pdf_is_none() -> None:
    doc = fitz.open()
    for _ in range(12):
        doc.new_page()  # blank pages, no text at all
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    assert looks_like_combined_export(buf.getvalue(), _MIME_PDF) is None


def test_unreadable_garbage_pdf_mime_is_none_not_raise() -> None:
    garbage = b"not actually a pdf" * 50
    assert looks_like_combined_export(garbage, _MIME_PDF) is None


# ── looks_like_combined_export: non-PDF mime never opened ───────────────────


def test_docx_mime_is_none() -> None:
    fake = b"PK\x03\x04" + b"\x00" * 100  # zip-shaped, but not a PDF
    assert looks_like_combined_export(fake, _MIME_DOCX) is None


def test_txt_mime_is_none() -> None:
    assert looks_like_combined_export(b"plain text resume", _MIME_TXT) is None


def test_non_pdf_mime_with_garbage_bytes_never_raises() -> None:
    """Bytes that would crash a PDF parser must never even be handed to one
    when the mime says non-PDF."""
    garbage = b"\x00\xff" * 10_000
    assert looks_like_combined_export(garbage, _MIME_TXT) is None


# ── is_cover_letter_text ─────────────────────────────────────────────────────


def test_dear_salutation_is_cover_letter_text() -> None:
    assert is_cover_letter_text("Dear Hiring Manager,\n\nI am pleased to apply...")


def test_sincerely_signoff_is_cover_letter_text() -> None:
    assert is_cover_letter_text(
        "It would be an honour to join your team.\n\nSincerely,\nPat Example"
    )


def test_i_am_writing_to_apply_is_cover_letter_text() -> None:
    assert is_cover_letter_text(
        "I am writing to apply for the Software Engineer position."
    )


def test_plain_resume_text_is_not_cover_letter_text() -> None:
    resume = (
        "Pat Example | pat.example@example.invalid | 604-555-0100\n"
        "EXPERIENCE\nSenior Engineer, Acme Corp, 2019-2026\n"
        "SKILLS\nPython, SQL, distributed systems\n"
        "EDUCATION\nBSc Computer Science"
    )
    assert is_cover_letter_text(resume) is False


def test_empty_text_is_not_cover_letter_text() -> None:
    assert is_cover_letter_text("") is False
