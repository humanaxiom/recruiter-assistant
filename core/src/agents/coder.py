"""CoderAgent — implements and iterates using Ollama (local, offline).

Before implementing, it queries Neo4j vector memory for similar prior
artifacts and injects them as context ("have we solved this before?").
It extends :class:`BaseAgent` so it shares the same client, memory, and
lineage-recording contract as every other subagent — produced files are
written to the working tree *and* recorded to graph memory.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from src.agents.base import AgentInput, AgentOutput, BaseAgent
from src.agents.parsing import extract_file_blocks
from src.memory.graph import GraphMemory

logger = logging.getLogger(__name__)

CODER_SYSTEM = """You are the CoderAgent in an offline TDD harness.
Rules:
- Tests already exist and are FAILING. Your job is to make them pass.
- Never modify test files unless the failure report proves a test is malformed.
- Full type annotations; mypy --strict must pass.
- ruff and black clean.
- When given a GATE FAILURE report, fix ONLY what the report indicates.
Output every changed file as a fenced block:
```python path=src/foo.py
<full file content>
```"""

WriterFn = Callable[[str, str], None]


def _default_writer(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class CoderAgent(BaseAgent):
    agent_id = "coder"
    temperature = 0.1
    system_prompt = CODER_SYSTEM

    def __init__(
        self, memory: GraphMemory | None = None, writer: WriterFn | None = None
    ) -> None:
        super().__init__(memory=memory)
        self._writer = writer or _default_writer

    async def run(self, input_: AgentInput) -> AgentOutput:
        """Standard one-shot interface (ABC contract).

        The orchestrator normally drives the coder through :meth:`iterate`
        inside the ReviewLoop; this satisfies BaseAgent and is a useful
        single-pass entry point.
        """
        summary, artifacts, tokens = await self._implement(input_.task, "", 1)
        output = self._ok(
            input_,
            result={"files": list(artifacts)},
            reasoning=summary,
            artifacts=artifacts,
            tokens=tokens,
        )
        await self._record(input_, output)
        return output

    async def iterate(
        self, task_id: str, task: str, failure_report: str, iteration: int
    ) -> str:
        """One review-loop iteration: read failures, apply fixes, record."""
        summary, artifacts, tokens = await self._implement(
            task, failure_report, iteration
        )
        if artifacts:
            input_ = AgentInput(task_id=task_id, task=task)
            output = self._ok(input_, None, summary, artifacts, tokens)
            await self._record(input_, output)
        return summary

    async def _implement(
        self, task: str, failure_report: str, iteration: int
    ) -> tuple[str, dict[str, str], int]:
        context = await self._memory_context(task)
        prompt = f"Task: {task}\n\n" f"Iteration: {iteration}\n\n" f"{context}"
        if failure_report:
            prompt += (
                f"Gate failure report:\n{failure_report}\n\n"
                "Fix the failures. Respond with the corrected files."
            )
        else:
            prompt += "Implement the task. Respond with the complete files."

        text, tokens = await self._complete(prompt)
        artifacts = self._extract_src_files(text)
        applied = self._apply_changes(artifacts)
        return (
            f"Applied {applied} file change(s) for iteration {iteration}",
            artifacts,
            tokens,
        )

    def _extract_src_files(self, text: str) -> dict[str, str]:
        """Accept only src/ and tests/ paths, never outside the project tree."""
        files: dict[str, str] = {}
        for path, content in extract_file_blocks(text).items():
            if path.startswith(("src/", "tests/")) and ".." not in path:
                files[path] = content
            else:
                logger.warning("Refused to write outside src/tests: %s", path)
        return files

    def _apply_changes(self, artifacts: dict[str, str]) -> int:
        for path, content in artifacts.items():
            self._writer(path, content)
            logger.info("Wrote %s", path)
        return len(artifacts)
