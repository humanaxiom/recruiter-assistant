"""Unit tests for the gate runner — branch gate, suite aggregation, reporting."""

from __future__ import annotations

import pytest

from src.gates.runner import (
    GateResult,
    GateStatus,
    GateSuiteResult,
    run_branch_gate,
)


class TestBranchGate:
    @pytest.mark.parametrize(
        "branch",
        [
            "agent/a1b2c3d4-rate-limiter",
            "feat/new-dashboard",
            "fix/neo4j-timeout",
            "chore/bump-deps",
        ],
    )
    def test_valid_branches_pass(self, branch: str) -> None:
        assert run_branch_gate(branch).status == GateStatus.GREEN

    @pytest.mark.parametrize(
        "branch",
        [
            "main",
            "my-random-branch",
            "agent/",
            "release/v1",
            "agent/has spaces",
        ],
    )
    def test_invalid_branches_fail(self, branch: str) -> None:
        assert run_branch_gate(branch).status == GateStatus.RED

    def test_main_is_always_rejected(self) -> None:
        result = run_branch_gate("main")
        assert result.status == GateStatus.RED
        assert "main" in result.output


class TestGateSuiteResult:
    def _make(self, statuses: list[GateStatus]) -> GateSuiteResult:
        return GateSuiteResult(
            results=[
                GateResult(f"gate-{i}", s, f"output-{i}", 10)
                for i, s in enumerate(statuses)
            ]
        )

    def test_all_green(self) -> None:
        suite = self._make([GateStatus.GREEN, GateStatus.GREEN])
        assert suite.all_green is True
        assert suite.failures == []
        assert "ALL GATES GREEN" in suite.failure_report()

    def test_one_red_fails_suite(self) -> None:
        suite = self._make([GateStatus.GREEN, GateStatus.RED, GateStatus.GREEN])
        assert suite.all_green is False
        assert len(suite.failures) == 1

    def test_skipped_does_not_fail_suite(self) -> None:
        suite = self._make([GateStatus.GREEN, GateStatus.SKIPPED])
        # SKIPPED is not GREEN — suite must not be all_green (strict policy)
        assert suite.all_green is False

    def test_failure_report_contains_gate_output(self) -> None:
        suite = self._make([GateStatus.RED])
        report = suite.failure_report()
        assert "gate-0" in report
        assert "output-0" in report

    def test_failure_report_truncates_long_output(self) -> None:
        long_output = "x" * 10_000
        suite = GateSuiteResult(
            results=[GateResult("mypy", GateStatus.RED, long_output, 5)]
        )
        report = suite.failure_report()
        assert len(report) < 5_000
