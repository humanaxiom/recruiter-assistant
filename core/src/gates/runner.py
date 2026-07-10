"""Gate runner — executes all non-negotiable gates and returns structured results.

Gates (all must be GREEN to pass):
    ruff, black, mypy, unit tests, integration tests, coverage, branch-name
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from enum import Enum

from src.settings import get_settings

BRANCH_PATTERN = re.compile(r"^(agent|feat|fix|chore)/[a-zA-Z0-9._-]+$")


class GateStatus(str, Enum):
    GREEN = "green"
    RED = "red"
    SKIPPED = "skipped"


@dataclass
class GateResult:
    name: str
    status: GateStatus
    output: str
    duration_ms: int


@dataclass
class GateSuiteResult:
    results: list[GateResult]

    @property
    def all_green(self) -> bool:
        return all(r.status == GateStatus.GREEN for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if r.status == GateStatus.RED]

    def failure_report(self) -> str:
        """Formatted report fed back to the coder agent on iteration."""
        if self.all_green:
            return "ALL GATES GREEN ✅"
        lines = ["GATE FAILURES — fix these and re-run:\n"]
        for f in self.failures:
            lines.append(f"── {f.name} ──\n{f.output[-3000:]}\n")
        return "\n".join(lines)


GATE_COMMANDS: dict[str, list[str]] = {
    "ruff": ["ruff", "check", "src", "tests"],
    "black": ["black", "--check", "src", "tests"],
    "mypy": ["mypy", "src", "--strict"],
    "unit-tests": ["pytest", "tests/unit", "-q", "--timeout=120"],
    "integration-tests": ["pytest", "tests/integration", "-q", "--timeout=300"],
}


async def _run_command(name: str, cmd: list[str]) -> GateResult:
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    duration = int((time.monotonic() - start) * 1000)
    status = GateStatus.GREEN if proc.returncode == 0 else GateStatus.RED
    return GateResult(name, status, stdout.decode(errors="replace"), duration)


async def run_coverage_gate() -> GateResult:
    threshold = get_settings().coverage_threshold
    return await _run_command(
        "coverage",
        [
            "pytest", "tests/unit",
            f"--cov=src", "--cov-report=term",
            f"--cov-fail-under={threshold}", "-q",
        ],
    )


def run_branch_gate(branch: str) -> GateResult:
    ok = bool(BRANCH_PATTERN.match(branch)) and branch != "main"
    return GateResult(
        name="branch-name",
        status=GateStatus.GREEN if ok else GateStatus.RED,
        output=(
            f"branch '{branch}' OK"
            if ok
            else f"branch '{branch}' must match agent|feat|fix|chore/<slug> and not be main"
        ),
        duration_ms=0,
    )


async def run_all_gates(branch: str, include_integration: bool = True) -> GateSuiteResult:
    results: list[GateResult] = [run_branch_gate(branch)]

    for name, cmd in GATE_COMMANDS.items():
        if name == "integration-tests" and not include_integration:
            results.append(GateResult(name, GateStatus.SKIPPED, "skipped", 0))
            continue
        results.append(await _run_command(name, cmd))

    results.append(await run_coverage_gate())
    return GateSuiteResult(results)
