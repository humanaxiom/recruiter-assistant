"""RED — pins the contract for ``tests/e2e/roster.py``, the stress harness's
synthetic Taleo-roster CSV generator.

Not written here (Tester scope: tests only, never implementation) —
``tests/e2e/roster.py`` itself. Every test that imports it fails at
COLLECTION with ``ModuleNotFoundError`` until it exists.

The generated CSV is proven correct the only way that matters: by round-
tripping through the REAL ``parse_candidate_csv`` (the exact parser
``candidate_roster_service`` feeds), never by re-implementing its column
mapping in the test.
"""

from __future__ import annotations

import pytest

from src.services.bulk_ingest_service import parse_candidate_csv
from src.services.candidate_roster_service import _normalize_name
from tests.e2e.roster import RosterSpec, build_roster_csv


def _row(spec: RosterSpec) -> object:
    parsed = parse_candidate_csv(build_roster_csv([spec]).encode("utf-8"))
    assert len(parsed) == 1
    return parsed[0]


# ── work authorization mapping, round-tripped through the real parser ──────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("No restrictions", "eligible"),
        ("Study Permit", "not_eligible"),
        ("Work Permit", "eligible"),
        ("Other", "unknown"),
    ],
)
def test_work_authorization_round_trips_via_the_real_parser(
    text: str, expected: str
) -> None:
    spec = RosterSpec(
        name="Test Candidate",
        email="test.candidate@example.invalid",
        work_authorization_text=text,
        apsa=False,
        cupe=False,
    )
    row = _row(spec)
    assert row.work_authorization == expected  # type: ignore[attr-defined]


def test_work_authorization_source_carries_the_raw_declaration() -> None:
    spec = RosterSpec(
        name="Test Candidate",
        email="test.candidate@example.invalid",
        work_authorization_text="No restrictions",
        apsa=False,
        cupe=False,
    )
    row = _row(spec)
    assert row.work_authorization_source == "No restrictions"  # type: ignore[attr-defined]


# ── APSA/CUPE internal flags ────────────────────────────────────────────────


@pytest.mark.parametrize("flag_name", ["apsa", "cupe"])
def test_internal_flag_true_only_when_marked_i(flag_name: str) -> None:
    kwargs = {
        "name": "Test Candidate",
        "email": "test.candidate@example.invalid",
        "work_authorization_text": "No restrictions",
        "apsa": flag_name == "apsa",
        "cupe": flag_name == "cupe",
    }
    row = _row(RosterSpec(**kwargs))  # type: ignore[arg-type]
    assert getattr(row, f"internal_{flag_name}") is True
    other = "cupe" if flag_name == "apsa" else "apsa"
    assert getattr(row, f"internal_{other}") is False


def test_neither_internal_flag_set_when_both_false() -> None:
    spec = RosterSpec(
        name="Test Candidate",
        email="test.candidate@example.invalid",
        work_authorization_text="No restrictions",
        apsa=False,
        cupe=False,
    )
    row = _row(spec)
    assert row.internal_apsa is False  # type: ignore[attr-defined]
    assert row.internal_cupe is False  # type: ignore[attr-defined]


# ── CSV quoting ──────────────────────────────────────────────────────────


def test_a_name_containing_a_comma_round_trips_exactly() -> None:
    spec = RosterSpec(
        name="Example, Pat",
        email="pat.example@example.invalid",
        work_authorization_text="No restrictions",
        apsa=False,
        cupe=False,
    )
    row = _row(spec)
    assert row.name == "Example, Pat"  # type: ignore[attr-defined]


def test_build_roster_csv_produces_one_row_per_spec() -> None:
    specs = [
        RosterSpec(
            name=f"Candidate {i}",
            email=f"candidate{i}@example.invalid",
            work_authorization_text="No restrictions",
            apsa=False,
            cupe=False,
        )
        for i in range(5)
    ]
    parsed = parse_candidate_csv(build_roster_csv(specs).encode("utf-8"))
    assert len(parsed) == 5
    assert [r.name for r in parsed] == [s.name for s in specs]  # type: ignore[attr-defined]


def test_build_roster_csv_returns_a_str() -> None:
    csv_text = build_roster_csv(
        [
            RosterSpec(
                name="Solo Candidate",
                email="solo@example.invalid",
                work_authorization_text="No restrictions",
                apsa=False,
                cupe=False,
            )
        ]
    )
    assert isinstance(csv_text, str)
    assert "Solo Candidate" in csv_text


# ── TODAY's finding: a credential suffix defeats the exact name-token match ─


@pytest.mark.xfail(
    strict=True,
    reason=(
        "recorded 2026-09-17: _normalize_name splits on non-letters, so a "
        "trailing credential ('PMP', 'CSM', ...) becomes an EXTRA token that "
        "survives into the frozenset and breaks the exact-set-equality name "
        "match in candidate_roster_service (`tok == tokens`). Flips to xpass "
        "the day someone teaches the matcher to ignore credential suffixes."
    ),
)
def test_credential_suffix_defeats_name_match_recorded() -> None:
    csv_side = _normalize_name("PAT EXAMPLE, PMP")
    resume_side = _normalize_name("Example, Pat")
    # Desired end state (what "fixing" this means): the two spellings of one
    # name normalise to the SAME token set despite the credential suffix.
    # Fails TODAY because "pmp" survives as an extra token -- that failure is
    # exactly what xfail(strict=True) records; it flips to XPASS (a hard
    # failure, forcing the marker's removal) the day someone fixes it.
    assert csv_side == resume_side
