"""DocsAgent — updates docs, ADRs, and Mermaid diagrams after changes land."""

from __future__ import annotations

from src.agents.base import AgentInput, AgentOutput, BaseAgent
from src.agents.parsing import extract_file_blocks

ALLOWED_PREFIXES = ("docs/", "README")


class DocsAgent(BaseAgent):
    agent_id = "docs"
    temperature = 0.3
    system_prompt = """You are the DocsAgent in an offline TDD harness.
Given a summary of implemented changes, produce documentation updates.

RULES:
- ADRs go in docs/adr/NNN-title.md with sections:
  Status / Date / Context / Decision / Architecture Diagram (Mermaid) /
  Consequences / Alternatives Considered
- Architecture diagrams are Mermaid, in docs/diagrams/ or embedded in ADRs
- Update README sections only when behaviour or interfaces changed
- Only touch files under docs/ or README.md — never src/ or tests/
- Concise, factual prose; no marketing language

Output every file as:
```markdown path=docs/adr/00X-name.md
<complete file content>
```"""

    async def run(self, input_: AgentInput) -> AgentOutput:
        changes = input_.context.get("changes_summary", input_.task)
        self._log("Generating documentation updates")

        try:
            memory_ctx = await self._memory_context(input_.task)
            text, tokens = await self._complete(
                f"{memory_ctx}Changes implemented:\n{changes}\n\n"
                "Produce the documentation updates now."
            )
            artifacts = {
                path: content
                for path, content in extract_file_blocks(text).items()
                if path.startswith(ALLOWED_PREFIXES) and ".." not in path
            }
            output = self._ok(
                input_,
                result=list(artifacts.keys()),
                reasoning=f"Produced {len(artifacts)} doc file(s)",
                artifacts=artifacts,
                tokens=tokens,
            )
            await self._record(input_, output)
            return output
        except Exception as exc:  # noqa: BLE001
            return self._fail(input_, str(exc))
