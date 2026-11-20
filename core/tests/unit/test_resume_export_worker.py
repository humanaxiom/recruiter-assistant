"""RED -- slice H3: ``src.worker.export_tasks`` wiring and never-raise rules.

API pinned::

    async def build_resume_export(ctx, export_id: str) -> str
        # "ready" | "failed" | "skipped" (unknown id, or not 'queued')
    async def expire_resume_exports(ctx) -> str
        # never raises; "unavailable" when pg_pool / blob_store is missing,
        # "error" when a step blows up

``build_resume_export`` is enqueued BY NAME from ``POST /jobs/{id}/resumes/
exports`` so it belongs in ``WorkerSettings.functions``; the sweep is a cron
(never enqueued by name), pinned to one minute of the hour like
``expire_split_requests``. ctx keys read: ``pg_pool`` and ``blob_store``. The
behaviour against a real Postgres is pinned by the integration suite.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.worker.main import WorkerSettings


def _tasks() -> Any:
    from src.worker import export_tasks

    return export_tasks


def test_build_task_is_a_registered_by_name_function() -> None:
    export_tasks = _tasks()
    assert inspect.iscoroutinefunction(export_tasks.build_resume_export)
    assert export_tasks.build_resume_export in WorkerSettings.functions
    assert export_tasks.build_resume_export.__name__ == "build_resume_export"


def test_sweep_is_an_hourly_cron_and_not_an_enqueue_by_name_task() -> None:
    export_tasks = _tasks()
    assert inspect.iscoroutinefunction(export_tasks.expire_resume_exports)
    jobs = [
        j
        for j in WorkerSettings.cron_jobs
        if j.coroutine is export_tasks.expire_resume_exports
    ]
    assert len(jobs) == 1
    assert jobs[0].hour is None
    assert jobs[0].minute is not None
    assert export_tasks.expire_resume_exports not in WorkerSettings.functions


@pytest.mark.asyncio
async def test_sweep_reports_unavailable_when_ctx_is_incomplete() -> None:
    export_tasks = _tasks()
    assert await export_tasks.expire_resume_exports({}) == "unavailable"
    assert (
        await export_tasks.expire_resume_exports({"pg_pool": MagicMock()})
        == "unavailable"
    )


@pytest.mark.asyncio
async def test_sweep_never_raises_when_the_database_is_gone() -> None:
    export_tasks = _tasks()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=RuntimeError("db gone"))
    ctx: dict[str, Any] = {"pg_pool": pool, "blob_store": MagicMock()}

    assert await export_tasks.expire_resume_exports(ctx) == "error"


@pytest.mark.asyncio
async def test_sweep_never_deletes_a_blob_when_the_query_fails() -> None:
    export_tasks = _tasks()
    store = MagicMock()
    store.delete = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=RuntimeError("db gone"))

    await export_tasks.expire_resume_exports({"pg_pool": pool, "blob_store": store})

    store.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_task_ignores_a_malformed_export_id() -> None:
    export_tasks = _tasks()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=AssertionError("must not touch the db"))
    ctx: dict[str, Any] = {"pg_pool": pool, "blob_store": MagicMock()}

    assert await export_tasks.build_resume_export(ctx, "not-a-uuid") == "skipped"
