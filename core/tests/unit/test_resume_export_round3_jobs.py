"""RED -- round 3: the export JOB fixes, offline half.

Pinned here (the behaviour against a real Postgres is in
``tests/integration/test_resume_export_round3_pg.py``)::

    ExportApplicant.cover_typed: bool = False
        # the applicant typed a cover letter into the app (``cover_letter_text``)
        # and has no cover file. The manifest line for that applicant carries
        # COVER_TYPED_NOTE (exact text below). It does not change the
        # anonymized / not-anonymized classification.
    export_tasks._ROWS_SQL
        # selects whether a typed cover letter exists (``cover_letter_text``)
        # without decrypting it, and ends in ``LIMIT $2`` (the applicant cap)
    ddl: ALTER TABLE resume_export_requests ADD COLUMN IF NOT EXISTS blind_review
        # BOOLEAN (nullable: NULL == built before the column existed)
    doctor._EXPORT_READY_OVERDUE_SQL + Finding(check="pg.export_ready_past_expiry")
        # ``ready`` rows whose expires_at is more than 2 hours in the past: the
        # hourly sweep should have expired them. A NEW constant and check, so the
        # existing ``_EXPORT_STALE_HOLDING_SQL`` assertions stay true.
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import fitz  # type: ignore[import-untyped]
import pytest

from src import doctor

NOTE = "cover letter typed in the app; not exported"
NAME = "Testy McTestface"
TERMS = [NAME, "Testy", "McTestface"]


def _pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate([NAME, "Experienced in recruiting and operations."]):
        page.insert_text((72, 72 + i * 16), line, fontsize=11)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _applicant(**kw: Any) -> Any:
    from src.services.resume_export_service import ExportApplicant

    base: dict[str, Any] = {"resume_pdf": _pdf(), "cover_pdf": None, "terms": TERMS}
    base.update(kw)
    return ExportApplicant(**base)


def _manifest(applicants: list[Any], *, blind: bool) -> list[str]:
    from src.services.resume_export_service import build_export

    data = build_export(applicants, blind_review=blind).data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read("MANIFEST.txt").decode("utf-8").splitlines()


def _line(lines: list[str], label: str) -> str:
    return next(ln for ln in lines if ln.startswith(label))


# -------------------------------------------------------- typed cover letters


def test_cover_typed_defaults_to_false() -> None:
    assert _applicant().cover_typed is False


@pytest.mark.parametrize("blind", [False, True])
def test_a_typed_cover_letter_is_noted_in_the_manifest(blind: bool) -> None:
    lines = _manifest([_applicant(cover_typed=True)], blind=blind)

    assert NOTE in _line(lines, "applicant-001")


@pytest.mark.parametrize("blind", [False, True])
def test_no_note_when_there_is_no_typed_cover_letter(blind: bool) -> None:
    lines = _manifest([_applicant(), _applicant(cover_typed=False)], blind=blind)

    assert not [ln for ln in lines if "typed in the app" in ln]


def test_the_note_names_only_the_applicant_that_typed_one() -> None:
    lines = _manifest(
        [_applicant(), _applicant(cover_typed=True), _applicant()], blind=False
    )

    assert NOTE not in _line(lines, "applicant-001")
    assert NOTE in _line(lines, "applicant-002")
    assert NOTE not in _line(lines, "applicant-003")


def test_a_typed_cover_letter_does_not_change_the_counts() -> None:
    lines = _manifest([_applicant(cover_typed=True)], blind=False)

    assert "applicants: 1" in lines
    assert "anonymized: 1" in lines
    assert "not anonymized: 0" in lines
    assert "not anonymized" not in _line(lines, "applicant-001").lower()


def test_a_typed_cover_letter_is_noted_when_the_resume_is_not_anonymized() -> None:
    lines = _manifest([_applicant(terms=None, cover_typed=True)], blind=False)

    line = _line(lines, "applicant-001")
    assert NOTE in line
    assert "not anonymized" in line.lower()


def test_the_typed_cover_letter_text_is_never_in_the_zip() -> None:
    from src.services.resume_export_service import build_export

    data = build_export([_applicant(cover_typed=True)], blind_review=False).data

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert not [n for n in zf.namelist() if "cover" in n]


def test_the_worker_query_asks_whether_a_typed_cover_letter_exists() -> None:
    from src.worker import export_tasks

    sql = export_tasks._ROWS_SQL.lower()

    assert "cover_letter_text" in sql
    assert "is not null" in sql


# --------------------------------------------------------- the build-time cap


def test_the_applicant_query_carries_the_cap_as_a_parameter() -> None:
    from src.worker import export_tasks

    assert re.search(r"limit\s+\$2\b", export_tasks._ROWS_SQL, re.I)


def test_the_applicant_query_still_excludes_withdrawn_and_is_ordered() -> None:
    from src.worker import export_tasks

    sql = export_tasks._ROWS_SQL.lower()

    assert "withdrawn_at is null" in sql
    assert "order by" in sql
    assert sql.index("order by") < sql.index("limit")


# ------------------------------------------------------- the blind_review DDL


def test_blind_review_is_added_to_an_existing_table_idempotently() -> None:
    from src.models.ddl import _STATEMENTS

    pattern = re.compile(
        r"ALTER TABLE\s+resume_export_requests\s+"
        r"ADD COLUMN IF NOT EXISTS\s+blind_review\s+BOOLEAN\b",
        re.I,
    )
    create = re.compile(r"CREATE TABLE IF NOT EXISTS\s+resume_export_requests\b", re.I)
    alters = [i for i, s in enumerate(_STATEMENTS) if pattern.search(s)]
    creates = [i for i, s in enumerate(_STATEMENTS) if create.search(s)]

    assert len(alters) == 1
    assert creates
    assert alters[0] > creates[0]


def test_the_stored_blind_review_has_no_not_null_default_that_hides_old_rows() -> None:
    from src.models.ddl import _STATEMENTS

    alter = next(
        s
        for s in _STATEMENTS
        if re.search(r"ADD COLUMN IF NOT EXISTS\s+blind_review", s, re.I)
        and "resume_export_requests" in s
    )

    assert "default" not in alter.lower()
    assert "not null" not in alter.lower()


# ---------------------------------------------------- doctor: overdue ready rows

_CHECK = "pg.export_ready_past_expiry"


def _acm(value: Any) -> Any:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _pg(overdue: list[dict[str, Any]], *, raises: Exception | None = None) -> MagicMock:
    conn = MagicMock(name="pg")
    conn.fetchval = AsyncMock(side_effect=[0, 0, 0, 0, 0])

    async def _fetch(sql: str, *_a: Any) -> list[dict[str, Any]]:
        lowered = sql.lower()
        if "resume_export_requests" in lowered and "'ready'" in lowered:
            if raises is not None:
                raise raises
            return overdue
        return []

    conn.fetch = AsyncMock(side_effect=_fetch)
    pool = MagicMock(name="pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return pool


def _neo() -> MagicMock:
    session = MagicMock(name="session")

    async def _run(*_a: Any, **_k: Any) -> Any:
        result = MagicMock()
        result.single = AsyncMock(return_value={"n": 0})
        return result

    session.run = AsyncMock(side_effect=_run)
    driver = MagicMock(name="driver")
    driver.session = MagicMock(return_value=_acm(session))
    return driver


def _rows(n: int) -> list[dict[str, Any]]:
    return [{"id": uuid4()} for _ in range(n)]


@pytest.mark.asyncio
async def test_a_ready_export_long_past_its_expiry_fails_the_doctor() -> None:
    findings = await doctor.run_checks(pg=_pg(_rows(3)), neo4j=_neo())

    f = next(x for x in findings if x.check == _CHECK)
    assert f.severity == "fail"
    assert f.count == 3
    assert doctor.exit_code(findings) == 1


@pytest.mark.asyncio
async def test_the_overdue_finding_has_a_remedy_and_names_no_row() -> None:
    rows = _rows(1)

    findings = await doctor.run_checks(pg=_pg(rows), neo4j=_neo())

    f = next(x for x in findings if x.check == _CHECK)
    assert f.remedy.strip()
    assert "expire_resume_exports" in f.remedy
    assert str(rows[0]["id"]) not in f.detail


@pytest.mark.asyncio
async def test_no_overdue_rows_means_no_overdue_finding() -> None:
    findings = await doctor.run_checks(pg=_pg([]), neo4j=_neo())

    assert not [f for f in findings if f.check == _CHECK]
    assert not [f for f in findings if f.check.endswith(".unreachable")], findings


@pytest.mark.asyncio
async def test_a_failing_overdue_query_becomes_a_finding_not_a_traceback() -> None:
    findings = await doctor.run_checks(
        pg=_pg([], raises=RuntimeError("relation does not exist")), neo4j=_neo()
    )

    assert any(
        f.severity == "fail" and "relation does not exist" in f.detail for f in findings
    )
    assert doctor.exit_code(findings) == 1


def test_the_overdue_sql_targets_ready_rows_two_hours_past_expiry() -> None:
    sql = doctor._EXPORT_READY_OVERDUE_SQL.lower()

    assert "resume_export_requests" in sql
    assert "status = 'ready'" in sql
    assert "expires_at" in sql
    assert re.search(r"2\s*hours?|hours\s*=>\s*2|120\s*min", sql)
    assert "now()" in sql


def test_the_overdue_sql_is_a_row_select_not_a_count() -> None:
    assert "count(" not in doctor._EXPORT_READY_OVERDUE_SQL.lower()
    assert doctor._EXPORT_READY_OVERDUE_SQL.strip().lower().startswith("select id")


def test_the_dead_row_check_is_untouched_by_the_new_one() -> None:
    sql = doctor._EXPORT_STALE_HOLDING_SQL.lower()

    assert "'ready'" not in sql
    assert "blob_key is not null" in sql
