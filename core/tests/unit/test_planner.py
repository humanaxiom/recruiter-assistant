"""Unit tests for PlannerAgent — plan validation, TDD ordering, dependency checks."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.base import AgentInput
from src.agents.planner import Plan, PlannerAgent

VALID_PLAN = {
    "plan_id": "p1",
    "goal": "Add rate limiter",
    "reasoning": "Tests first, then implement, review, secure, document.",
    "subtasks": [
        {"id": "t1", "agent": "tester", "task": "failing tests", "depends_on": []},
        {"id": "t2", "agent": "coder", "task": "implement", "depends_on": ["t1"]},
        {"id": "t3", "agent": "reviewer", "task": "review", "depends_on": ["t2"]},
        {"id": "t4", "agent": "security", "task": "audit", "depends_on": ["t2"]},
        {"id": "t5", "agent": "docs", "task": "document", "depends_on": ["t3", "t4"]},
    ],
}


@pytest.fixture
def planner() -> PlannerAgent:
    with patch("src.agents.base.AsyncOpenAI"):
        return PlannerAgent()


def patched_complete(payload: dict) -> AsyncMock:  # type: ignore[type-arg]
    return AsyncMock(return_value=(json.dumps(payload), 100))


@pytest.mark.asyncio
async def test_valid_plan_accepted(planner: PlannerAgent) -> None:
    with patch.object(planner, "_complete", patched_complete(VALID_PLAN)):
        out = await planner.run(AgentInput(task_id="T1", task="Add rate limiter"))
    assert out.success is True
    plan: Plan = out.result
    assert len(plan.subtasks) == 5
    assert "plan.json" in out.artifacts


@pytest.mark.asyncio
async def test_tdd_violation_rejected(planner: PlannerAgent) -> None:
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["subtasks"][0], bad["subtasks"][1] = bad["subtasks"][1], bad["subtasks"][0]
    with patch.object(planner, "_complete", patched_complete(bad)):
        out = await planner.run(AgentInput(task_id="T2", task="x"))
    assert out.success is False
    assert "TDD violation" in (out.error or "")


@pytest.mark.asyncio
async def test_unknown_agent_rejected(planner: PlannerAgent) -> None:
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["subtasks"][0]["agent"] = "wizard"
    with patch.object(planner, "_complete", patched_complete(bad)):
        out = await planner.run(AgentInput(task_id="T3", task="x"))
    assert out.success is False
    assert "unknown agent" in (out.error or "")


@pytest.mark.asyncio
async def test_dangling_dependency_rejected(planner: PlannerAgent) -> None:
    bad = json.loads(json.dumps(VALID_PLAN))
    bad["subtasks"][1]["depends_on"] = ["nope"]
    with patch.object(planner, "_complete", patched_complete(bad)):
        out = await planner.run(AgentInput(task_id="T4", task="x"))
    assert out.success is False
    assert "unknown 'nope'" in (out.error or "")


@pytest.mark.asyncio
async def test_empty_plan_rejected(planner: PlannerAgent) -> None:
    bad = {**VALID_PLAN, "subtasks": []}
    with patch.object(planner, "_complete", patched_complete(bad)):
        out = await planner.run(AgentInput(task_id="T5", task="x"))
    assert out.success is False


@pytest.mark.asyncio
async def test_invalid_json_fails_gracefully(planner: PlannerAgent) -> None:
    with patch.object(planner, "_complete", AsyncMock(return_value=("not json", 5))):
        out = await planner.run(AgentInput(task_id="T6", task="x"))
    assert out.success is False
    assert "Invalid plan" in (out.error or "")
