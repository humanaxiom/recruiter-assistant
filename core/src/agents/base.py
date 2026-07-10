"""BaseAgent — shared contract for all subagents.

Every subagent (Planner, Tester, Coder, Reviewer, Docs, Security) extends
this. Structured I/O, Ollama client, memory access, scratchpad logging.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

from src.memory.graph import GraphMemory
from src.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class AgentInput:
    task_id: str
    task: str
    context: dict[str, Any] = field(default_factory=dict)
    prior_outputs: dict[str, "AgentOutput"] = field(default_factory=dict)


@dataclass
class AgentOutput:
    task_id: str
    agent_id: str
    success: bool
    result: Any
    reasoning: str
    artifacts: dict[str, str] = field(default_factory=dict)  # path -> content
    tokens_used: int = 0
    error: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class BaseAgent(ABC):
    """All subagents extend this class."""

    agent_id: str = "base"
    system_prompt: str = ""
    temperature: float = 0.2

    def __init__(self, memory: GraphMemory | None = None) -> None:
        s = get_settings()
        self._client = AsyncOpenAI(base_url=s.ollama_base_url, api_key="ollama")
        self._model = s.agent_model
        self._memory = memory
        self._scratchpad: list[str] = []

    @abstractmethod
    async def run(self, input_: AgentInput) -> AgentOutput:
        ...

    # ── Shared machinery ───────────────────────────────────────────────────

    async def _complete(self, user_content: str) -> tuple[str, int]:
        """One completion against local Ollama; returns (text, tokens)."""
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user_content},
            ],  # type: ignore[arg-type]
            temperature=self.temperature,
        )
        text = response.choices[0].message.content or ""
        tokens = response.usage.total_tokens if response.usage else 0
        return text, tokens

    async def _memory_context(self, query: str, k: int = 3) -> str:
        """Retrieve similar prior artifacts from Neo4j vector memory."""
        if self._memory is None:
            return ""
        similar = await self._memory.similar_artifacts(query, k=k)
        if not similar:
            return ""
        blocks = "\n---\n".join(
            f"[{a['kind']}] (score {a['score']:.2f})\n{a['content']}"
            for a in similar
        )
        return f"## Similar prior work (graph memory)\n{blocks}\n\n"

    async def _record(self, input_: AgentInput, output: AgentOutput) -> None:
        """Persist lineage + artifacts to graph memory."""
        if self._memory is None:
            return
        subtask_id = f"{input_.task_id}-{self.agent_id}"
        await self._memory.record_subtask(
            task_id=input_.task_id,
            subtask_id=subtask_id,
            agent_id=self.agent_id,
            description=input_.task[:500],
        )
        for path, content in output.artifacts.items():
            await self._memory.record_artifact(
                subtask_id=subtask_id,
                artifact_id=f"{subtask_id}:{path}",
                content=content,
                kind=self.agent_id,
            )

    def _log(self, message: str) -> None:
        entry = (
            f"[{datetime.now(timezone.utc).isoformat()}] "
            f"[{self.agent_id}] {message}"
        )
        self._scratchpad.append(entry)
        logger.info(entry)

    def _ok(
        self,
        input_: AgentInput,
        result: Any,
        reasoning: str,
        artifacts: dict[str, str] | None = None,
        tokens: int = 0,
    ) -> AgentOutput:
        return AgentOutput(
            task_id=input_.task_id,
            agent_id=self.agent_id,
            success=True,
            result=result,
            reasoning=reasoning,
            artifacts=artifacts or {},
            tokens_used=tokens,
        )

    def _fail(self, input_: AgentInput, error: str) -> AgentOutput:
        return AgentOutput(
            task_id=input_.task_id,
            agent_id=self.agent_id,
            success=False,
            result=None,
            reasoning="",
            error=error,
        )
