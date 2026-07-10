"""Unit tests for Orchestrator — topological order, merge-blocking gates."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.agents.base import AgentOutput
from src.agents.orchestrator import Orchestrator, PipelineResult
from src.agents.planner import Plan, Subtask
from src.agents.reviewer import Review


def make_plan(subtasks: list[Subtask]) -> Plan:
    return Plan(plan_id="p", goal="g", reasoning="r", subtasks=subtasks)


def ok_output(agent_id: str, result: object = None) -> AgentOutput:
    return AgentOutput(
        task_id="T", agent_id=agent_id, success=True,
        result=result, reasoning="ok",
    )


@pytest.fixture
def orch() -> Orchestrator:
    with patch("src.agents.base.AsyncOpenAI"):
        return Orchestrator(branch="agent/T-test")


def test_topological_order_respects_dependencies(orch: Orchestrator) -> None:
    subtasks = [
        Subtask(id="c", agent="docs", task="", depends_on=["b"]),
        Subtask(id="a", agent="tester", task="", depends_on=[]),
        Subtask(id="b", agent="reviewer", task="", depends_on=["a"]),
    ]
    ordered = [s.id for s in orch._ordered(subtasks)]
    assert ordered.index("a") < ordered.index("b") < ordered.index("c")


def test_cycle_detection_raises(orch: Orchestrator) -> None:
    from graphlib import CycleError

    subtasks = [
        Subtask(id="a", agent="tester", task="", depends_on=["b"]),
        Subtask(id="b", agent="coder", task="", depends_on=["a"]),
    ]
    with pytest.raises(CycleError):
        orch._ordered(subtasks)


@pytest.mark.asyncio
async def test_reviewer_rejection_blocks_pipeline(orch: Orchestrator) -> None:
    plan = make_plan([
        Subtask(id="t1", agent="tester", task="tests", depends_on=[]),
        Subtask(id="t2", agent="reviewer", task="review", depends_on=["t1"]),
        Subtask(id="t3", agent="docs", task="docs", depends_on=["t2"]),
    ])
    rejected = Review(approved=False, summary="major issues")

    orch._planner.run = AsyncMock(return_value=ok_output("planner", plan))  # type: ignore[method-assign]
    orch._agents["tester"].run = AsyncMock(return_value=ok_output("tester", ["f"]))  # type: ignore[method-assign]
    orch._agents["reviewer"].run = AsyncMock(  # type: ignore[method-assign]
        return_value=ok_output("reviewer", rejected)
    )
    docs_mock = AsyncMock()
    orch._agents["docs"].run = docs_mock  # type: ignore[method-assign]

    result: PipelineResult = await orch.run("T", "spec")

    assert result.success is False
    assert result.blocked_by == "reviewer"
    docs_mock.assert_not_called()  # docs never runs after a block


@pytest.mark.asyncio
async def test_planner_failure_blocks_pipeline(orch: Orchestrator) -> None:
    orch._planner.run = AsyncMock(  # type: ignore[method-assign]
        return_value=AgentOutput(
            task_id="T", agent_id="planner", success=False,
            result=None, reasoning="", error="bad json",
        )
    )
    result = await orch.run("T", "spec")
    assert result.success is False
    assert result.blocked_by == "planner"


@pytest.mark.asyncio
async def test_full_green_pipeline(orch: Orchestrator) -> None:
    plan = make_plan([
        Subtask(id="t1", agent="tester", task="tests", depends_on=[]),
        Subtask(id="t2", agent="reviewer", task="review", depends_on=["t1"]),
    ])
    orch._planner.run = AsyncMock(return_value=ok_output("planner", plan))  # type: ignore[method-assign]
    orch._agents["tester"].run = AsyncMock(return_value=ok_output("tester", ["f"]))  # type: ignore[method-assign]
    orch._agents["reviewer"].run = AsyncMock(  # type: ignore[method-assign]
        return_value=ok_output("reviewer", Review(approved=True, summary="lgtm"))
    )
    result = await orch.run("T", "spec")
    assert result.success is True
    assert result.blocked_by is None
    assert set(result.outputs) == {"t1", "t2"}
