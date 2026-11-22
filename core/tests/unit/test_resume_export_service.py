"""RED -- slice H: ``src.services.resume_export_service`` (PDF redaction + ZIP).

Contract pinned for the coder (sync, pure-ish; the route runs them in a thread):

* ``NotAnonymizedError(Exception)`` -- raised by ``redact_pdf`` when a redacted
  copy cannot be vouched for (a term / email / phone survives re-extraction,
  or the PDF has no text layer).
* ``redact_pdf(pdf_bytes: bytes, terms: list[str]) -> bytes`` -- PyMuPDF
  redaction annotations APPLIED (text removed, not covered), for every term
  (case-insensitive) plus every email / phone match; page count preserved;
  re-extracts text afterwards and fails closed.
* ``collect_terms(candidate_name: str | None, parsed: dict | None)
  -> list[str] | None`` -- ``None`` when there is no parsed name (unparsed
  résumé). Otherwise the full name, each name token of >= 3 characters, every
  ``experience[].company`` and every ``education[].institution`` (these are
  the same inputs ``redaction.blind_label_map`` uses).
* ``ExportApplicant(resume_pdf: bytes, cover_pdf: bytes | None,
  terms: list[str] | None)`` -- ``terms is None`` means "unparsed".
* ``build_export_zip(applicants: list[ExportApplicant], *, blind_review: bool)
  -> bytes`` -- applicants are numbered 1..N in list order (the route orders by
  creation); entries ``as-is/applicant-NNN_resume.pdf`` / ``_cover_letter.pdf``,
  the same under ``anonymized/`` (only when verified), and ``MANIFEST.txt``
  with the exact count lines ``applicants: N``, ``anonymized: N`` and
  ``not anonymized: N`` plus one line per applicant starting with its
  ``applicant-NNN`` label and containing ``not anonymized`` iff the applicant
  did not get a full anonymized copy. Blind jobs get no ``as-is/`` entries.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

import fitz  # type: ignore[import-untyped]
import pytest

NAME = "Testy McTestface"
EMAIL = "testy.mctestface@example.invalid"
PHONE = "604-555-0142"
EMPLOYER = "Zorblax Analytics"
SCHOOL = "Quuxville Institute"
ORDINARY = ["Experienced", "recruiting", "operations", "stakeholders"]


def _make_pdf(pages: list[list[str]]) -> bytes:
    doc = fitz.open()
    for lines in pages:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + i * 16), line, fontsize=11)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _resume_pdf() -> bytes:
    return _make_pdf(
        [
            [
                NAME,
                f"{EMAIL}  {PHONE}",
                "Experienced in recruiting and operations.",
                f"Worked at {EMPLOYER} with stakeholders.",
            ],
            [f"Studied at {SCHOOL}.", f"Reference: {NAME.upper()} on file."],
        ]
    )


def _cover_pdf() -> bytes:
    return _make_pdf(
        [["Dear Hiring Manager,", f"I am {NAME}. Reach me at {EMAIL}.", "Thanks."]]
    )


def _image_only_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
    pix.clear_with(200)
    page.insert_image(fitz.Rect(72, 72, 200, 200), pixmap=pix)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _text(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return " ".join(p.get_text() for p in doc)
    finally:
        doc.close()


def _flat(pdf: bytes) -> str:
    return re.sub(r"\s+", " ", _text(pdf)).lower()


TERMS = [NAME, EMPLOYER, SCHOOL]


# --------------------------------------------------------------- redact_pdf


def test_redact_pdf_removes_terms_email_and_phone_but_keeps_ordinary_text() -> None:
    from src.services.resume_export_service import redact_pdf

    out = redact_pdf(_resume_pdf(), TERMS)

    flat = _flat(out)
    for secret in (NAME, EMAIL, PHONE, EMPLOYER, SCHOOL):
        assert secret.lower() not in flat, secret
    assert "555-0142" not in flat
    for word in ORDINARY:
        assert word.lower() in flat, word


def test_redact_pdf_is_case_insensitive_for_terms() -> None:
    from src.services.resume_export_service import redact_pdf

    # The fixture prints the name upper-cased on page 2.
    out = redact_pdf(_resume_pdf(), [NAME])
    assert "testface" not in _flat(out)


def test_redact_pdf_returns_a_pdf() -> None:
    from src.services.resume_export_service import redact_pdf

    out = redact_pdf(_resume_pdf(), TERMS)

    # The anonymized copy is rebuilt as reflowed text (user decision), so the
    # original page count is not preserved; it must still be a readable PDF.
    assert out.startswith(b"%PDF")
    doc = fitz.open(stream=out, filetype="pdf")
    assert doc.page_count >= 1
    doc.close()


def test_redact_pdf_removes_text_rather_than_covering_it() -> None:
    """Search finds nothing AND no raw bytes of the name survive in any
    extractable text span (a black box over live text would still extract)."""
    from src.services.resume_export_service import redact_pdf

    out = redact_pdf(_cover_pdf(), [NAME])

    doc = fitz.open(stream=out, filetype="pdf")
    try:
        assert doc[0].search_for("McTestface") == []
        spans = [
            s["text"]
            for b in doc[0].get_text("dict")["blocks"]
            for ln in b.get("lines", [])
            for s in ln["spans"]
        ]
    finally:
        doc.close()
    assert not any("mctestface" in s.lower() for s in spans)


def test_redact_pdf_without_a_text_layer_is_not_anonymized() -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    with pytest.raises(NotAnonymizedError):
        redact_pdf(_image_only_pdf(), TERMS)


def test_redact_pdf_rejects_non_pdf_bytes_as_not_anonymized() -> None:
    from src.services.resume_export_service import NotAnonymizedError, redact_pdf

    with pytest.raises(NotAnonymizedError):
        redact_pdf(b"this is not a pdf", TERMS)


# ------------------------------------------------------------ collect_terms


def _parsed() -> dict[str, Any]:
    return {
        "candidate": {"name": NAME},
        "experience": [{"company": EMPLOYER, "title": "Analyst"}],
        "education": [{"institution": SCHOOL, "degree": "BSc"}],
    }


def test_collect_terms_has_name_tokens_employers_and_schools() -> None:
    from src.services.resume_export_service import collect_terms

    terms = collect_terms(NAME, _parsed())

    assert terms is not None
    lowered = {t.lower() for t in terms}
    assert {NAME.lower(), EMPLOYER.lower(), SCHOOL.lower()} <= lowered
    assert {"testy", "mctestface"} <= lowered


@pytest.mark.parametrize("name", [None, "", "   "])
def test_collect_terms_is_none_when_there_is_no_name(name: str | None) -> None:
    from src.services.resume_export_service import collect_terms

    assert collect_terms(name, _parsed()) is None
    assert collect_terms(name, None) is None


def test_collect_terms_with_a_name_but_no_parsed_body_still_has_the_name() -> None:
    from src.services.resume_export_service import collect_terms

    terms = collect_terms(NAME, None)
    assert terms is not None
    assert NAME.lower() in {t.lower() for t in terms}


# --------------------------------------------------------- build_export_zip


def _zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _manifest(zf: zipfile.ZipFile) -> str:
    return zf.read("MANIFEST.txt").decode("utf-8")


def _applicant(**kw: Any) -> Any:
    from src.services.resume_export_service import ExportApplicant

    base: dict[str, Any] = {
        "resume_pdf": _resume_pdf(),
        "cover_pdf": None,
        "terms": TERMS,
    }
    base.update(kw)
    return ExportApplicant(**base)


def test_zip_layout_for_applicants_with_and_without_a_cover() -> None:
    from src.services.resume_export_service import build_export_zip

    data = build_export_zip(
        [_applicant(cover_pdf=_cover_pdf()), _applicant()], blind_review=False
    )

    names = set(_zip(data).namelist())
    assert names == {
        "MANIFEST.txt",
        "as-is/applicant-001_resume.pdf",
        "as-is/applicant-001_cover_letter.pdf",
        "anonymized/applicant-001_resume.pdf",
        "anonymized/applicant-001_cover_letter.pdf",
        "as-is/applicant-002_resume.pdf",
        "anonymized/applicant-002_resume.pdf",
    }


def test_as_is_entries_are_byte_identical_and_anonymized_are_clean() -> None:
    from src.services.resume_export_service import build_export_zip

    resume, cover = _resume_pdf(), _cover_pdf()
    applicant = _applicant(resume_pdf=resume, cover_pdf=cover)
    zf = _zip(build_export_zip([applicant], blind_review=False))

    assert zf.read("as-is/applicant-001_resume.pdf") == resume
    assert zf.read("as-is/applicant-001_cover_letter.pdf") == cover
    for entry in ("resume", "cover_letter"):
        flat = _flat(zf.read(f"anonymized/applicant-001_{entry}.pdf"))
        for secret in (NAME, EMAIL, PHONE, "mctestface"):
            assert secret.lower() not in flat


def test_numbering_follows_list_order_and_is_stable() -> None:
    from src.services.resume_export_service import build_export_zip

    first = _make_pdf([["First applicant marker alphaone", NAME]])
    second = _make_pdf([["Second applicant marker betatwo", NAME]])
    applicants = [_applicant(resume_pdf=first), _applicant(resume_pdf=second)]

    one = _zip(build_export_zip(applicants, blind_review=False))
    two = _zip(build_export_zip(applicants, blind_review=False))

    assert "alphaone" in _flat(one.read("as-is/applicant-001_resume.pdf"))
    assert "betatwo" in _flat(one.read("as-is/applicant-002_resume.pdf"))
    assert one.namelist() == two.namelist()


def test_manifest_has_counts_and_a_line_per_applicant() -> None:
    from src.services.resume_export_service import build_export_zip

    zf = _zip(
        build_export_zip(
            [_applicant(), _applicant(terms=None), _applicant()], blind_review=False
        )
    )
    lines = _manifest(zf).splitlines()

    assert "applicants: 3" in lines
    assert "anonymized: 2" in lines
    assert "not anonymized: 1" in lines
    per = {
        n: ln
        for n in ("001", "002", "003")
        for ln in lines
        if ln.startswith(f"applicant-{n}")
    }
    assert set(per) == {"001", "002", "003"}
    assert "not anonymized" in per["002"].lower()
    assert "not anonymized" not in per["001"].lower()
    assert "not anonymized" not in per["003"].lower()


def test_unparsed_applicant_gets_as_is_but_no_anonymized_entries() -> None:
    from src.services.resume_export_service import build_export_zip

    zf = _zip(
        build_export_zip(
            [_applicant(terms=None, cover_pdf=_cover_pdf())], blind_review=False
        )
    )
    names = set(zf.namelist())

    assert "as-is/applicant-001_resume.pdf" in names
    assert "as-is/applicant-001_cover_letter.pdf" in names
    assert not [n for n in names if n.startswith("anonymized/")]


def test_image_only_resume_is_listed_as_not_anonymized_and_omitted() -> None:
    from src.services.resume_export_service import build_export_zip

    zf = _zip(
        build_export_zip([_applicant(resume_pdf=_image_only_pdf())], blind_review=False)
    )

    assert "anonymized/applicant-001_resume.pdf" not in zf.namelist()
    assert "as-is/applicant-001_resume.pdf" in zf.namelist()
    assert "not anonymized: 1" in _manifest(zf).splitlines()


def test_unverifiable_cover_is_omitted_but_the_resume_copy_is_kept() -> None:
    from src.services.resume_export_service import build_export_zip

    zf = _zip(
        build_export_zip([_applicant(cover_pdf=_image_only_pdf())], blind_review=False)
    )
    names = set(zf.namelist())

    assert "anonymized/applicant-001_resume.pdf" in names
    assert "anonymized/applicant-001_cover_letter.pdf" not in names
    assert "as-is/applicant-001_cover_letter.pdf" in names
    line = next(
        ln for ln in _manifest(zf).splitlines() if ln.startswith("applicant-001")
    )
    assert "not anonymized" in line.lower()


def test_blind_review_zip_has_no_as_is_entries() -> None:
    from src.services.resume_export_service import build_export_zip

    zf = _zip(build_export_zip([_applicant(cover_pdf=_cover_pdf())], blind_review=True))
    names = set(zf.namelist())

    assert not [n for n in names if n.startswith("as-is/")]
    assert "anonymized/applicant-001_resume.pdf" in names
    assert "anonymized/applicant-001_cover_letter.pdf" in names


def test_blind_review_unanonymizable_applicant_is_omitted_entirely() -> None:
    from src.services.resume_export_service import build_export_zip

    zf = _zip(build_export_zip([_applicant(terms=None)], blind_review=True))

    assert zf.namelist() == ["MANIFEST.txt"]
    assert "not anonymized: 1" in _manifest(zf).splitlines()


def test_no_candidate_data_in_entry_names_or_manifest() -> None:
    from src.services.resume_export_service import build_export_zip

    zf = _zip(
        build_export_zip(
            [_applicant(cover_pdf=_cover_pdf()), _applicant(terms=None)],
            blind_review=False,
        )
    )
    blob = "\n".join(zf.namelist()) + "\n" + _manifest(zf)

    for secret in (NAME, EMAIL, PHONE, EMPLOYER, SCHOOL, "mctestface", "example"):
        assert secret.lower() not in blob.lower(), secret
    assert all(re.fullmatch(r"[A-Za-z0-9_./-]+", n) for n in zf.namelist())


def test_empty_applicant_list_yields_a_manifest_only_zip() -> None:
    from src.services.resume_export_service import build_export_zip

    zf = _zip(build_export_zip([], blind_review=False))

    assert zf.namelist() == ["MANIFEST.txt"]
    assert "applicants: 0" in _manifest(zf).splitlines()
