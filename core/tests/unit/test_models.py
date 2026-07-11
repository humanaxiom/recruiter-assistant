"""Unit tests for Postgres models + idempotent schema bootstrap."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.db import Base, GateStatus, Task, TaskStatus, init_schema


def test_enum_values() -> None:
    assert TaskStatus.PENDING.value == "pending"
    assert TaskStatus.ESCALATED.value == "escalated"
    assert GateStatus.GREEN.value == "green"


def test_task_tablename() -> None:
    assert Task.__tablename__ == "tasks"
    assert "tasks" in Base.metadata.tables


@pytest.mark.asyncio
async def test_init_schema_runs_create_all() -> None:
    conn = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=conn)
    begin_cm.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.begin = MagicMock(return_value=begin_cm)

    await init_schema(engine)

    conn.run_sync.assert_awaited_once()
