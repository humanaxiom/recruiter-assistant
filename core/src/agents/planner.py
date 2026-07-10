"""PlannerAgent — decomposes a task into an ordered subagent plan."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.agents.base import AgentInput, AgentOutput, BaseAgent

VALID_AGENTS = {"tester", "coder", "reviewer", "docs", "security"}


@dataclass
class Subtask:
    id: str
    agent: str
    task: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    plan_id: str
    goal: str
    reasoning: str
    subtasks: list[Subtask]


class PlannerAgent(BaseAgent):
    agent_id = "planner"
    temperature = 0.1
    system_prompt = """You are the PlannerAgent in an offline TDD multi-agent harness.
Decompose the task into subtasks assigned to: tester, coder, reviewer, docs, security.

HARD RULES:
- tester ALWAYS precedes coder (TDD: failing tests first)
- reviewer always follows coder
- security is included when the task touches auth, input handling, secrets, or network
- docs is always last
- Respond with ONLY valid JSON, no markdown fences, matching:
{"plan_id": str, "goal": str, "reasoning": str,
 "subtasks": [{"id": str, "agent": str, "task": str, "depends_on": [str]}]}"""

    async def run(self, input_: AgentInput) -> AgentOutput:
        self._log(f"Planning: {input_.task[:80]}")
        memory_ctx = await self._memory_context(input_.task)

        try:
            text, tokens = await self._complete(
                f"{memory_ctx}Task: {input_.task}\n\n"
                f"Context: {json.dumps(input_.context)}"
            )
            data = json.loads(text.strip().removeprefix("```json").removesuffix("```"))
            plan = self._validate(data)
            output = self._ok(
                input_, plan, plan.reasoning,
                artifacts={"plan.json": json.dumps(data, indent=2)},
                tokens=tokens,
            )
            await self._record(input_, output)
            return output
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            return self._fail(input_, f"Invalid plan: {exc}")
        except Exception as exc:  # noqa: BLE001 — surface as structured failure
            return self._fail(input_, str(exc))

    def _validate(self, data: dict) -> Plan:  # type: ignore[type-arg]
        subtasks = [
            Subtask(
                id=s["id"],
                agent=s["agent"],
                task=s["task"],
                depends_on=s.get("depends_on", []),
            )
            for s in data["subtasks"]
        ]
        if not subtasks:
            raise ValueError("plan has no subtasks")

        ids = {s.id for s in subtasks}
        for s in subtasks:
            if s.agent not in VALID_AGENTS:
                raise ValueError(f"unknown agent '{s.agent}'")
            for dep in s.depends_on:
                if dep not in ids:
                    raise ValueError(f"subtask {s.id} depends on unknown '{dep}'")

        order = [s.agent for s in subtasks]
        if "coder" in order and "tester" in order:
            if order.index("tester") > order.index("coder"):
                raise ValueError("TDD violation: tester must precede coder")

        return Plan(
            plan_id=data["plan_id"],
            goal=data["goal"],
            reasoning=data["reasoning"],
            subtasks=subtasks,
        )
