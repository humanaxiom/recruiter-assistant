"""Unit tests for SecurityAgent — pass/fail and blocking severities."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.base import AgentInput
from src.agents.security import SecurityAgent


@pytest.fixture
def security() -> SecurityAgent:
    with patch("src.agents.base.AsyncOpenAI"):
        return SecurityAgent()


def _input(diff: str = "code") -> AgentInput:
    return AgentInput(task_id="T", task="audit", context={"diff": diff})


@pytest.mark.asyncio
async def test_no_target_fails(security: SecurityAgent) -> None:
    out = await security.run(AgentInput(task_id="T", task="audit"))
    assert out.success is False


@pytest.mark.asyncio
async def test_clean_audit_passes(security: SecurityAgent) -> None:
    payload = {"passed": True, "findings": []}
    with patch.object(
        security, "_complete", AsyncMock(return_value=(json.dumps(payload), 10))
    ):
        out = await security.run(_input())
    assert out.success is True
    assert out.result.passed is True


@pytest.mark.asyncio
async def test_high_severity_forces_fail(security: SecurityAgent) -> None:
    payload = {
        "passed": True,
        "findings": [
            {
                "category": "injection",
                "severity": "high",
                "file": "src/x.py",
                "message": "raw cypher",
                "remediation": "parametrize",
            }
        ],
    }
    with patch.object(
        security, "_complete", AsyncMock(return_value=(json.dumps(payload), 10))
    ):
        out = await security.run(_input())
    assert out.result.passed is False


@pytest.mark.asyncio
async def test_invalid_report_fails(security: SecurityAgent) -> None:
    with patch.object(security, "_complete", AsyncMock(return_value=("nope", 1))):
        out = await security.run(_input())
    assert out.success is False
