"""Unit tests for ReviewerAgent — approval logic, blocking downgrade."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.base import AgentInput
from src.agents.reviewer import ReviewerAgent


@pytest.fixture
def reviewer() -> ReviewerAgent:
    with patch("src.agents.base.AsyncOpenAI"):
        return ReviewerAgent()


def _input(diff: str = "some diff") -> AgentInput:
    return AgentInput(task_id="T", task="review", context={"diff": diff})


@pytest.mark.asyncio
async def test_no_diff_fails(reviewer: ReviewerAgent) -> None:
    out = await reviewer.run(AgentInput(task_id="T", task="review"))
    assert out.success is False


@pytest.mark.asyncio
async def test_clean_review_approved(reviewer: ReviewerAgent) -> None:
    payload = {"approved": True, "summary": "lgtm", "findings": []}
    with patch.object(
        reviewer, "_complete", AsyncMock(return_value=(json.dumps(payload), 10))
    ):
        out = await reviewer.run(_input())
    assert out.success is True
    assert out.result.approved is True


@pytest.mark.asyncio
async def test_blocking_finding_forces_rejection(reviewer: ReviewerAgent) -> None:
    payload = {
        "approved": True,  # model tries to approve...
        "summary": "has issues",
        "findings": [
            {
                "severity": "critical",
                "file": "src/x.py",
                "line": 1,
                "message": "sql injection",
                "suggestion": "parametrize",
            }
        ],
    }
    with patch.object(
        reviewer, "_complete", AsyncMock(return_value=(json.dumps(payload), 10))
    ):
        out = await reviewer.run(_input())
    assert out.result.approved is False  # ...guard overrides it


@pytest.mark.asyncio
async def test_invalid_json_fails(reviewer: ReviewerAgent) -> None:
    with patch.object(reviewer, "_complete", AsyncMock(return_value=("garbage", 1))):
        out = await reviewer.run(_input())
    assert out.success is False
    assert "Invalid review JSON" in (out.error or "")
