"""Unit tests for GraphMemory — schema bootstrap, writes, vector reads.

Uses a fake async Neo4j driver so no live database is needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.memory.graph import GraphMemory


class FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for record in self._records:
            yield record


class FakeSession:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._records = records

    async def run(self, query: str, **params: Any) -> FakeResult:
        self.calls.append((query, params))
        return FakeResult(self._records)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class FakeDriver:
    def __init__(self, records: list[dict[str, Any]] | None = None) -> None:
        self.session_obj = FakeSession(records or [])
        self.closed = False

    def session(self) -> FakeSession:
        return self.session_obj

    async def close(self) -> None:
        self.closed = True


def make_memory(records: list[dict[str, Any]] | None = None) -> GraphMemory:
    driver = FakeDriver(records)
    with patch("src.memory.graph.AsyncOpenAI"):
        return GraphMemory(driver=driver)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_ensure_schema_runs_all_statements() -> None:
    mem = make_memory()
    await mem.ensure_schema()
    session = mem._driver.session()  # type: ignore[attr-defined]
    assert len(session.calls) == 5
    assert any("VECTOR INDEX artifact_embeddings" in q for q, _ in session.calls)


@pytest.mark.asyncio
async def test_close_delegates_to_driver() -> None:
    mem = make_memory()
    await mem.close()
    assert mem._driver.closed is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_record_subtask() -> None:
    mem = make_memory()
    await mem.record_subtask("task", "sub", "coder", "desc")
    session = mem._driver.session()  # type: ignore[attr-defined]
    _, params = session.calls[-1]
    assert params["task_id"] == "task"
    assert params["agent_id"] == "coder"


@pytest.mark.asyncio
async def test_record_artifact_embeds_content() -> None:
    mem = make_memory()
    mem._embed = AsyncMock(return_value=[0.0] * 768)  # type: ignore[method-assign]
    await mem.record_artifact("sub", "art", "content", "coder")
    session = mem._driver.session()  # type: ignore[attr-defined]
    _, params = session.calls[-1]
    assert params["embedding"] == [0.0] * 768
    assert params["artifact_id"] == "art"


@pytest.mark.asyncio
async def test_similar_artifacts_returns_records() -> None:
    records = [{"id": "a", "kind": "coder", "content": "x", "score": 0.9}]
    mem = make_memory(records)
    mem._embed = AsyncMock(return_value=[0.0] * 768)  # type: ignore[method-assign]
    out = await mem.similar_artifacts("query", k=3)
    assert out == records


@pytest.mark.asyncio
async def test_task_lineage_returns_records() -> None:
    records = [{"subtask": "s", "agent": "coder", "artifacts": ["a"]}]
    mem = make_memory(records)
    out = await mem.task_lineage("task")
    assert out == records


@pytest.mark.asyncio
async def test_embed_reads_vector_from_client() -> None:
    mem = make_memory()
    response = SimpleNamespace(data=[SimpleNamespace(embedding=[1.0, 2.0, 3.0])])
    mem._embed_client.embeddings.create = AsyncMock(return_value=response)
    assert await mem._embed("text") == [1.0, 2.0, 3.0]
