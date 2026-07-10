"""CoderAgent — implements and iterates using Ollama (local, offline).

Before implementing, it queries Neo4j vector memory for similar prior
artifacts and injects them as context ("have we solved this before?").
"""

from __future__ import annotations

import logging
import os

from openai import AsyncOpenAI

from src.memory.graph import GraphMemory
from src.settings import get_settings

logger = logging.getLogger(__name__)

CODER_SYSTEM = """You are the CoderAgent in an offline TDD harness.
Rules:
- Tests already exist and are FAILING. Your job is to make them pass.
- Never modify test files unless the failure report proves a test is malformed.
- Full type annotations; mypy --strict must pass.
- ruff and black clean.
- When given a GATE FAILURE report, fix ONLY what the report indicates.
Output unified diffs or complete file contents as instructed by the harness runner.
"""


class CoderAgent:
    def __init__(self, memory: GraphMemory | None = None) -> None:
        s = get_settings()
        self._client = AsyncOpenAI(base_url=s.ollama_base_url, api_key="ollama")
        self._model = s.agent_model
        self._memory = memory

    async def iterate(self, task: str, failure_report: str, iteration: int) -> str:
        """One review-loop iteration: read failures, apply fixes."""
        context = await self._retrieve_context(task) if self._memory else ""

        messages = [
            {"role": "system", "content": CODER_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Task: {task}\n\n"
                    f"Iteration: {iteration}\n\n"
                    f"{context}"
                    f"Gate failure report:\n{failure_report}\n\n"
                    "Fix the failures. Respond with the corrected files."
                ),
            },
        ]

        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            temperature=0.1,
        )
        content = response.choices[0].message.content or ""
        applied = self._apply_changes(content)
        return f"Applied {applied} file change(s) for iteration {iteration}"

    async def _retrieve_context(self, task: str) -> str:
        assert self._memory is not None
        similar = await self._memory.similar_artifacts(task, k=3)
        if not similar:
            return ""
        blocks = "\n---\n".join(
            f"[{a['kind']}] (score {a['score']:.2f})\n{a['content']}" for a in similar
        )
        return f"Similar prior work from memory:\n{blocks}\n\n"

    def _apply_changes(self, model_output: str) -> int:
        """Parse fenced file blocks (```path=src/x.py) and write them.

        Format expected from the model:
            ```python path=src/foo.py
            <full file content>
            ```
        """
        import re

        pattern = re.compile(
            r"```[a-z]*\s+path=([^\s`]+)\n(.*?)```", re.DOTALL
        )
        count = 0
        for match in pattern.finditer(model_output):
            path, content = match.group(1), match.group(2)
            if path.startswith(("tests/", "src/")) and ".." not in path:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                count += 1
                logger.info("Wrote %s", path)
            else:
                logger.warning("Refused to write outside src/tests: %s", path)
        return count
