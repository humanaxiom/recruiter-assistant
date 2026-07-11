"""Unit tests for the arq worker wiring (``src/worker/main.py``).

Phase 1 adds one thing to the worker: on ``startup`` it builds *its own*
``BlobStore`` (rooted at ``settings.storage_dir``) and parks it on
``ctx['blob_store']`` so the parse/project tasks in Phases 3-4 can read it.
The worker does not share the API's store.

All external IO (asyncpg, Neo4j, the DDL) is mocked exactly as the Phase 0
tests do — no live services. ``get_settings`` is patched so the store roots at
``tmp_path`` instead of the container's ``/data``.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.settings import Settings
from src.storage.blob_store import BlobStore
from src.worker.main import WorkerSettings, startup
from src.worker.resume_tasks import parse_resume
from src.worker.tasks import parse_job


def _fake_driver() -> Any:
    driver = MagicMock()
    driver.close = AsyncMock()
    return driver


def test_startup_is_a_coroutine() -> None:
    assert inspect.iscoroutinefunction(startup)


@pytest.mark.asyncio
async def test_startup_parks_a_blob_store_on_ctx(tmp_path: Path) -> None:
    ctx: dict[str, Any] = {}
    settings = Settings(storage_dir=str(tmp_path))
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch(
            "src.worker.main.asyncpg.create_pool",
            AsyncMock(return_value=MagicMock()),
        ),
        patch("src.worker.main.init_schema", AsyncMock()),
        patch(
            "src.worker.main.AsyncGraphDatabase.driver",
            return_value=_fake_driver(),
        ),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
    ):
        await startup(ctx)

    store = ctx.get("blob_store")
    assert isinstance(store, BlobStore)


@pytest.mark.asyncio
async def test_startup_blob_store_is_rooted_at_storage_dir(tmp_path: Path) -> None:
    ctx: dict[str, Any] = {}
    settings = Settings(storage_dir=str(tmp_path))
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch(
            "src.worker.main.asyncpg.create_pool",
            AsyncMock(return_value=MagicMock()),
        ),
        patch("src.worker.main.init_schema", AsyncMock()),
        patch(
            "src.worker.main.AsyncGraphDatabase.driver",
            return_value=_fake_driver(),
        ),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
    ):
        await startup(ctx)

    store: BlobStore = ctx["blob_store"]
    await store.put("worker.txt", b"z")
    assert (tmp_path / "worker.txt").read_bytes() == b"z"


def test_worker_registers_the_phase_3_parse_tasks() -> None:
    """Phase 3 registers the parse tasks — and only those.

    The outbox drainer (``project_to_graph``) is deliberately absent: Phase 3
    stops at parse -> Postgres -> outbox row, and Phase 4 adds the drainer that
    projects those rows into Neo4j. Undelivered outbox rows at this stage are
    the outbox pattern working, not a dropped requirement.
    """
    assert WorkerSettings.functions == [parse_job, parse_resume]
