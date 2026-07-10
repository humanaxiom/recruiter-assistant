"""TesterAgent — writes FAILING tests first (the Red step)."""

from __future__ import annotations

import re

from src.agents.base import AgentInput, AgentOutput, BaseAgent

FILE_BLOCK = re.compile(r"```[a-z]*\s+path=([^\s`]+)\n(.*?)```", re.DOTALL)


class TesterAgent(BaseAgent):
    agent_id = "tester"
    temperature = 0.2
    system_prompt = """You are the TesterAgent in an offline TDD harness.
Write FAILING pytest tests for the given spec. The implementation does not exist yet.

RULES:
- pytest + pytest-asyncio; @pytest.mark.asyncio for async tests
- Cover: happy path, edge cases, error cases; parametrize where natural
- Mock ALL external I/O (Ollama, Postgres, Neo4j, Redis) in unit tests
- Full type annotations; tests must satisfy ruff/black/mypy --strict
- Tests go in tests/unit/test_<module>.py (or tests/integration/ if the spec demands real stores)
- NEVER write implementation code — only tests
- Aim for >= 5 tests per public class/function

Output every file as:
```python path=tests/unit/test_<name>.py
<complete file content>
```"""

    async def run(self, input_: AgentInput) -> AgentOutput:
        self._log(f"Writing failing tests for: {input_.task[:80]}")
        memory_ctx = await self._memory_context(input_.task)

        try:
            text, tokens = await self._complete(
                f"{memory_ctx}Spec:\n{input_.task}\n\n"
                "Write the failing tests now."
            )
            artifacts = self._extract_test_files(text)
            if not artifacts:
                return self._fail(input_, "TesterAgent produced no test files")

            output = self._ok(
                input_,
                result=list(artifacts.keys()),
                reasoning=f"Wrote {len(artifacts)} failing test file(s)",
                artifacts=artifacts,
                tokens=tokens,
            )
            await self._record(input_, output)
            return output
        except Exception as exc:  # noqa: BLE001
            return self._fail(input_, str(exc))

    def _extract_test_files(self, text: str) -> dict[str, str]:
        """Only accept files under tests/ — a tester never writes src/."""
        files: dict[str, str] = {}
        for match in FILE_BLOCK.finditer(text):
            path, content = match.group(1), match.group(2)
            if path.startswith("tests/") and ".." not in path:
                files[path] = content
            else:
                self._log(f"Rejected non-test path from tester: {path}")
        return files
