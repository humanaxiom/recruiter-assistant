"""SecurityAgent — focused security audit of changes (offline, rule-driven)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.agents.base import AgentInput, AgentOutput, BaseAgent


@dataclass
class SecurityFinding:
    category: str        # injection | secrets | authz | input-validation | dos | crypto
    severity: str        # critical | high | medium | low
    file: str
    message: str
    remediation: str


@dataclass
class SecurityReport:
    passed: bool
    findings: list[SecurityFinding] = field(default_factory=list)


class SecurityAgent(BaseAgent):
    agent_id = "security"
    temperature = 0.0
    system_prompt = """You are the SecurityAgent in an offline harness for a
FastAPI + Postgres + Neo4j + Redis application. Audit the provided diff/code.

Check for:
- SQL/Cypher injection (raw string interpolation into queries — parameters required)
- Secrets or credentials hardcoded anywhere
- Missing input validation at FastAPI boundaries (must use Pydantic models)
- Path traversal in any file-writing code (agents write files!)
- SSRF / unexpected egress (this codebase must stay offline — flag ANY new external URL)
- Unbounded resource use (missing timeouts, unpaginated queries, unbounded Redis growth)

Respond ONLY with valid JSON:
{"passed": bool, "findings": [{"category": str, "severity": str,
  "file": str, "message": str, "remediation": str}]}
passed=false if any critical or high finding exists."""

    async def run(self, input_: AgentInput) -> AgentOutput:
        target = input_.context.get("diff") or input_.context.get("code", "")
        if not target:
            return self._fail(input_, "No diff/code in context")

        self._log("Running security audit")
        try:
            text, tokens = await self._complete(
                f"Task context: {input_.task}\n\nAudit target:\n{target[:24_000]}"
            )
            data = json.loads(text.strip().removeprefix("```json").removesuffix("```"))
            findings = [SecurityFinding(**f) for f in data.get("findings", [])]
            has_blocking = any(
                f.severity in ("critical", "high") for f in findings
            )
            report = SecurityReport(
                passed=bool(data["passed"]) and not has_blocking,
                findings=findings,
            )
            output = self._ok(
                input_, report,
                reasoning=f"{len(findings)} finding(s); passed={report.passed}",
                artifacts={"security-report.json": json.dumps(data, indent=2)},
                tokens=tokens,
            )
            await self._record(input_, output)
            return output
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            return self._fail(input_, f"Invalid security report: {exc}")
        except Exception as exc:  # noqa: BLE001
            return self._fail(input_, str(exc))
