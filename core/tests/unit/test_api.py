"""Unit tests for the FastAPI routes — logic exercised without live stores."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.api.main import (
    TaskCreate,
    app,
    create_task,
    get_task,
    health,
    lifespan,
    run_gates,
    similar,
    task_lineage,
)
from src.models.db import Task


@pytest.mark.asyncio
async def test_health() -> None:
    assert await health() == {"status": "ok"}


@pytest.mark.asyncio
async def test_create_task_enqueues_pipeline() -> None:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    app.state.arq = AsyncMock()

    payload = TaskCreate(title="Add rate limiter", spec="limit requests per ip")
    task = await create_task(payload, session)

    assert task.title == "Add rate limiter"
    assert task.branch is not None and task.branch.startswith("agent/")
    session.add.assert_called_once()
    session.commit.assert_awaited_once()
    app.state.arq.enqueue_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_task_found() -> None:
    existing = Task(title="t", spec="spec spec")
    session = MagicMock()
    session.get = AsyncMock(return_value=existing)
    assert await get_task(uuid.uuid4(), session) is existing


@pytest.mark.asyncio
async def test_get_task_missing_raises_404() -> None:
    session = MagicMock()
    session.get = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await get_task(uuid.uuid4(), session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_task_lineage_delegates_to_memory() -> None:
    app.state.memory = AsyncMock()
    app.state.memory.task_lineage = AsyncMock(return_value=[{"subtask": "s"}])
    out = await task_lineage(uuid.uuid4())
    assert out == [{"subtask": "s"}]


@pytest.mark.asyncio
async def test_similar_delegates_to_memory() -> None:
    app.state.memory = AsyncMock()
    app.state.memory.similar_artifacts = AsyncMock(return_value=[{"id": "a"}])
    out = await similar("query", k=3)
    assert out == [{"id": "a"}]


@pytest.mark.asyncio
async def test_run_gates_returns_job_id() -> None:
    app.state.arq = AsyncMock()
    app.state.arq.enqueue_job = AsyncMock(return_value=SimpleNamespace(job_id="j1"))
    assert await run_gates("agent/x-y") == {"job_id": "j1"}


@pytest.mark.asyncio
async def test_lifespan_bootstraps_and_tears_down() -> None:
    memory = AsyncMock()
    engine = MagicMock()
    engine.dispose = AsyncMock()
    with (
        patch("src.api.main.create_async_engine", return_value=engine),
        patch("src.api.main.async_sessionmaker"),
        patch("src.api.main.create_pool", AsyncMock(return_value=AsyncMock())),
        patch("src.api.main.GraphMemory", return_value=memory),
        patch("src.api.main.init_schema", AsyncMock()) as init_mock,
    ):
        async with lifespan(app):
            pass

    init_mock.assert_awaited_once()
    memory.ensure_schema.assert_awaited_once()
    memory.close.assert_awaited_once()
    engine.dispose.assert_awaited_once()
