"""ReviewLoop — the core iterate-until-green mechanism.

Runs: gates → if red, feed failure report to CoderAgent → retry.
Caps at MAX_REVIEW_ITERATIONS then escalates to human.

Produced artifacts land in Neo4j graph memory via the coder's own
lineage recording (``CoderAgent.iterate`` → ``_record``). Postgres
Run/GateResult ledger rows are written by the caller (worker/orchestrator),
not here — this class stays free of a DB session so it is trivially testable.

Any exception from the gate runner or the coder is caught and converted into
a structured escalation rather than crashing the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from src.gates.runner import GateResult, GateStatus, GateSuiteResult, run_all_gates
from src.settings import get_settings

logger = logging.getLogger(__name__)


class CoderProtocol(Protocol):
    """Any agent that can apply fixes given a failure report."""

    async def iterate(
        self, task_id: str, task: str, failure_report: str, iteration: int
    ) -> str:
        """Apply fixes; return a summary of changes made."""
        ...


@dataclass
class LoopOutcome:
    success: bool
    iterations_used: int
    final_gates: GateSuiteResult
    escalated: bool
    history: list[str]


def _error_suite(message: str) -> GateSuiteResult:
    return GateSuiteResult([GateResult("loop-error", GateStatus.RED, message, 0)])


class ReviewLoop:
    def __init__(self, coder: CoderProtocol, branch: str) -> None:
        self._coder = coder
        self._branch = branch
        self._max_iterations = get_settings().max_review_iterations

    async def run(self, task_id: str, task: str) -> LoopOutcome:
        history: list[str] = []

        for iteration in range(1, self._max_iterations + 1):
            logger.info("Review loop iteration %d/%d", iteration, self._max_iterations)
            try:
                gates = await run_all_gates(self._branch)
            except Exception as exc:  # noqa: BLE001 — surface as escalation
                logger.exception("Gate runner crashed on iteration %d", iteration)
                history.append(f"[iter {iteration}] gate runner error: {exc}")
                return LoopOutcome(
                    False, iteration, _error_suite(str(exc)), True, history
                )

            if gates.all_green:
                logger.info("All gates green on iteration %d", iteration)
                return LoopOutcome(True, iteration, gates, False, history)

            report = gates.failure_report()
            logger.warning(
                "Iteration %d red: %s", iteration, [f.name for f in gates.failures]
            )
            try:
                summary = await self._coder.iterate(task_id, task, report, iteration)
            except Exception as exc:  # noqa: BLE001 — surface as escalation
                logger.exception("Coder crashed on iteration %d", iteration)
                history.append(f"[iter {iteration}] coder error: {exc}")
                return LoopOutcome(False, iteration, gates, True, history)
            history.append(f"[iter {iteration}] {summary}")

        # Exhausted iterations — final gate check, then escalate
        try:
            final = await run_all_gates(self._branch)
        except Exception as exc:  # noqa: BLE001 — surface as escalation
            return LoopOutcome(
                False, self._max_iterations, _error_suite(str(exc)), True, history
            )
        if final.all_green:
            return LoopOutcome(True, self._max_iterations, final, False, history)

        logger.error("Escalating to human after %d iterations", self._max_iterations)
        return LoopOutcome(False, self._max_iterations, final, True, history)
