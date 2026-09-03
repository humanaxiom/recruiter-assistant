"""The backfill script's two decisions, tested.

``core/scripts`` is linted and type-checked but not covered by the suite —
these tools are exercised by hand against real exports. This one is the
exception, because it WRITES to a live database, and the thing it writes is a
judgement call about somebody's requisition titles. A dry run and a human's eye
are the primary control; these are the second.

Both helpers are pure functions of one row, which is why they were written that
way.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_job_fields.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_backfill_job_fields", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "20260612 00138559 APSA JDFN 20260612",
        "parsed_title": "Multimedia Specialist",
        "title_absent_from_jd": True,
        "location": None,
    }
    base.update(over)
    return base


# ── the title rule ──────────────────────────────────────────────────────────


def test_a_filename_stem_is_replaced_by_the_extracted_title() -> None:
    """The 23 pilot requisitions. Nothing is inferred — the real title has been
    sitting in ``description_parsed`` since they were first parsed."""
    assert _module()._title_fix(_row()) == "Multimedia Specialist"


def test_a_title_that_appears_in_the_jd_is_left_alone() -> None:
    """The load-bearing safety rule. A filename stem never appears in the body
    of the document it names; a title somebody typed almost always does. Where
    the signal is absent, doing nothing is the safe direction — an un-improved
    title costs a reader nothing, a silently renamed requisition costs trust."""
    assert _module()._title_fix(_row(title_absent_from_jd=False)) is None


def test_a_row_that_already_matches_is_not_rewritten() -> None:
    """Idempotence. Re-running --apply must be a no-op, or an operator cannot
    safely run it twice."""
    assert _module()._title_fix(_row(title="Multimedia Specialist")) is None


def test_an_unparsed_row_is_left_alone() -> None:
    """No extraction, nothing to copy. A failed JD parse must not lose its
    title as a side effect of a backfill."""
    assert _module()._title_fix(_row(parsed_title=None)) is None


# ── the campus rule ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("Burnaby, BC", "Burnaby"),
        ("bby", "Burnaby"),
        ("harbour centre", "Vancouver"),
    ],
)
def test_an_existing_location_is_canonicalised(stored: str, expected: str) -> None:
    assert _module()._location_fix(_row(location=stored)) == expected


@pytest.mark.parametrize("stored", [None, "", "   ", "Burnaby", "Remote"])
def test_nothing_is_invented_and_nothing_already_clean_is_rewritten(
    stored: str | None,
) -> None:
    """Two cases in one rule: an empty column stays empty — this script never
    guesses a campus, because the one pilot JD that mentions one mentions two
    as a description of scope — and a value that is already canonical (or
    already unrecognised, like "Remote") produces no write."""
    assert _module()._location_fix(_row(location=stored)) is None
