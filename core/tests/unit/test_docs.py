"""Unit tests for DocsAgent — docs/ allowlist, nested-fence safety."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.agents.base import AgentInput
from src.agents.docs import DocsAgent


@pytest.fixture
def docs() -> DocsAgent:
    with patch("src.agents.base.AsyncOpenAI"):
        return DocsAgent()


@pytest.mark.asyncio
async def test_produces_adr_with_inner_mermaid(docs: DocsAgent) -> None:
    body = "# ADR\n\n```mermaid\ngraph LR\n  A-->B\n```\n\nDone.\n"
    output = f"```markdown path=docs/adr/001-x.md\n{body}```"
    with patch.object(docs, "_complete", AsyncMock(return_value=(output, 10))):
        out = await docs.run(AgentInput(task_id="T", task="changes"))
    assert out.success is True
    assert "```mermaid" in out.artifacts["docs/adr/001-x.md"]


@pytest.mark.asyncio
async def test_rejects_src_paths(docs: DocsAgent) -> None:
    output = "```python path=src/x.py\nx = 1\n```"
    with patch.object(docs, "_complete", AsyncMock(return_value=(output, 10))):
        out = await docs.run(AgentInput(task_id="T", task="changes"))
    assert out.success is True
    assert out.artifacts == {}


@pytest.mark.asyncio
async def test_exception_is_structured_failure(docs: DocsAgent) -> None:
    with patch.object(docs, "_complete", AsyncMock(side_effect=RuntimeError("boom"))):
        out = await docs.run(AgentInput(task_id="T", task="changes"))
    assert out.success is False
