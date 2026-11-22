"""RED -- slice H3: doctor invariant for ``resume_export_requests``.

A built export ZIP holds every applicant's identity, so its blob must be gone
the moment the row is ``expired`` or ``failed``. A row in either state that
still holds a ``blob_key`` is the ROADMAP A7 shape (a retention promise nothing
enforces), so the doctor asks the running database.

API pinned (``src/doctor.py``)::

    _EXPORT_STALE_HOLDING_SQL: str
        SELECT id FROM resume_export_requests
        WHERE status IN ('expired', 'failed') AND blob_key IS NOT NULL
    Finding(check="pg.export_rows_retain_blob", severity="fail",
            count=<offending rows>, detail=<no PII>, remedy=<non-empty>)

Same HARD CONSTRAINT as the split check: rows are read through ``conn.fetch``
(``count = len(rows)``), never an extra ``fetchval`` -- the existing doctor
fakes answer ``fetchval`` from a fixed five-element list.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src import doctor

_CHECK = "pg.export_rows_retain_blob"


def _acm(value: Any) -> Any:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _pg(rows: list[dict[str, Any]], *, raises: Exception | None = None) -> MagicMock:
    conn = MagicMock(name="pg")
    conn.fetchval = AsyncMock(side_effect=[0, 0, 0, 0, 0])

    async def _fetch(sql: str, *_a: Any) -> list[dict[str, Any]]:
        if "resume_export_requests" in sql:
            if raises is not None:
                raise raises
            return rows
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
async def test_a_dead_export_still_holding_a_blob_fails_the_doctor() -> None:
    findings = await doctor.run_checks(pg=_pg(_rows(2)), neo4j=_neo())
    f = next(x for x in findings if x.check == _CHECK)
    assert f.severity == "fail"
    assert f.count == 2
    assert doctor.exit_code(findings) == 1


@pytest.mark.asyncio
async def test_the_finding_has_a_remedy_and_names_no_row_contents() -> None:
    rows = _rows(1)
    findings = await doctor.run_checks(pg=_pg(rows), neo4j=_neo())
    f = next(x for x in findings if x.check == _CHECK)
    assert f.remedy.strip()
    assert "export" in f.remedy.lower()
    assert str(rows[0]["id"]) not in f.detail


@pytest.mark.asyncio
async def test_no_offending_rows_means_no_finding() -> None:
    findings = await doctor.run_checks(pg=_pg([]), neo4j=_neo())
    assert not [f for f in findings if f.check == _CHECK]
    assert not [f for f in findings if f.check.endswith(".unreachable")], findings


@pytest.mark.asyncio
async def test_a_failing_export_query_becomes_a_finding_not_a_traceback() -> None:
    findings = await doctor.run_checks(
        pg=_pg([], raises=RuntimeError("relation does not exist")), neo4j=_neo()
    )
    assert any(
        f.severity == "fail" and "relation does not exist" in f.detail for f in findings
    )
    assert doctor.exit_code(findings) == 1


def test_sql_targets_dead_statuses_holding_a_blob_key() -> None:
    sql = doctor._EXPORT_STALE_HOLDING_SQL.lower()
    assert "resume_export_requests" in sql
    for status in ("'expired'", "'failed'"):
        assert status in sql
    for live in ("'queued'", "'building'", "'ready'"):
        assert live not in sql
    assert "blob_key is not null" in sql


def test_sql_is_a_row_select_not_a_count() -> None:
    assert "count(" not in doctor._EXPORT_STALE_HOLDING_SQL.lower()
