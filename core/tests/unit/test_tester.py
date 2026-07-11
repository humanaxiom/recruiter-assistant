"""Unit tests for TesterAgent — tests/ allowlist, extraction, failures."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.agents.base import AgentInput
from src.agents.tester import TesterAgent


@pytest.fixture
def tester() -> TesterAgent:
    with patch("src.agents.base.AsyncOpenAI"):
        return TesterAgent()


@pytest.mark.asyncio
async def test_writes_test_files(tester: TesterAgent) -> None:
    output = "```python path=tests/unit/test_x.py\ndef test_x():\n    assert False\n```"
    with patch.object(tester, "_complete", AsyncMock(return_value=(output, 10))):
        out = await tester.run(AgentInput(task_id="T", task="spec"))
    assert out.success is True
    assert "tests/unit/test_x.py" in out.artifacts


@pytest.mark.asyncio
async def test_rejects_src_paths(tester: TesterAgent) -> None:
    output = "```python path=src/x.py\nx = 1\n```"
    with patch.object(tester, "_complete", AsyncMock(return_value=(output, 10))):
        out = await tester.run(AgentInput(task_id="T", task="spec"))
    assert out.success is False
    assert "no test files" in (out.error or "")


@pytest.mark.asyncio
async def test_exception_is_structured_failure(tester: TesterAgent) -> None:
    with patch.object(tester, "_complete", AsyncMock(side_effect=RuntimeError("x"))):
        out = await tester.run(AgentInput(task_id="T", task="spec"))
    assert out.success is False
    assert "x" in (out.error or "")
