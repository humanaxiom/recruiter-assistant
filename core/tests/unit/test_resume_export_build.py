"""RED -- slice H3: ``build_export`` caps, missing files and the manifest warning.

Contract pinned for ``src.services.resume_export_service``:

* ``ExportApplicant.resume_pdf`` may be ``None`` (the stored file is missing)
  and ``ExportApplicant`` gains ``cover_missing: bool = False``. A missing file
  never fails the export: the applicant is listed in the manifest on a line
  starting with its ``applicant-NNN`` label and containing ``file missing``,
  numbering of the others is unchanged, and no entry is written for the
  missing file.
* ``build_export(applicants, *, blind_review, max_pages_per_file=None)`` -- a
  file with MORE pages than the cap (``None`` -> the setting
  ``resume_export_max_pages_per_file``, default 20) is marked not anonymized
  and, on a non-blind job, still included as-is.
* The manifest of a non-blind export carries a warning line telling the reader
  not to forward the ``as-is`` folder to blind reviewers.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import fitz  # type: ignore[import-untyped]
import pytest

NAME = "Testy McTestface"
TERMS = [NAME, "Testy", "McTestface"]


def _pdf(pages: int = 1) -> bytes:
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page()
        lines = [NAME, "testy@example.invalid"] if n == 0 else []
        lines.append(f"Experienced in recruiting, page {n + 1}.")
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + i * 16), line, fontsize=11)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _applicant(**kw: Any) -> Any:
    from src.services.resume_export_service import ExportApplicant

    base: dict[str, Any] = {"resume_pdf": _pdf(), "cover_pdf": None, "terms": TERMS}
    base.update(kw)
    return ExportApplicant(**base)


def _build(applicants: list[Any], **kw: Any) -> zipfile.ZipFile:
    from src.services.resume_export_service import build_export

    kw.setdefault("blind_review", False)
    return zipfile.ZipFile(io.BytesIO(build_export(applicants, **kw).data))


def _lines(zf: zipfile.ZipFile) -> list[str]:
    return zf.read("MANIFEST.txt").decode("utf-8").splitlines()


def _line(zf: zipfile.ZipFile, label: str) -> str:
    return next(ln for ln in _lines(zf) if ln.startswith(label))


# ----------------------------------------------------------- D: warning


def test_manifest_warns_not_to_forward_the_as_is_folder_to_blind_reviewers() -> None:
    zf = _build([_applicant()])

    warnings = [
        ln
        for ln in _lines(zf)
        if "do not forward" in ln.lower() and "as-is" in ln.lower()
    ]
    assert len(warnings) == 1
    assert "blind" in warnings[0].lower()


def test_the_warning_does_not_disturb_the_count_and_applicant_lines() -> None:
    zf = _build([_applicant(), _applicant(terms=None)])
    lines = _lines(zf)

    assert "applicants: 2" in lines
    assert "anonymized: 1" in lines
    assert "not anonymized: 1" in lines
    assert _line(zf, "applicant-002")


# ----------------------------------------------------- C7: missing files


def test_a_missing_resume_is_listed_and_does_not_fail_the_export() -> None:
    zf = _build([_applicant(), _applicant(resume_pdf=None), _applicant()])
    names = set(zf.namelist())

    assert "file missing" in _line(zf, "applicant-002").lower()
    assert "file missing" not in _line(zf, "applicant-001").lower()
    assert "file missing" not in _line(zf, "applicant-003").lower()
    assert {
        "as-is/applicant-001_resume.pdf",
        "anonymized/applicant-001_resume.pdf",
        "as-is/applicant-003_resume.pdf",
        "anonymized/applicant-003_resume.pdf",
    } <= names
    assert not [n for n in names if "applicant-002" in n]
    lines = _lines(zf)
    assert "applicants: 3" in lines
    assert "anonymized: 2" in lines
    assert "not anonymized: 1" in lines


def test_a_missing_resume_with_a_cover_keeps_the_cover_as_is() -> None:
    zf = _build([_applicant(resume_pdf=None, cover_pdf=_pdf())])

    assert "file missing" in _line(zf, "applicant-001").lower()
    assert "as-is/applicant-001_cover_letter.pdf" in zf.namelist()
    assert "as-is/applicant-001_resume.pdf" not in zf.namelist()


def test_a_missing_cover_is_listed_and_the_resume_still_ships() -> None:
    zf = _build([_applicant(cover_missing=True)])
    names = set(zf.namelist())

    assert "file missing" in _line(zf, "applicant-001").lower()
    assert "as-is/applicant-001_resume.pdf" in names
    assert "anonymized/applicant-001_resume.pdf" in names
    assert not [n for n in names if "cover_letter" in n]


def test_a_missing_resume_on_a_blind_job_is_listed_and_ships_nothing() -> None:
    zf = _build([_applicant(resume_pdf=None)], blind_review=True)

    assert zf.namelist() == ["MANIFEST.txt"]
    assert "file missing" in _line(zf, "applicant-001").lower()


def test_a_missing_resume_is_never_counted_as_anonymized() -> None:
    zf = _build([_applicant(resume_pdf=None, terms=None)])
    lines = _lines(zf)

    assert "anonymized: 0" in lines
    assert "not anonymized: 1" in lines


# ------------------------------------------------------ B3: the page cap


def test_a_file_over_the_page_cap_is_not_anonymized_but_still_ships_as_is() -> None:
    big = _pdf(3)
    zf = _build([_applicant(resume_pdf=big)], max_pages_per_file=2)
    names = set(zf.namelist())

    assert "as-is/applicant-001_resume.pdf" in names
    assert zf.read("as-is/applicant-001_resume.pdf") == big
    assert "anonymized/applicant-001_resume.pdf" not in names
    assert "not anonymized" in _line(zf, "applicant-001").lower()
    assert "not anonymized: 1" in _lines(zf)


def test_a_file_exactly_at_the_cap_is_anonymized() -> None:
    zf = _build([_applicant(resume_pdf=_pdf(2))], max_pages_per_file=2)

    assert "anonymized/applicant-001_resume.pdf" in zf.namelist()
    assert "anonymized: 1" in _lines(zf)


def test_a_cover_over_the_cap_is_not_anonymized_and_keeps_the_resume_copy() -> None:
    zf = _build([_applicant(cover_pdf=_pdf(3))], max_pages_per_file=2)
    names = set(zf.namelist())

    assert "anonymized/applicant-001_resume.pdf" in names
    assert "anonymized/applicant-001_cover_letter.pdf" not in names
    assert "as-is/applicant-001_cover_letter.pdf" in names
    assert "not anonymized" in _line(zf, "applicant-001").lower()


def test_an_over_cap_file_on_a_blind_job_is_omitted_entirely() -> None:
    big = _applicant(resume_pdf=_pdf(3))
    zf = _build([big], blind_review=True, max_pages_per_file=2)

    assert zf.namelist() == ["MANIFEST.txt"]
    assert "not anonymized: 1" in _lines(zf)


@pytest.mark.parametrize(("pages", "anonymized"), [(20, True), (21, False)])
def test_the_default_cap_comes_from_settings_and_is_twenty(
    pages: int, anonymized: bool
) -> None:
    zf = _build([_applicant(resume_pdf=_pdf(pages))])

    assert ("anonymized/applicant-001_resume.pdf" in zf.namelist()) is anonymized
