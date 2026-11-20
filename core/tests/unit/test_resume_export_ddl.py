"""RED -- slice H3: the ``resume_export_requests`` DDL, offline.

The behaviour of the table (CHECK, FK cascade, defaults) is pinned against a
real Postgres in ``tests/integration/test_resume_export_jobs_pg.py``; this only
pins that the statement exists, so a missing table fails fast and offline.
"""

from __future__ import annotations

import re

from src.models.ddl import _STATEMENTS


def _create_sql() -> str:
    for stmt in _STATEMENTS:
        if re.search(
            r"CREATE TABLE IF NOT EXISTS\s+resume_export_requests\b", stmt, re.I
        ):
            return stmt
    raise AssertionError("no CREATE TABLE IF NOT EXISTS resume_export_requests")


def test_the_table_is_created_on_startup() -> None:
    assert _create_sql()


def test_it_declares_every_pinned_column() -> None:
    sql = _create_sql().lower()
    for column in (
        "id",
        "job_id",
        "requested_by",
        "status",
        "blob_key",
        "counts",
        "created_at",
        "expires_at",
    ):
        assert re.search(rf"\b{column}\b", sql), column


def test_status_is_constrained_to_the_five_states() -> None:
    sql = _create_sql().lower()
    for state in ("queued", "building", "ready", "failed", "expired"):
        assert f"'{state}'" in sql, state
    assert "check" in sql


def test_counts_is_jsonb_and_the_job_fk_cascades() -> None:
    sql = _create_sql().lower()
    assert re.search(r"counts\s+jsonb", sql)
    assert re.search(r"references\s+jobs\s*\(\s*id\s*\)\s+on delete cascade", sql)
