"""sfu-telemetry pilot (ADR-048) — every model call is a GenAI span.

One CLIENT span per logical call (retries included), carrying the model,
token counts, attempt count and prompt HASH — never a prompt or response
body, and never an exception message (a validator's message can embed the
model's output, i.e. candidate data). The existing ``llm.attempt`` log line
also gains the model name, which was the gap the telemetry survey found.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from src.pipeline.llm.client import LLMClient, LLMUnavailableError
from tests.unit.telemetry_support import SPANS

BASE_URL = "http://aria-gb10:11434/v1"


def _client(handler: Any) -> LLMClient:
    c = LLMClient(
        BASE_URL, "gpt-oss:20b", "nomic-embed-text", max_retries=1, expected_dim=3
    )
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _chat_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "gpt-oss:20b",
            "choices": [{"message": {"content": "Jane Doe is a strong match"}}],
            "usage": {"prompt_tokens": 1432, "completion_tokens": 211},
        },
    )


@pytest.fixture(autouse=True)
def _clear() -> None:
    SPANS.clear()


@pytest.mark.asyncio
async def test_chat_emits_one_genai_span_with_usage() -> None:
    client = _client(_chat_ok)
    await client.chat([{"role": "user", "content": "résumé of Jane Doe"}])
    (span,) = SPANS.get_finished_spans()
    a = dict(span.attributes or {})
    assert span.name == "chat gpt-oss:20b"
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.request.model"] == "gpt-oss:20b"
    assert a["gen_ai.provider.name"] == "ollama"
    assert a["server.address"] == "aria-gb10"
    assert a["gen_ai.usage.input_tokens"] == 1432
    assert a["gen_ai.usage.output_tokens"] == 211
    assert a["sfu.llm.attempts"] == 1
    assert len(str(a["sfu.prompt.hash"])) == 16
    assert "Jane" not in json.dumps(a, default=str)


@pytest.mark.asyncio
async def test_embed_emits_an_embeddings_span() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    await _client(handler).embed(["python"])
    (span,) = SPANS.get_finished_spans()
    assert span.name == "embeddings nomic-embed-text"
    assert (span.attributes or {})["gen_ai.operation.name"] == "embeddings"


@pytest.mark.asyncio
async def test_retries_are_counted_and_exhaustion_marks_error_by_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(self: LLMClient, attempt: int) -> None:
        return None

    monkeypatch.setattr(LLMClient, "_sleep_backoff", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model Jane Doe overloaded")

    with pytest.raises(LLMUnavailableError):
        await _client(handler).chat([{"role": "user", "content": "x"}])
    (span,) = SPANS.get_finished_spans()
    a = dict(span.attributes or {})
    assert a["sfu.llm.attempts"] == 2
    assert a["error.type"] == "LLMUnavailableError"
    assert "Jane" not in json.dumps(a, default=str) + (span.status.description or "")


@pytest.mark.asyncio
async def test_llm_attempt_log_carries_the_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="src.pipeline.llm.client")
    await _client(_chat_ok).chat([{"role": "user", "content": "x"}])
    (rec,) = [r for r in caplog.records if r.getMessage() == "llm.attempt"]
    assert rec.__dict__["model"] == "gpt-oss:20b"
