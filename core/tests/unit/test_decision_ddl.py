"""RED -- human decision record, slice S2: the ``decision_records`` DDL, string
level. A string match cannot prove Postgres accepts it -- that is
``tests/integration/test_decision_records_pg.py`` -- but it does pin that the
statements are IN ``_STATEMENTS`` (and so run at startup), are idempotent, and
that no source file anywhere UPDATEs or DELETEs the table (append-only).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.models.ddl import _STATEMENTS

_SRC = Path(__file__).resolve().parents[2] / "src"


def _flat(stmt: str) -> str:
    return re.sub(r"\s+", "", stmt).lower()


def _create_table(name: str) -> str | None:
    for stmt in _STATEMENTS:
        if re.search(rf"create\s+table\s+if\s+not\s+exists\s+{name}\b", stmt, re.I):
            return _flat(stmt)
    return None


def _index_of(name: str) -> int | None:
    for i, stmt in enumerate(_STATEMENTS):
        if re.search(rf"create\s+table\s+if\s+not\s+exists\s+{name}\b", stmt, re.I):
            return i
    return None


def _table_sql() -> str:
    sql = _create_table("decision_records")
    assert sql is not None, "no CREATE TABLE IF NOT EXISTS decision_records statement"
    return sql


def test_table_is_declared_idempotently() -> None:
    assert _table_sql()


@pytest.mark.parametrize(
    "fragment",
    [
        "id UUID PRIMARY KEY DEFAULT gen_random_uuid()",
        "seq BIGINT GENERATED ALWAYS AS IDENTITY",
        "job_id UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE",
        "resume_id UUID NOT NULL REFERENCES resumes (id) ON DELETE CASCADE",
        "decision TEXT NOT NULL CHECK (decision IN ('accept','reject','hold'))",
        "rationale TEXT NOT NULL "
        "CHECK (char_length(btrim(rationale)) BETWEEN 10 AND 1000)",
        "decided_by TEXT NOT NULL",
        "decided_by_user_id UUID",
        "decided_by_role TEXT NOT NULL",
        "decided_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "snap_rank INTEGER",
        "snap_score_final DOUBLE PRECISION",
        "snap_entry_id UUID",
        "snap_blind_review BOOLEAN NOT NULL DEFAULT false",
    ],
)
def test_table_carries_the_contract_fragment(fragment: str) -> None:
    assert _flat(fragment) in _table_sql()


def test_snapshot_columns_are_nullable() -> None:
    sql = _table_sql()
    for col, typ in (
        ("snap_rank", "integer"),
        ("snap_score_final", "doubleprecision"),
        ("snap_entry_id", "uuid"),
    ):
        assert f"{col}{typ}" in sql
        assert f"{col}{typ}notnull" not in sql


def test_identity_snapshot_columns_have_no_foreign_key() -> None:
    sql = _table_sql()
    assert "decided_by_user_iduuidreferences" not in sql
    assert "snap_entry_iduuidreferences" not in sql


def test_lookup_index_is_declared_exactly_once() -> None:
    hits = [
        _flat(s)
        for s in _STATEMENTS
        if re.search(
            r"create\s+index\s+if\s+not\s+exists\s+decision_records_job_resume_idx",
            s,
            re.I,
        )
    ]
    assert len(hits) == 1
    assert "ondecision_records(job_id,resume_id,seqdesc)" in hits[0]


def test_table_is_appended_after_interview_questions() -> None:
    iq = _index_of("interview_questions")
    mine = _index_of("decision_records")
    assert iq is not None
    assert mine is not None and mine > iq


def test_no_statement_updates_or_deletes_decision_records_at_startup() -> None:
    for stmt in _STATEMENTS:
        flat = stmt.lower()
        assert not re.search(r"\bupdate\s+decision_records\b", flat)
        assert not re.search(r"\bdelete\s+from\s+decision_records\b", flat)


def test_no_source_file_mutates_decision_records() -> None:
    """Append-only is a code invariant: nothing in ``src`` may UPDATE or DELETE
    the table (a CASCADE from jobs/resumes is the only way a row goes)."""
    pattern = re.compile(
        r"(update\s+decision_records\b|delete\s+from\s+decision_records\b"
        r"|truncate\s+(table\s+)?decision_records\b)",
        re.I,
    )
    offenders = [
        str(p.relative_to(_SRC))
        for p in _SRC.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_decision_records_source_is_actually_scanned() -> None:
    """Guard against the scan above passing vacuously."""
    assert any(
        "decision_records" in p.read_text(encoding="utf-8") for p in _SRC.rglob("*.py")
    )
