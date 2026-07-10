"""ReviewLoop — the core iterate-until-green mechanism.

Runs: gates → if red, feed failure report to CoderAgent → retry.
Caps at MAX_REVIEW_ITERATIONS then escalates to human.
Every iteration is recorded in Postgres (Run + GateResult rows) and
the produced artifacts land in Neo4j graph memory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from src.gates.runner import GateSuiteResult, run_all_gates
from src.settings import get_settings

logger = logging.getLogger(__name__)


class CoderProtocol(Protocol):
    """Any agent that can apply fixes given a failure report."""

    async def iterate(self, task: str, failure_report: str, iteration: int) -> str:
        """Apply fixes; return a summary of changes made."""
        ...


@dataclass
class LoopOutcome:
    success: bool
    iterations_used: int
    final_gates: GateSuiteResult
    escalated: bool
    history: list[str]


class ReviewLoop:
    def __init__(self, coder: CoderProtocol, branch: str) -> None:
        self._coder = coder
        self._branch = branch
        self._max_iterations = get_settings().max_review_iterations

    async def run(self, task: str) -> LoopOutcome:
        history: list[str] = []

        for iteration in range(1, self._max_iterations + 1):
            logger.info("Review loop iteration %d/%d", iteration, self._max_iterations)
            gates = await run_all_gates(self._branch)

            if gates.all_green:
                logger.info("All gates green on iteration %d", iteration)
                return LoopOutcome(
                    success=True,
                    iterations_used=iteration,
                    final_gates=gates,
                    escalated=False,
                    history=history,
                )

            report = gates.failure_report()
            logger.warning(
                "Iteration %d red: %s",
                iteration,
                [f.name for f in gates.failures],
            )
            summary = await self._coder.iterate(task, report, iteration)
            history.append(f"[iter {iteration}] {summary}")

        # Exhausted iterations — final gate check, then escalate
        final = await run_all_gates(self._branch)
        if final.all_green:
            return LoopOutcome(True, self._max_iterations, final, False, history)

        logger.error("Escalating to human after %d iterations", self._max_iterations)
        return LoopOutcome(
            success=False,
            iterations_used=self._max_iterations,
            final_gates=final,
            escalated=True,
            history=history,
        )
