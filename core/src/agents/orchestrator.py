"""Orchestrator — dispatches subagents per the Planner's plan.

Responsibilities:
- Get a Plan from PlannerAgent
- Topologically execute subtasks respecting depends_on
- Route each subtask to its subagent, passing prior outputs as context
- Run the ReviewLoop (iterate-until-green) after the coder stage
- Enforce: reviewer approval + security pass are merge-blocking
- Record everything to Postgres ledger (via caller) and Neo4j lineage
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from graphlib import TopologicalSorter

from src.agents.base import AgentInput, AgentOutput, BaseAgent
from src.agents.coder import CoderAgent
from src.agents.docs import DocsAgent
from src.agents.planner import Plan, PlannerAgent, Subtask
from src.agents.reviewer import ReviewerAgent
from src.agents.security import SecurityAgent
from src.agents.tester import TesterAgent
from src.gates.review_loop import LoopOutcome, ReviewLoop
from src.memory.graph import GraphMemory

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    task_id: str
    success: bool
    plan: Plan | None
    outputs: dict[str, AgentOutput] = field(default_factory=dict)
    loop_outcome: LoopOutcome | None = None
    blocked_by: str | None = None  # "reviewer" | "security" | "planner" | agent id


class Orchestrator:
    def __init__(self, branch: str, memory: GraphMemory | None = None) -> None:
        self._branch = branch
        self._memory = memory
        self._planner = PlannerAgent(memory=memory)
        self._agents: dict[str, BaseAgent] = {
            "tester": TesterAgent(memory=memory),
            "coder": CoderAgent(memory=memory),  # type: ignore[dict-item]
            "reviewer": ReviewerAgent(memory=memory),
            "docs": DocsAgent(memory=memory),
            "security": SecurityAgent(memory=memory),
        }

    async def run(self, task_id: str, task_spec: str) -> PipelineResult:
        # 1. Plan
        plan_output = await self._planner.run(
            AgentInput(task_id=task_id, task=task_spec)
        )
        if not plan_output.success:
            return PipelineResult(
                task_id, False, None, blocked_by="planner"
            )
        plan: Plan = plan_output.result
        logger.info("Plan %s: %d subtasks", plan.plan_id, len(plan.subtasks))

        # 2. Topological execution
        result = PipelineResult(task_id, True, plan)
        for subtask in self._ordered(plan.subtasks):
            output = await self._dispatch(task_id, task_spec, subtask, result)
            result.outputs[subtask.id] = output

            if not output.success:
                result.success = False
                result.blocked_by = subtask.agent
                logger.error("Pipeline blocked at %s: %s", subtask.agent, output.error)
                return result

            # Merge-blocking checks
            if subtask.agent == "reviewer" and not output.result.approved:
                result.success = False
                result.blocked_by = "reviewer"
                logger.warning("Reviewer did not approve — pipeline blocked")
                return result
            if subtask.agent == "security" and not output.result.passed:
                result.success = False
                result.blocked_by = "security"
                logger.warning("Security audit failed — pipeline blocked")
                return result

        return result

    # ── Internals ──────────────────────────────────────────────────────────

    def _ordered(self, subtasks: list[Subtask]) -> list[Subtask]:
        """Dependency-respecting order via topological sort."""
        by_id = {s.id: s for s in subtasks}
        sorter: TopologicalSorter[str] = TopologicalSorter(
            {s.id: set(s.depends_on) for s in subtasks}
        )
        return [by_id[sid] for sid in sorter.static_order()]

    async def _dispatch(
        self,
        task_id: str,
        task_spec: str,
        subtask: Subtask,
        result: PipelineResult,
    ) -> AgentOutput:
        agent = self._agents[subtask.agent]
        logger.info("Dispatching %s → %s", subtask.id, subtask.agent)

        context = self._build_context(subtask, result)
        input_ = AgentInput(
            task_id=task_id,
            task=subtask.task,
            context=context,
            prior_outputs=result.outputs,
        )

        # Coder runs inside the iterate-until-green ReviewLoop
        if subtask.agent == "coder":
            loop = ReviewLoop(coder=self._agents["coder"], branch=self._branch)  # type: ignore[arg-type]
            outcome = await loop.run(subtask.task)
            result.loop_outcome = outcome
            if outcome.success:
                return AgentOutput(
                    task_id=task_id,
                    agent_id="coder",
                    success=True,
                    result={"iterations": outcome.iterations_used},
                    reasoning="; ".join(outcome.history) or "green first try",
                )
            return AgentOutput(
                task_id=task_id,
                agent_id="coder",
                success=False,
                result=None,
                reasoning="",
                error=(
                    f"Escalated after {outcome.iterations_used} iterations. "
                    f"Failures: {outcome.final_gates.failure_report()[:2000]}"
                ),
            )

        return await agent.run(input_)

    def _build_context(
        self, subtask: Subtask, result: PipelineResult
    ) -> dict[str, object]:
        """Feed each agent what it needs from upstream outputs."""
        context: dict[str, object] = {}
        if subtask.agent in ("reviewer", "security"):
            # In a live run the harness injects `git diff` here; upstream
            # artifacts serve as the audit target for library usage.
            artifacts: dict[str, str] = {}
            for dep_id in subtask.depends_on:
                dep = result.outputs.get(dep_id)
                if dep:
                    artifacts.update(dep.artifacts)
            context["diff"] = "\n\n".join(
                f"--- {p} ---\n{c}" for p, c in artifacts.items()
            )
        if subtask.agent == "docs":
            context["changes_summary"] = "\n".join(
                f"{o.agent_id}: {o.reasoning}" for o in result.outputs.values()
            )
        return context
