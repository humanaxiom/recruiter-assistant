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
