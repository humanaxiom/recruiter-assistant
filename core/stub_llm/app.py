"""FastAPI server exposing an OpenAI-compatible surface for the offline LLM
stub. Run with:

    uvicorn stub_llm.app:app --host 0.0.0.0 --port 8000

Both ``/v1/chat/completions`` + ``/v1/embeddings`` AND the same routes
without the ``/v1`` prefix are mounted, so either shape of ``LLM_BASE_URL``
(``http://llmstub:8000/v1`` or ``http://llmstub:8000``) works against it
without the app needing to know which one is configured. No outbound network
call is ever made — every response is generated in-process by
``stub_llm.responses``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import FastAPI, Request

from stub_llm.responses import embed, respond

app = FastAPI(title="stub-llm", description="Offline OpenAI-compatible fake model")


async def _chat_completions(request: Request) -> dict[str, Any]:
    body = await request.json()
    messages = body.get("messages") or []
    system_prompt = ""
    user_prompt = ""
    if messages:
        first = messages[0]
        if isinstance(first, dict) and first.get("role") == "system":
            system_prompt = first.get("content") or ""
        last = messages[-1]
        if isinstance(last, dict):
            user_prompt = last.get("content") or ""
    result = respond(system_prompt, user_prompt)
    content = json.dumps(result)
    return {
        "id": "stub-chatcmpl",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "stub-model"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


async def _embeddings(request: Request) -> dict[str, Any]:
    body = await request.json()
    raw_input = body.get("input") or []
    texts: list[str] = raw_input if isinstance(raw_input, list) else [raw_input]
    vectors = embed(texts)
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "embedding": vec, "index": i}
            for i, vec in enumerate(vectors)
        ],
        "model": body.get("model", "stub-embed-model"),
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


for _prefix in ("", "/v1"):
    app.add_api_route(
        f"{_prefix}/chat/completions", _chat_completions, methods=["POST"]
    )
    app.add_api_route(f"{_prefix}/embeddings", _embeddings, methods=["POST"])


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
