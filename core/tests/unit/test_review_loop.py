"""Unit tests for ReviewLoop — iterate-until-green behaviour, escalation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.gates.review_loop import ReviewLoop
from src.gates.runner import GateResult, GateStatus, GateSuiteResult


def green_suite() -> GateSuiteResult:
    return GateSuiteResult([GateResult("all", GateStatus.GREEN, "ok", 1)])


def red_suite() -> GateSuiteResult:
    return GateSuiteResult([GateResult("mypy", GateStatus.RED, "type error", 1)])


class FakeCoder:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def iterate(self, task: str, failure_report: str, iteration: int) -> str:
        self.calls.append(iteration)
        return f"fixed iteration {iteration}"


@pytest.fixture
def coder() -> FakeCoder:
    return FakeCoder()


@pytest.mark.asyncio
async def test_green_first_try_no_coder_calls(coder: FakeCoder) -> None:
    with patch(
        "src.gates.review_loop.run_all_gates",
        AsyncMock(return_value=green_suite()),
    ):
        loop = ReviewLoop(coder=coder, branch="agent/t1-test")
        outcome = await loop.run("task")

    assert outcome.success is True
    assert outcome.iterations_used == 1
    assert coder.calls == []
    assert outcome.escalated is False


@pytest.mark.asyncio
async def test_red_then_green_iterates_once(coder: FakeCoder) -> None:
    with patch(
        "src.gates.review_loop.run_all_gates",
        AsyncMock(side_effect=[red_suite(), green_suite()]),
    ):
        loop = ReviewLoop(coder=coder, branch="agent/t2-test")
        outcome = await loop.run("task")

    assert outcome.success is True
    assert outcome.iterations_used == 2
    assert coder.calls == [1]
    assert outcome.history == ["[iter 1] fixed iteration 1"]


@pytest.mark.asyncio
async def test_escalates_after_max_iterations(coder: FakeCoder) -> None:
    with (
        patch(
            "src.gates.review_loop.run_all_gates",
            AsyncMock(return_value=red_suite()),
        ),
        patch("src.gates.review_loop.get_settings") as mock_settings,
    ):
        mock_settings.return_value.max_review_iterations = 3
        loop = ReviewLoop(coder=coder, branch="agent/t3-test")
        outcome = await loop.run("task")

    assert outcome.success is False
    assert outcome.escalated is True
    assert coder.calls == [1, 2, 3]


@pytest.mark.asyncio
async def test_final_check_can_rescue_last_iteration(coder: FakeCoder) -> None:
    """If the final coder pass fixes things, the post-loop check catches it."""
    with (
        patch(
            "src.gates.review_loop.run_all_gates",
            AsyncMock(
                side_effect=[red_suite(), red_suite(), red_suite(), green_suite()]
            ),
        ),
        patch("src.gates.review_loop.get_settings") as mock_settings,
    ):
        mock_settings.return_value.max_review_iterations = 3
        loop = ReviewLoop(coder=coder, branch="agent/t4-test")
        outcome = await loop.run("task")

    assert outcome.success is True
    assert outcome.escalated is False
    assert coder.calls == [1, 2, 3]
