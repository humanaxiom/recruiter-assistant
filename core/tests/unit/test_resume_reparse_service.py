"""RED — pins the résumé re-parse SQL contract at the module level:

* ``resume_service._RESET_FOR_REPARSE_SQL`` carries every predicate the spec
  requires (reset the five parse-outcome columns, never touch a withdrawn or
  clean row, and gate eligibility on ``failed`` OR ``parsed AND degraded``).
* ``resume_service.reset_for_reparse`` calls it and returns a bool keyed on
  whether a row came back.
* ``reconcile._SELECT_STALLED`` measures staleness from
  ``GREATEST(uploaded_at, COALESCE(reparse_requested_at, uploaded_at))`` — not
  bare ``uploaded_at`` — so a fresh re-parse is not immediately re-queued by
  the reconciler's own next tick.
* ``models/ddl.py`` carries the idempotent ``reparse_requested_at`` ALTER.
* ``schemas/resumes.ResumeReparseOut`` mirrors ``JobReparseOut``'s shape.

None of these names exist yet on this branch — every test below fails at
import (``AttributeError``/``ImportError``) or on a plain string
`in`/`not in` assertion. RED half of the TDD cycle. Real-Postgres behaviour
(does the UPDATE actually apply, does the reconciler actually skip a fresh
retry) is proven in ``test_resume_reparse_pg.py``, not here.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.models import ddl
from src.schemas.resumes import ResumeReparseOut
from src.services import resume_service
from src.worker import reconcile


def _squash(sql: str) -> str:
    """Collapse whitespace so a reformatted-but-equivalent SQL string still
    matches a substring assertion."""
    return re.sub(r"\s+", " ", sql).strip().upper()


# ── _RESET_FOR_REPARSE_SQL ────────────────────────────────────────────────


def test_reset_for_reparse_sql_resets_the_five_parse_outcome_columns() -> None:
    sql = _squash(resume_service._RESET_FOR_REPARSE_SQL)
    assert "STATUS = 'UPLOADED'" in sql
    assert "FAILURE_REASON = NULL" in sql
    assert "PARSED = NULL" in sql
    assert "PARSED_AT = NULL" in sql
    assert "RECONCILE_ATTEMPTS = 0" in sql
    assert "REPARSE_REQUESTED_AT = NOW()" in sql


def test_reset_for_reparse_sql_never_touches_a_withdrawn_row() -> None:
    sql = _squash(resume_service._RESET_FOR_REPARSE_SQL)
    assert "WITHDRAWN_AT IS NULL" in sql


def test_reset_for_reparse_sql_gates_on_failed_or_degraded_parsed() -> None:
    sql = _squash(resume_service._RESET_FOR_REPARSE_SQL)
    assert "STATUS = 'FAILED'" in sql
    assert "STATUS = 'PARSED'" in sql
    assert "DEGRADED" in sql
    # never touches 'uploaded'/'parsing' — those are in-flight, owned by the
    # reconciler, not this route.
    assert sql.count("'UPLOADED'") == 1  # only the SET clause new status


def test_reset_for_reparse_sql_returns_the_id() -> None:
    assert "RETURNING ID" in _squash(resume_service._RESET_FOR_REPARSE_SQL)


def test_reset_for_reparse_sql_filters_on_the_given_id() -> None:
    assert "WHERE ID = $1" in _squash(resume_service._RESET_FOR_REPARSE_SQL)


# ── reset_for_reparse (thin async wrapper) ────────────────────────────────


@pytest.mark.asyncio
async def test_reset_for_reparse_returns_true_when_a_row_comes_back() -> None:
    resume_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": resume_id})
    result = await resume_service.reset_for_reparse(conn, resume_id)
    assert result is True
    conn.fetchrow.assert_awaited_once_with(
        resume_service._RESET_FOR_REPARSE_SQL, resume_id
    )


@pytest.mark.asyncio
async def test_reset_for_reparse_returns_false_when_no_row_is_eligible() -> None:
    resume_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    result = await resume_service.reset_for_reparse(conn, resume_id)
    assert result is False


# ── reconcile._SELECT_STALLED — the double-enqueue guard ──────────────────


def test_select_stalled_measures_staleness_from_reparse_requested_at_too() -> None:
    """Without this, a fresh re-parse (old ``uploaded_at``, brand new
    ``reparse_requested_at``) is picked up by the very next reconciler tick
    and double-enqueued alongside the user's own in-flight retry."""
    sql = _squash(reconcile._SELECT_STALLED)
    assert "GREATEST(UPLOADED_AT, COALESCE(REPARSE_REQUESTED_AT, UPLOADED_AT))" in sql
    # the bare, un-greatest'd predicate must be gone from the WHERE clause
    stale_predicate = (
        "WHERE STATUS IN ('UPLOADED', 'PARSING') AND WITHDRAWN_AT IS NULL "
        "AND UPLOADED_AT <"
    )
    assert stale_predicate not in sql


def test_select_stalled_orders_on_the_same_greatest_expression() -> None:
    sql = _squash(reconcile._SELECT_STALLED)
    order_clause = sql.split("ORDER BY", 1)[1]
    greatest_expr = "GREATEST(UPLOADED_AT, COALESCE(REPARSE_REQUESTED_AT, UPLOADED_AT))"
    assert greatest_expr in order_clause


# ── DDL ────────────────────────────────────────────────────────────────────


def test_ddl_carries_the_reparse_requested_at_alter() -> None:
    joined = " ".join(ddl._STATEMENTS)
    assert (
        "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS reparse_requested_at "
        "TIMESTAMPTZ" in joined
    )


def test_reparse_requested_at_alter_is_idempotent() -> None:
    stmt = next(s for s in ddl._STATEMENTS if "reparse_requested_at" in s.lower())
    assert "IF NOT EXISTS" in stmt.upper()


# ── ResumeReparseOut ────────────────────────────────────────────────────────


def test_resume_reparse_out_shape() -> None:
    resume_id = uuid4()
    out = ResumeReparseOut(id=resume_id, status="queued")
    assert out.id == resume_id
    assert out.status == "queued"


def test_resume_reparse_out_rejects_extra_fields() -> None:
    with pytest.raises(Exception):  # noqa: B017 — pydantic's own ValidationError
        ResumeReparseOut(id=uuid4(), status="queued", extra_field="nope")  # type: ignore[call-arg]


def test_resume_reparse_out_rejects_a_status_other_than_queued() -> None:
    with pytest.raises(Exception):  # noqa: B017 — pydantic's own ValidationError
        ResumeReparseOut(id=uuid4(), status="done")  # type: ignore[arg-type]
