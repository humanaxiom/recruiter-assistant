"""Unit tests for CoderAgent — extraction, writing, memory recording."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.agents.base import AgentInput
from src.agents.coder import CoderAgent


def _make_coder(memory: object = None) -> tuple[CoderAgent, dict[str, str]]:
    written: dict[str, str] = {}
    with patch("src.agents.base.AsyncOpenAI"):
        coder = CoderAgent(
            memory=memory,  # type: ignore[arg-type]
            writer=lambda p, c: written.__setitem__(p, c),
        )
    return coder, written


@pytest.mark.asyncio
async def test_iterate_writes_src_files() -> None:
    coder, written = _make_coder()
    output = "```python path=src/impl.py\nVALUE = 1\n```"
    with patch.object(coder, "_complete", AsyncMock(return_value=(output, 5))):
        summary = await coder.iterate("T", "task", "mypy failed", 1)
    assert "Applied 1 file change(s)" in summary
    assert written == {"src/impl.py": "VALUE = 1\n"}


@pytest.mark.asyncio
async def test_iterate_refuses_paths_outside_project() -> None:
    coder, written = _make_coder()
    output = "```python path=../evil.py\nBAD = 1\n```"
    with patch.object(coder, "_complete", AsyncMock(return_value=(output, 5))):
        await coder.iterate("T", "task", "report", 1)
    assert written == {}


@pytest.mark.asyncio
async def test_iterate_records_artifacts_to_memory() -> None:
    memory = AsyncMock()
    coder, _ = _make_coder(memory=memory)
    memory.similar_artifacts = AsyncMock(return_value=[])
    output = "```python path=src/impl.py\nVALUE = 1\n```"
    with patch.object(coder, "_complete", AsyncMock(return_value=(output, 5))):
        await coder.iterate("T", "task", "report", 1)
    memory.record_subtask.assert_awaited_once()
    memory.record_artifact.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_one_shot() -> None:
    coder, written = _make_coder()
    output = "```python path=src/impl.py\nVALUE = 2\n```"
    with patch.object(coder, "_complete", AsyncMock(return_value=(output, 5))):
        out = await coder.run(AgentInput(task_id="T", task="do it"))
    assert out.success is True
    assert out.artifacts == {"src/impl.py": "VALUE = 2\n"}
    assert written == {"src/impl.py": "VALUE = 2\n"}
