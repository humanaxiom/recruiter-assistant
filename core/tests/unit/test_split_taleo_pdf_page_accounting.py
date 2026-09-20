"""Page accounting for the Taleo PDF splitter, tested.

``core/scripts`` is linted and type-checked but not covered by the suite —
these tools are exercised by hand against real exports (see
``scripts/split-taleo.ps1``/``.sh``). ``page_accounting`` and ``parse_ranges``
are pure functions of page lists, which is why a small test is natural here:
a silently-dropped or doubly-assigned page is exactly the defect this repo's
prose-only-invariant pattern produces, and these two are cheap to pin down.
"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path
from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "split_taleo_pdf.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_split_taleo_pdf", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _module()


def test_page_accounting_clean_when_every_page_assigned_exactly_once() -> None:
    missing, duplicated = _MOD.page_accounting(6, [[1, 2], [3, 4], [5, 6]])
    assert missing == []
    assert duplicated == []


def test_page_accounting_reports_a_page_assigned_to_no_applicant() -> None:
    # Page 5 (of 6) never appears in any applicant's written pages.
    missing, duplicated = _MOD.page_accounting(6, [[1, 2], [3, 4], [6]])
    assert missing == [5]
    assert duplicated == []


def test_page_accounting_reports_a_page_assigned_to_two_applicants() -> None:
    # Page 3 was written to both applicant 1 and applicant 2.
    missing, duplicated = _MOD.page_accounting(4, [[1, 2, 3], [3, 4]])
    assert missing == []
    assert duplicated == [3]


def test_page_accounting_reports_duplicate_within_one_applicant() -> None:
    # Page 2 written twice to the SAME applicant (e.g. résumé + cover letter).
    missing, duplicated = _MOD.page_accounting(3, [[1, 2, 2], [3]])
    assert missing == []
    assert duplicated == [2]


def test_parse_ranges_under_coverage_shows_up_as_missing_pages() -> None:
    # "1-2;5-6" on a 6-page doc never mentions pages 3-4.
    applicants = _MOD.parse_ranges("1-2;5-6", 6)
    written = [
        [p + 1 for lo, hi in parts for p in range(lo, hi + 1)] for parts in applicants
    ]
    missing, duplicated = _MOD.page_accounting(6, written)
    assert missing == [3, 4]
    assert duplicated == []


def test_report_page_accounting_true_when_clean() -> None:
    assert _MOD.report_page_accounting([], []) is True


def test_report_page_accounting_false_when_dirty() -> None:
    assert _MOD.report_page_accounting([5], [3]) is False


# ── LLM-mode zip contents: PDFs only, never manifest.json ───────────────────
#
# The in-app paired uploader (Feature 2/resumes.py) accepts a
# ``pairing_manifest`` as its OWN multipart field; a ``manifest.json`` zipped
# alongside the résumés trips the zip allowlist (json isn't an accepted
# résumé extension) and rejects the WHOLE upload with a 400 (see
# ``test_upload_resumes_manifest_in_zip_rejected_with_guidance`` in
# ``test_route_resumes.py``). So the splitter's LLM-mode zip must contain
# résumé/cover PDFs ONLY — never the manifest — even though the manifest is
# written to the SAME output directory.


def test_zip_outputs_contains_only_the_given_resume_and_cover_pdfs(
    tmp_path: Path,
) -> None:
    resume1 = tmp_path / "001_resume.pdf"
    resume1.write_bytes(b"%PDF-1.4 resume one")
    resume2 = tmp_path / "002_resume.pdf"
    resume2.write_bytes(b"%PDF-1.4 resume two")
    cover1 = tmp_path / "001_cover_letter.pdf"
    cover1.write_bytes(b"%PDF-1.4 cover one")

    zip_path = _MOD._zip_outputs(tmp_path, [resume1, resume2], [cover1])

    assert zip_path == tmp_path / "applicants.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert names == {"001_resume.pdf", "002_resume.pdf", "001_cover_letter.pdf"}


def test_zip_outputs_excludes_manifest_json_even_when_present_in_out_dir(
    tmp_path: Path,
) -> None:
    resume1 = tmp_path / "001_resume.pdf"
    resume1.write_bytes(b"%PDF-1.4 resume one")
    # A real manifest.json sits in the SAME directory (as _write_pairing_manifest
    # writes it) — the zip helper must never pick it up implicitly.
    (tmp_path / "manifest.json").write_text('{"applicants": []}', encoding="utf-8")

    zip_path = _MOD._zip_outputs(tmp_path, [resume1], [])

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert "manifest.json" not in names
    assert names == {"001_resume.pdf"}


def test_zip_outputs_handles_no_covers() -> None:
    def _run(tmp_path: Path) -> None:
        resume1 = tmp_path / "001_resume.pdf"
        resume1.write_bytes(b"%PDF-1.4 resume one")
        zip_path = _MOD._zip_outputs(tmp_path, [resume1], [])
        with zipfile.ZipFile(zip_path) as zf:
            assert set(zf.namelist()) == {"001_resume.pdf"}

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        _run(Path(d))


# ── report_cover_only: a cover-only applicant is flagged, not silently kept ─
#
# The LLM manifest occasionally emits a row with cover-letter pages but NO
# résumé pages at all (e.g. a mis-segmented boundary). Such a row has no
# résumé to ingest, so it is excluded from the pairing manifest (already true
# of ``_write_pairing_manifest``'s ``resume_path is not None`` filter) AND
# must be surfaced to the operator — ``report_cover_only`` counts these rows
# so the CLI can exit non-zero rather than silently dropping an applicant.


def test_report_cover_only_counts_rows_with_no_resume_path(tmp_path: Path) -> None:
    cover_path = tmp_path / "002_cover_letter.pdf"
    emitted = [
        ("Pat Example", tmp_path / "001_resume.pdf", None, [1, 2]),
        ("Sam Example", None, cover_path, [3]),
    ]
    assert _MOD.report_cover_only(emitted) == 1


def test_report_cover_only_counts_multiple_cover_only_rows(tmp_path: Path) -> None:
    emitted = [
        ("A", None, tmp_path / "a_cover_letter.pdf", [1]),
        ("B", None, tmp_path / "b_cover_letter.pdf", [2]),
        ("C", tmp_path / "c_resume.pdf", None, [3]),
    ]
    assert _MOD.report_cover_only(emitted) == 2


def test_report_cover_only_zero_when_every_applicant_has_a_resume(
    tmp_path: Path,
) -> None:
    emitted = [
        ("A", tmp_path / "a_resume.pdf", tmp_path / "a_cover_letter.pdf", [1, 2]),
        ("B", tmp_path / "b_resume.pdf", None, [3]),
    ]
    assert _MOD.report_cover_only(emitted) == 0


def test_report_cover_only_zero_for_empty_emitted_list() -> None:
    assert _MOD.report_cover_only([]) == 0


# ── _zippable: a cover-only row is excluded from BOTH lists (reviewer MAJOR,
# 2026-09-18) — before this helper existed, ``_run_llm_mode`` built ``covers``
# from every emitted row with a cover path regardless of whether that row also
# had a résumé, so a cover-only applicant's cover letter (real candidate PII)
# was zipped into ``applicants.zip`` despite ``report_cover_only`` printing
# that such applicants "are EXCLUDED from manifest.json and applicants.zip".


def test_zippable_excludes_a_cover_only_row_from_covers(tmp_path: Path) -> None:
    resume_a = tmp_path / "001_resume.pdf"
    cover_a = tmp_path / "001_cover_letter.pdf"
    cover_only = tmp_path / "002_cover_letter.pdf"
    emitted = [
        ("A", resume_a, cover_a, [1, 2]),
        ("B (cover only)", None, cover_only, [3]),
    ]
    resumes, covers = _MOD._zippable(emitted)
    assert resumes == [resume_a]
    assert covers == [cover_a]
    assert cover_only not in covers


def test_zippable_excludes_a_cover_only_row_even_when_it_sorts_first(
    tmp_path: Path,
) -> None:
    cover_only = tmp_path / "001_cover_letter.pdf"
    resume_b = tmp_path / "002_resume.pdf"
    emitted = [
        ("A (cover only)", None, cover_only, [1]),
        ("B", resume_b, None, [2]),
    ]
    resumes, covers = _MOD._zippable(emitted)
    assert resumes == [resume_b]
    assert covers == []


def test_zippable_keeps_a_resume_with_no_cover(tmp_path: Path) -> None:
    resume_only = tmp_path / "001_resume.pdf"
    emitted = [("A", resume_only, None, [1])]
    resumes, covers = _MOD._zippable(emitted)
    assert resumes == [resume_only]
    assert covers == []


# ── merged_applicant_files: a résumé PDF carrying 2+ distinct applicant
# emails means the LLM merged two people into one file — page accounting
# can't see this because every page IS assigned, just to the wrong file.
# 2026-09-19 finding: a 4-page résumé file carried two applicants' emails.


def test_merged_applicant_files_reports_two_distinct_emails(tmp_path: Path) -> None:
    resume = tmp_path / "001_pat_example_resume.pdf"
    resume.write_bytes(b"%PDF-1.4")

    def fake_page_texts(path: Path) -> list[str]:
        assert path == resume
        return ["contact pat@example.com", "contact sam@example.com"]

    rows = _MOD.merged_applicant_files(
        [("Pat Example", resume, None, [1, 2])], fake_page_texts
    )
    assert rows == [("Pat Example", 2)]


def test_merged_applicant_files_not_reported_for_same_email_twice(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "001_pat_example_resume.pdf"
    resume.write_bytes(b"%PDF-1.4")

    def fake_page_texts(path: Path) -> list[str]:
        return ["contact Pat@Example.com", "again pat@example.com"]

    rows = _MOD.merged_applicant_files(
        [("Pat Example", resume, None, [1, 2])], fake_page_texts
    )
    assert rows == []


def test_merged_applicant_files_not_reported_when_no_emails(tmp_path: Path) -> None:
    resume = tmp_path / "001_pat_example_resume.pdf"
    resume.write_bytes(b"%PDF-1.4")

    def fake_page_texts(path: Path) -> list[str]:
        return ["no contact info here", "still nothing"]

    rows = _MOD.merged_applicant_files(
        [("Pat Example", resume, None, [1, 2])], fake_page_texts
    )
    assert rows == []


def test_merged_applicant_files_only_scans_resume_files_not_cover_letters(
    tmp_path: Path,
) -> None:
    resume = tmp_path / "001_pat_resume.pdf"
    resume.write_bytes(b"%PDF-1.4")
    cover = tmp_path / "001_pat_cover_letter.pdf"
    cover.write_bytes(b"%PDF-1.4")

    def fake_page_texts(path: Path) -> list[str]:
        if path == cover:
            return ["Dear recruiter@company.com, ...", "sincerely, another@company.com"]
        return ["contact pat@example.com"]

    rows = _MOD.merged_applicant_files(
        [("Pat", resume, cover, [1, 2])], fake_page_texts
    )
    assert rows == []


def test_merged_applicant_files_two_files_one_merged(tmp_path: Path) -> None:
    resume_a = tmp_path / "001_pat_resume.pdf"
    resume_a.write_bytes(b"%PDF-1.4")
    resume_b = tmp_path / "002_sam_resume.pdf"
    resume_b.write_bytes(b"%PDF-1.4")

    def fake_page_texts(path: Path) -> list[str]:
        if path == resume_a:
            return ["pat@example.com", "different@example.com"]
        return ["sam@example.com"]

    rows = _MOD.merged_applicant_files(
        [("Pat", resume_a, None, [1, 2]), ("Sam", resume_b, None, [3])],
        fake_page_texts,
    )
    assert rows == [("Pat", 2)]


def test_report_merged_applicants_returns_count_and_prints(
    capsys: object,
) -> None:
    count = _MOD.report_merged_applicants([("001_pat_resume.pdf", 2)])
    assert count == 1
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "PROBABLE MERGED APPLICANT" in out
    assert "001_pat_resume.pdf" in out
    assert "2 distinct applicant emails" in out


def test_report_merged_applicants_zero_for_empty_rows() -> None:
    assert _MOD.report_merged_applicants([]) == 0
