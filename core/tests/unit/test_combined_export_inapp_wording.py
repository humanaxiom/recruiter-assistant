"""RED -- slice E1: the combined-export refusal points at the in-app screen.

Until the in-app split existed, the refusal told users to run a shell script
(``scripts/split-taleo.sh``). Recruiters cannot run that; the product now has a
"Split a combined Taleo export" control on the job page, and the refusal must
send them there instead.

NOTE for the coder: ``test_combined_export_detection.py`` still asserts the
OLD wording (``"scripts/split-taleo.sh" in reason``, line ~119). That assertion
contradicts this file by design and must be changed in the green commit.
"""

from __future__ import annotations

from io import BytesIO

import fitz  # type: ignore[import-untyped]

from src.services.combined_export import looks_like_combined_export

_MIME_PDF = "application/pdf"


def _combined_pdf(pages: int = 12, applicants: int = 3) -> bytes:
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page()
        lines = [
            f"Applicant {i % applicants}",
            f"person{i % applicants}@example.invalid",
            "604-555-0100",
            "RESUME",
        ]
        for n, line in enumerate(lines):
            page.insert_text((72, 72 + n * 14), line, fontsize=10)
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _reason() -> str:
    reason = looks_like_combined_export(_combined_pdf(), _MIME_PDF)
    assert reason is not None
    return reason


def test_refusal_no_longer_mentions_the_shell_script() -> None:
    assert "split-taleo" not in _reason()
    assert "scripts/" not in _reason()


def test_refusal_names_the_in_app_control() -> None:
    assert "Split a combined Taleo export" in _reason()


def test_refusal_still_reports_page_and_applicant_counts() -> None:
    reason = _reason()
    assert "12" in reason
    assert "3" in reason


def test_refusal_mentions_the_job_page() -> None:
    assert "job page" in _reason().lower()


def test_refusal_never_contains_a_filename_or_an_email() -> None:
    reason = _reason()
    assert ".pdf" not in reason.lower()
    assert "@" not in reason
