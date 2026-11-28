"""sfu-telemetry pilot (ADR-048) — audit rows carry the trace that wrote them.

``audit_log.trace_id`` is nullable (rows written before this change, and
rows written outside any request, have none) and is added idempotently like
every other column in ``ddl.py``. The provenance ledger copies it, so an
auditor can jump from a ledger event straight to the request's trace.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from opentelemetry import trace

from src.models import ddl
from src.services import audit_service
from tests.unit.telemetry_support import SPANS  # noqa: F401  (installs the provider)


def test_ddl_adds_a_nullable_trace_id_column_idempotently() -> None:
    stmts = [" ".join(s.split()) for s in ddl._STATEMENTS]
    assert "ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS trace_id TEXT" in stmts


def test_insert_sql_writes_trace_id() -> None:
    assert "trace_id" in audit_service._INSERT_SQL


@pytest.mark.asyncio
async def test_record_audit_binds_the_current_trace_id() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    with trace.get_tracer("t").start_as_current_span(
        "POST /resumes/{id}/reveal"
    ) as span:
        await audit_service.record_audit(
            conn,
            actor_kind="user",
            actor_user_id=uuid.uuid4(),
            actor_service=None,
            action="reveal",
            subject_type="resume",
            subject_id=uuid.uuid4(),
        )
        expected = format(span.get_span_context().trace_id, "032x")
    assert expected in conn.execute.await_args.args


@pytest.mark.asyncio
async def test_record_audit_outside_a_span_binds_null_trace_id() -> None:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    await audit_service.record_audit(
        conn,
        actor_kind="service",
        actor_user_id=None,
        actor_service="taleo-sync",
        action="taleo_sync",
        subject_type="job",
        subject_id=uuid.uuid4(),
    )
    args = conn.execute.await_args.args
    assert args[-1] is None
