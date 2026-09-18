"""Unit tests for ``parse_candidate_csv`` (Sponsor Requirements PR2 · S3/I1).

The real Taleo "All Candidates" export arrived (see
``docs/SPONSOR_REQUIREMENTS_PLAN.md`` ~line 402) and it has no attachment
filename on any of its 316 rows and one duplicated person with CONFLICTING
work-authorization declarations. That is why ``parse_candidate_csv`` returns a
``list[CandidateRosterRow]`` (ordered, line-numbered) rather than a dict keyed
by anything — there is no natural unique key, and collapsing the duplicate
would silently hide the conflict a recruiter needs to see.

RED half of the TDD cycle: ``CandidateRosterRow``/``parse_candidate_csv``/
``WORK_AUTHORIZATION_MAP`` do not exist yet in
``src.services.bulk_ingest_service``, so this whole file fails at collection
with an ``ImportError``. That failure IS the expected RED state for this slice.

Work-authorization mapping is a screening decision, not a formatting one: an
unrecognised declaration must map to ``"unknown"`` and NEVER to
``"not_eligible"`` — silently banding a real candidate last on an
unrecognised string would be an adverse decision on a protected ground
(BC Human Rights Code; see ``src/schemas/resumes.py``'s ``WorkAuthorization``
docstring). Several tests below exist specifically to pin that.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.errors import AppError
from src.services.bulk_ingest_service import (
    _MAX_MANIFEST_BYTES,
    WORK_AUTHORIZATION_MAP,
    CandidateRosterRow,
    ManifestError,
    parse_candidate_csv,
)

_VENDOR_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "vendor"
    / "taleo"
    / "all_candidates_export.csv"
)


def _csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


# ── header handling ──────────────────────────────────────────────────────


def test_bom_is_stripped_so_first_header_matches_without_the_bom() -> None:
    # Only a Name column, deliberately, with the BOM glued to it: if the BOM
    # weren't stripped the header key would be "﻿name" (no match), the
    # required-column check would fail, and this would raise instead.
    blob = b'\xef\xbb\xbfName\n"Smith, Jordan"\n'
    rows = parse_candidate_csv(blob)
    assert len(rows) == 1
    assert rows[0].name == "Smith, Jordan"
    assert rows[0].email is None


@pytest.mark.parametrize(
    "header",
    [
        "Work Authorization",
        "work_authorization",
        "WORK AUTHORIZATION",
        "  Work  Authorization  ",
    ],
)
def test_work_authorization_header_matches_case_and_space_insensitively(
    header: str,
) -> None:
    blob = _csv_bytes(f'Name,"{header}"\n"Smith, Jordan","No Restrictions"\n')
    rows = parse_candidate_csv(blob)
    assert len(rows) == 1
    assert rows[0].work_authorization == "eligible"


def test_missing_both_name_and_email_columns_raises() -> None:
    blob = _csv_bytes("SFU ID,Work Authorization\n301000000,eligible\n")
    with pytest.raises(ManifestError):
        parse_candidate_csv(blob)


def test_name_column_alone_is_sufficient() -> None:
    rows = parse_candidate_csv(_csv_bytes('Name\n"Smith, Jordan"\n'))
    assert len(rows) == 1
    assert rows[0].name == "Smith, Jordan"


def test_email_column_alone_is_sufficient() -> None:
    rows = parse_candidate_csv(_csv_bytes("Email\njordan@example.invalid\n"))
    assert len(rows) == 1
    assert rows[0].email == "jordan@example.invalid"
    assert rows[0].name is None


def test_empty_blob_raises() -> None:
    with pytest.raises(ManifestError):
        parse_candidate_csv(b"")


def test_oversize_blob_raises_with_message_stating_the_cap() -> None:
    oversize = b"Name\n" + b"a\n" * (_MAX_MANIFEST_BYTES)
    assert len(oversize) > _MAX_MANIFEST_BYTES
    with pytest.raises(ManifestError) as excinfo:
        parse_candidate_csv(oversize)
    assert str(_MAX_MANIFEST_BYTES) in str(excinfo.value)


def test_manifest_error_is_an_app_error() -> None:
    # Same trust boundary/rendering contract as parse_csv_manifest's errors.
    with pytest.raises(AppError):
        parse_candidate_csv(b"")


# ── cell handling ─────────────────────────────────────────────────────────


def test_whitespace_only_cell_becomes_none() -> None:
    blob = _csv_bytes('Name,Email,SFU ID\n"Smith, Jordan"," "," "\n')
    rows = parse_candidate_csv(blob)
    assert len(rows) == 1
    assert rows[0].email is None
    assert rows[0].sfu_id is None


def test_row_with_neither_name_nor_email_is_skipped_not_an_error() -> None:
    blob = _csv_bytes('Name,Email\n"Smith, Jordan",jordan@example.invalid\n" "," "\n')
    rows = parse_candidate_csv(blob)
    assert len(rows) == 1
    assert rows[0].name == "Smith, Jordan"


def test_line_no_is_the_real_csv_line_and_survives_a_skipped_row() -> None:
    # header=1, kept=2, skipped=3 (blank), kept=4 -> line_no 2 then 4.
    blob = _csv_bytes(
        "Name,Email\n"
        '"Alpha, One",alpha@example.invalid\n'
        '" "," "\n'
        '"Beta, Two",beta@example.invalid\n'
    )
    rows = parse_candidate_csv(blob)
    assert [r.line_no for r in rows] == [2, 4]
    assert [r.name for r in rows] == ["Alpha, One", "Beta, Two"]


# ── work-authorization mapping ──────────────────────────────────────────


def test_work_authorization_map_key_set_is_exactly_the_four_declarations() -> None:
    assert set(WORK_AUTHORIZATION_MAP) == {
        "no restrictions",
        "work permit",
        "study permit",
        "not eligible to work in canada",
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("No Restrictions", "eligible"),
        ("Work Permit", "eligible"),  # sponsor-confirmed 2026-09-09; 123/316 real rows
        ("Study Permit", "not_eligible"),
        ("Not eligible to work in Canada", "not_eligible"),
        ("NO RESTRICTIONS", "eligible"),
        ("  no restrictions ", "eligible"),
        ("  WORK permit  ", "eligible"),
    ],
)
def test_work_authorization_mapping_is_case_and_whitespace_tolerant(
    raw: str, expected: str
) -> None:
    blob = _csv_bytes(f'Name,"Work Authorization"\n"Smith, Jordan","{raw}"\n')
    rows = parse_candidate_csv(blob)
    assert len(rows) == 1
    assert rows[0].work_authorization == expected
    assert rows[0].work_authorization_source == raw


@pytest.mark.parametrize(
    "raw",
    [
        "Pending Review",
        "TBD",
        "N/A",
        "Awaiting document",
        "???",
    ],
)
def test_unrecognised_work_authorization_maps_to_unknown_never_not_eligible(
    raw: str,
) -> None:
    blob = _csv_bytes(f'Name,"Work Authorization"\n"Smith, Jordan","{raw}"\n')
    rows = parse_candidate_csv(blob)
    assert len(rows) == 1
    assert rows[0].work_authorization == "unknown"
    assert rows[0].work_authorization_source == raw


def test_blank_work_authorization_maps_to_unknown_with_none_source() -> None:
    blob = _csv_bytes('Name,"Work Authorization"\n"Smith, Jordan"," "\n')
    rows = parse_candidate_csv(blob)
    assert len(rows) == 1
    assert rows[0].work_authorization == "unknown"
    assert rows[0].work_authorization_source is None


def test_missing_work_authorization_column_yields_unknown_for_every_row() -> None:
    blob = _csv_bytes('Name\n"Smith, Jordan"\n"Doe, Alex"\n')
    rows = parse_candidate_csv(blob)
    assert len(rows) == 2
    assert all(r.work_authorization == "unknown" for r in rows)
    assert all(r.work_authorization_source is None for r in rows)


# ── internal-employee flags ──────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["I", "i", " I ", " i "])
def test_apsa_internal_i_case_and_whitespace_tolerant_sets_flag_true(
    raw: str,
) -> None:
    blob = _csv_bytes(f'Name,"APSA Internal"\n"Smith, Jordan","{raw}"\n')
    rows = parse_candidate_csv(blob)
    assert rows[0].internal_apsa is True
    # Contract change (slice 2): "CUPE Internal" has no column at all here,
    # so it is None (absent), not False (present-and-not-internal).
    assert rows[0].internal_cupe is None


@pytest.mark.parametrize("raw", ["I", "i", " I ", " i "])
def test_cupe_internal_i_case_and_whitespace_tolerant_sets_flag_true(
    raw: str,
) -> None:
    blob = _csv_bytes(f'Name,"CUPE Internal"\n"Smith, Jordan","{raw}"\n')
    rows = parse_candidate_csv(blob)
    assert rows[0].internal_cupe is True
    # Contract change (slice 2): "APSA Internal" has no column at all here,
    # so it is None (absent), not False (present-and-not-internal).
    assert rows[0].internal_apsa is None


def test_blank_apsa_and_cupe_cells_are_false() -> None:
    blob = _csv_bytes('Name,"APSA Internal","CUPE Internal"\n"Smith, Jordan"," "," "\n')
    rows = parse_candidate_csv(blob)
    assert rows[0].internal_apsa is False
    assert rows[0].internal_cupe is False


@pytest.mark.parametrize("raw", ["Yes", "1", "true", "X", "II"])
def test_apsa_cupe_any_other_non_blank_value_is_false_not_guessed(raw: str) -> None:
    blob = _csv_bytes(
        f'Name,"APSA Internal","CUPE Internal"\n"Smith, Jordan","{raw}","{raw}"\n'
    )
    rows = parse_candidate_csv(blob)
    assert rows[0].internal_apsa is False
    assert rows[0].internal_cupe is False


def test_no_apsa_cupe_columns_yields_none_for_every_row_and_does_not_raise() -> None:
    """Contract change (slice 2): the ABSENT-column case must be
    distinguishable from a present-but-blank cell, so
    ``reconcile_candidate_roster`` can tell "this roster has no APSA/CUPE
    columns at all" from "every candidate in it happens to be non-internal"
    and skip writing the field entirely rather than clearing a previously-set
    flag. ``None`` means absent; ``False`` means present-and-not-internal."""
    blob = _csv_bytes('Name\n"Smith, Jordan"\n"Doe, Alex"\n')
    rows = parse_candidate_csv(blob)
    assert len(rows) == 2
    assert all(r.internal_apsa is None for r in rows)
    assert all(r.internal_cupe is None for r in rows)


# ── formula injection: parser must not mutate cells ──────────────────────


def test_parser_does_not_neutralise_formula_looking_cells() -> None:
    # Neutralisation (_csv_safe/_csv_row in shortlist_service.py) is the
    # EXPORT boundary's job and is already tested there. The import-side
    # parser must hand back the raw cell verbatim so the reconciler sees
    # exactly what Taleo sent.
    blob = _csv_bytes("Name,Email\n\"=cmd|' /C calc'!A1\",+injection@example.invalid\n")
    rows = parse_candidate_csv(blob)
    assert len(rows) == 1
    assert rows[0].name == "=cmd|' /C calc'!A1"
    assert rows[0].email == "+injection@example.invalid"


# NOTE: a test asserting a roster-sourced cell IS neutralised on export is
# deferred — there is no roster-export code path in this codebase yet (only
# the shortlist exporter exists). Adding one here would require writing
# implementation to make it meaningful, which this file must not do.


# ── CandidateRosterRow shape ──────────────────────────────────────────────


def test_candidate_roster_row_is_frozen() -> None:
    row = CandidateRosterRow(
        line_no=2,
        name="Smith, Jordan",
        email="jordan@example.invalid",
        sfu_id=None,
        work_authorization="unknown",
        work_authorization_source=None,
        internal_apsa=False,
        internal_cupe=False,
        submission_date=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.name = "Doe, Alex"  # type: ignore[misc]


def test_submission_date_is_carried_through_verbatim() -> None:
    blob = _csv_bytes('Name,"Submission Date"\n"Smith, Jordan","03/09/2026"\n')
    rows = parse_candidate_csv(blob)
    assert rows[0].submission_date == "03/09/2026"


def test_sfu_id_is_carried_through() -> None:
    blob = _csv_bytes('Name,"SFU ID"\n"Smith, Jordan",301282405\n')
    rows = parse_candidate_csv(blob)
    assert rows[0].sfu_id == "301282405"


# ── vendor fixture characterisation ───────────────────────────────────────


def test_vendor_fixture_parses_to_exactly_13_rows() -> None:
    blob = _VENDOR_FIXTURE.read_bytes()
    rows = parse_candidate_csv(blob)
    assert len(rows) == 13


def test_vendor_fixture_work_authorization_distribution() -> None:
    rows = parse_candidate_csv(_VENDOR_FIXTURE.read_bytes())
    counts: dict[str, int] = {"eligible": 0, "not_eligible": 0, "unknown": 0}
    for row in rows:
        counts[row.work_authorization] += 1
    # eligible: Nolan(No Restrictions), Okafor(Work Permit), Thibodeaux(No
    #   Restrictions), Kowalczyk(No Restrictions), Ngata(Work Permit),
    #   Lindqvist(NO RESTRICTIONS), injection row(No Restrictions),
    #   Delacroix(Work Permit) = 8
    # not_eligible: Vaszary(Study Permit), Brannigan(Not eligible...),
    #   second Nolan row(Not eligible...) = 3
    # unknown: Sandoval-Reyes(blank), Adeyemi(Pending Review) = 2
    assert counts == {"eligible": 8, "not_eligible": 3, "unknown": 2}


def test_vendor_fixture_internal_flags() -> None:
    rows = parse_candidate_csv(_VENDOR_FIXTURE.read_bytes())
    by_name = {(r.name, r.line_no): r for r in rows}
    ngata = by_name[("Ngata, Hemi", 10)]
    assert ngata.internal_apsa is True
    assert ngata.internal_cupe is True

    thibodeaux = by_name[("Thibodeaux, Rene", 8)]
    assert thibodeaux.internal_apsa is True
    assert thibodeaux.internal_cupe is False

    kowalczyk = by_name[("Kowalczyk, Dariusz", 9)]
    assert kowalczyk.internal_apsa is False
    assert kowalczyk.internal_cupe is True

    # the injection row's non-"I" APSA/CUPE cells ("@SUM(1)", "-2+3") are
    # not "I" so both flags must be False, not guessed.
    injected = by_name[("=cmd|' /C calc'!A1", 12)]
    assert injected.internal_apsa is False
    assert injected.internal_cupe is False

    others_with_no_flags = [
        r
        for (name, _line), r in by_name.items()
        if name not in {"Ngata, Hemi", "Thibodeaux, Rene", "Kowalczyk, Dariusz"}
    ]
    assert all(r.internal_apsa is False for r in others_with_no_flags)
    assert all(r.internal_cupe is False for r in others_with_no_flags)


def test_vendor_fixture_accented_name_round_trips() -> None:
    rows = parse_candidate_csv(_VENDOR_FIXTURE.read_bytes())
    names = [r.name for r in rows]
    assert "Delacroix, Émile" in names


def test_vendor_fixture_row_with_name_but_no_email_yields_none_email() -> None:
    rows = parse_candidate_csv(_VENDOR_FIXTURE.read_bytes())
    delacroix = next(r for r in rows if r.name == "Delacroix, Émile")
    assert delacroix.email is None


def test_vendor_fixture_duplicate_bree_nolan_both_present_with_conflicting_auth() -> (
    None
):
    rows = parse_candidate_csv(_VENDOR_FIXTURE.read_bytes())
    nolans = [r for r in rows if r.name == "Nolan, Bree"]
    assert len(nolans) == 2
    auths = {r.work_authorization for r in nolans}
    assert auths == {"eligible", "not_eligible"}
    # order and line numbers preserved, not collapsed to one entry.
    assert nolans[0].line_no == 2
    assert nolans[1].line_no == 14
    assert nolans[0].work_authorization == "eligible"
    assert nolans[1].work_authorization == "not_eligible"


def test_vendor_fixture_last_blank_line_is_skipped() -> None:
    rows = parse_candidate_csv(_VENDOR_FIXTURE.read_bytes())
    assert all(r.name is not None or r.email is not None for r in rows)
    assert max(r.line_no for r in rows) == 14
