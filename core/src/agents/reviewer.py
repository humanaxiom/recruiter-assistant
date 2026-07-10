"""ReviewerAgent — structured code review of a diff against project rules."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from src.agents.base import AgentInput, AgentOutput, BaseAgent


class Severity(str, Enum):
    CRITICAL = "critical"   # blocks merge
    MAJOR = "major"         # must fix before merge
    MINOR = "minor"         # should fix
    NIT = "nit"             # optional


@dataclass
class Finding:
    severity: Severity
    file: str
    line: int | None
    message: str
    suggestion: str | None = None


@dataclass
class Review:
    approved: bool
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""

    @property
    def blocking(self) -> list[Finding]:
        return [
            f for f in self.findings
            if f.severity in (Severity.CRITICAL, Severity.MAJOR)
        ]


class ReviewerAgent(BaseAgent):
    agent_id = "reviewer"
    temperature = 0.1
    system_prompt = """You are the ReviewerAgent in an offline TDD harness.
Review the diff against project rules. Respond ONLY with valid JSON:
{"approved": bool, "summary": str,
 "findings": [{"severity": "critical|major|minor|nit", "file": str,
               "line": int|null, "message": str, "suggestion": str|null}]}

Review for:
- Data placement: Postgres=transactions, Neo4j=graph/vector only, Redis=queue only
- Type safety: no unjustified `# type: ignore`, no `Any` without a comment
- Async correctness: no blocking I/O in async paths, no un-awaited coroutines
- Test integrity: tests were NOT weakened or deleted to force green
- Config discipline: everything through src/settings.py
- Offline rule: no cloud API endpoints introduced
- Security: injection, secrets in code, unvalidated input at API boundaries
approved=true ONLY if there are zero critical and zero major findings."""

    async def run(self, input_: AgentInput) -> AgentOutput:
        diff = input_.context.get("diff", "")
        if not diff:
            return self._fail(input_, "No diff provided in context['diff']")

        self._log(f"Reviewing diff ({len(diff)} chars)")
        try:
            text, tokens = await self._complete(
                f"Task: {input_.task}\n\nDiff to review:\n{diff[:24_000]}"
            )
            data = json.loads(text.strip().removeprefix("```json").removesuffix("```"))
            review = Review(
                approved=bool(data["approved"]),
                summary=data.get("summary", ""),
                findings=[
                    Finding(
                        severity=Severity(f["severity"]),
                        file=f["file"],
                        line=f.get("line"),
                        message=f["message"],
                        suggestion=f.get("suggestion"),
                    )
                    for f in data.get("findings", [])
                ],
            )
            # Consistency guard: model can't approve with blocking findings
            if review.blocking:
                review.approved = False

            output = self._ok(
                input_, review, review.summary,
                artifacts={"review.json": json.dumps(data, indent=2)},
                tokens=tokens,
            )
            await self._record(input_, output)
            return output
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            return self._fail(input_, f"Invalid review JSON: {exc}")
        except Exception as exc:  # noqa: BLE001
            return self._fail(input_, str(exc))
