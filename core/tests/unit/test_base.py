"""Unit tests for BaseAgent shared machinery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.base import AgentInput, AgentOutput, BaseAgent


class ConcreteAgent(BaseAgent):
    agent_id = "concrete"

    async def run(self, input_: AgentInput) -> AgentOutput:  # pragma: no cover
        return self._ok(input_, None, "noop")


@pytest.fixture
def agent() -> ConcreteAgent:
    with patch("src.agents.base.AsyncOpenAI"):
        return ConcreteAgent()


def test_ok_and_fail(agent: ConcreteAgent) -> None:
    inp = AgentInput(task_id="T", task="do")
    ok = agent._ok(inp, result={"x": 1}, reasoning="done", artifacts={"a": "b"})
    assert ok.success is True
    assert ok.artifacts == {"a": "b"}

    bad = agent._fail(inp, "boom")
    assert bad.success is False
    assert bad.error == "boom"


def test_log_appends_to_scratchpad(agent: ConcreteAgent) -> None:
    agent._log("hello")
    assert any("hello" in line for line in agent._scratchpad)


@pytest.mark.asyncio
async def test_memory_context_none_memory(agent: ConcreteAgent) -> None:
    assert await agent._memory_context("q") == ""


@pytest.mark.asyncio
async def test_memory_context_with_hits() -> None:
    memory = AsyncMock()
    memory.similar_artifacts = AsyncMock(
        return_value=[{"kind": "coder", "score": 0.9, "content": "prior"}]
    )
    with patch("src.agents.base.AsyncOpenAI"):
        agent = ConcreteAgent(memory=memory)
    ctx = await agent._memory_context("q")
    assert "prior" in ctx
    assert "Similar prior work" in ctx


@pytest.mark.asyncio
async def test_memory_context_no_hits() -> None:
    memory = AsyncMock()
    memory.similar_artifacts = AsyncMock(return_value=[])
    with patch("src.agents.base.AsyncOpenAI"):
        agent = ConcreteAgent(memory=memory)
    assert await agent._memory_context("q") == ""


@pytest.mark.asyncio
async def test_record_persists_subtask_and_artifacts() -> None:
    memory = AsyncMock()
    with patch("src.agents.base.AsyncOpenAI"):
        agent = ConcreteAgent(memory=memory)
    inp = AgentInput(task_id="T", task="do")
    out = agent._ok(inp, None, "r", artifacts={"src/a.py": "code"})
    await agent._record(inp, out)
    memory.record_subtask.assert_awaited_once()
    memory.record_artifact.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_noop_without_memory(agent: ConcreteAgent) -> None:
    inp = AgentInput(task_id="T", task="do")
    out = agent._ok(inp, None, "r", artifacts={"a": "b"})
    await agent._record(inp, out)  # must not raise


@pytest.mark.asyncio
async def test_complete_returns_text_and_tokens(agent: ConcreteAgent) -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
        usage=SimpleNamespace(total_tokens=42),
    )
    agent._client.chat.completions.create = AsyncMock(return_value=response)
    text, tokens = await agent._complete("prompt")
    assert text == "hi"
    assert tokens == 42
