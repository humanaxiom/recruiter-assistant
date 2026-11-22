"""Slice C1 (RED) -- doctor invariant for ``resume_split_requests`` (plan bullet 5).

Retention decision 4: the combined PDF blob is deleted and the proposal NULLed
the moment a request reaches a terminal state (accepted / discarded / failed).
A terminal row that still holds a blob key or a non-NULL proposal is exactly
the ROADMAP A7 shape -- a promise in prose that no row-level check enforces --
so the doctor asks the running database.

API pinned here (``src/doctor.py``)::

    _SPLIT_TERMINAL_HOLDING_SQL: str
        SELECT id FROM resume_split_requests
        WHERE status IN ('accepted','discarded','failed')
          AND (source_blob_key IS NOT NULL OR proposal IS NOT NULL)
    Finding(check="pg.split_terminal_rows_retain_data", severity="fail",
            count=<number of offending rows>, detail=<no PII>, remedy=<non-empty>)

HARD CONSTRAINT on the implementation: the check reads through ``conn.fetch``
(rows of offending ids; ``count = len(rows)``), NOT ``conn.fetchval``. The
existing ``test_doctor.py::_pg`` fake answers ``fetchval`` from a fixed
5-element list in call order and returns ``[]`` from ``fetch``; a sixth
``fetchval`` would raise StopAsyncIteration and turn every existing "healthy"
doctor test into a ``pg.unreachable`` failure.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src import doctor

_CHECK = "pg.split_terminal_rows_retain_data"


def _acm(value: Any) -> Any:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _pg(
    split_rows: list[dict[str, Any]], *, raises: Exception | None = None
) -> MagicMock:
    """The five legacy fetchval answers (all clean) + a SQL-dispatched fetch."""
    conn = MagicMock(name="pg")
    conn.fetchval = AsyncMock(side_effect=[0, 0, 0, 0, 0])

    async def _fetch(sql: str, *_a: Any) -> list[dict[str, Any]]:
        if "resume_split_requests" in sql:
            if raises is not None:
                raise raises
            return split_rows
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
async def test_terminal_rows_still_holding_data_fail_the_doctor() -> None:
    findings = await doctor.run_checks(pg=_pg(_rows(3)), neo4j=_neo())
    f = next(x for x in findings if x.check == _CHECK)
    assert f.severity == "fail"
    assert f.count == 3
    assert doctor.exit_code(findings) == 1


@pytest.mark.asyncio
async def test_the_finding_carries_a_remedy_and_names_no_row_contents() -> None:
    rows = _rows(1)
    findings = await doctor.run_checks(pg=_pg(rows), neo4j=_neo())
    f = next(x for x in findings if x.check == _CHECK)
    assert f.remedy.strip()
    assert "split" in f.remedy.lower()
    assert "%PDF" not in f.detail


@pytest.mark.asyncio
async def test_no_offending_rows_means_no_finding() -> None:
    findings = await doctor.run_checks(pg=_pg([]), neo4j=_neo())
    assert not [f for f in findings if f.check == _CHECK]


@pytest.mark.asyncio
async def test_a_clean_split_table_does_not_disturb_the_legacy_checks() -> None:
    findings = await doctor.run_checks(pg=_pg([]), neo4j=_neo())
    assert not [f for f in findings if f.check.endswith(".unreachable")], findings


@pytest.mark.asyncio
async def test_a_failing_split_query_becomes_a_finding_not_a_traceback() -> None:
    findings = await doctor.run_checks(
        pg=_pg([], raises=RuntimeError("relation does not exist")), neo4j=_neo()
    )
    assert any(
        f.severity == "fail" and "relation does not exist" in f.detail for f in findings
    )
    assert doctor.exit_code(findings) == 1


def test_sql_targets_exactly_the_terminal_statuses_and_both_retained_columns() -> None:
    sql = doctor._SPLIT_TERMINAL_HOLDING_SQL.lower()
    assert "resume_split_requests" in sql
    for status in ("'accepted'", "'discarded'", "'failed'"):
        assert status in sql
    for live in ("'queued'", "'segmenting'", "'proposed'"):
        assert live not in sql
    assert "source_blob_key is not null" in sql
    assert "proposal is not null" in sql
    assert " or " in sql  # either one is enough to be a violation


def test_sql_is_a_row_select_not_a_count() -> None:
    # See the HARD CONSTRAINT in the module docstring: rows via conn.fetch.
    assert "count(" not in doctor._SPLIT_TERMINAL_HOLDING_SQL.lower()
