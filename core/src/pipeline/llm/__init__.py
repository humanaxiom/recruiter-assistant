"""LLM client + embedding cache — hand-rolled httpx OpenAI-compatible client.

Ported from hris ``packages/pipeline/src/pipeline/llm`` (see
``phase3-source-dossier.md`` §3-4). Deliberately NOT built on the
``openai`` SDK — that package has been removed from requirements.txt.
"""

from __future__ import annotations

from src.pipeline.llm.cache import CachedEmbedder
from src.pipeline.llm.client import (
    LLMClient,
    LLMOutputInvalidError,
    LLMUnavailableError,
)

__all__ = [
    "CachedEmbedder",
    "LLMClient",
    "LLMOutputInvalidError",
    "LLMUnavailableError",
]
