"""OpenAI-compatible LLM client.

One class — ``LLMClient`` — covers chat, JSON-strict chat, and embeddings
against any OpenAI-compatible endpoint. The same code talks to Ollama in
dev and vLLM in prod; the differences live in env (LLM_BASE_URL).

Reliability features baked in:

* exponential backoff with jitter on 5xx / connection / timeout errors
* circuit breaker (N consecutive failures -> open for cooldown_s)
* JSON-mode: validates against a pydantic schema and retries once with
  the validator error appended, so a model that produced *almost*-valid
  JSON gets one shot to correct itself before we raise

Privacy: prompt content is NEVER logged unless ``debug_llm=True``.
Resume content goes into prompts; treat it as PII.

Ported from hris ``packages/pipeline/src/pipeline/llm/client.py``
(``phase3-source-dossier.md`` §3). Behaviorally verbatim — only the
import paths and the ``structlog`` -> stdlib ``logging`` swap changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Status codes worth retrying. 429 included because Ollama returns it
# when KEEP_ALIVE evicts a model mid-flight.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# Strips a ```json ... ``` (or plain ```) fence the model wraps around
# its output, plus any prose before the first '{' or after the last '}'.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMUnavailableError(RuntimeError):
    """Circuit breaker is open OR all retries exhausted on a transient error."""


class LLMOutputInvalidError(RuntimeError):
    """Model produced non-JSON or schema-invalid JSON after the retry budget."""


class LLMClient:
    """Async OpenAI-compatible client with retries, circuit breaker, JSON mode.

    Use as an async context manager so the underlying httpx client closes:

        async with LLMClient(base_url, gen_model, emb_model) as llm:
            ...
    """

    def __init__(
        self,
        base_url: str,
        model_gen: str,
        model_emb: str,
        *,
        timeout_s: float = 120.0,
        max_retries: int = 2,
        breaker_threshold: int = 10,
        breaker_cooldown_s: float = 30.0,
        debug_llm: bool = False,
        native_chat: bool = False,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        # Ollama's native API lives at the host root, not under /v1.
        self._native_chat = native_chat
        self._native_root = self._base.removesuffix("/v1")
        self._gen = model_gen
        self._emb = model_emb
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._breaker_threshold = breaker_threshold
        self._breaker_cooldown_s = breaker_cooldown_s
        self._debug = debug_llm

        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout_s)

        self._consecutive_failures = 0
        self._opened_at: float | None = None

    # ---------------- public API ----------------

    async def chat(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> str:
        if self._native_chat:
            return await self._chat_native(messages, temperature, max_tokens, json_mode)
        return await self._chat_openai(messages, temperature, max_tokens, json_mode)

    async def _chat_openai(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        body: dict[str, Any] = {
            "model": self._gen,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            # OpenAI JSON mode + disable "thinking". On reasoning models
            # (glm-4.7, gpt-oss, deepseek-r1) the reasoning trace counts
            # against max_tokens; on a large prompt it fills the budget so
            # generation stops before any JSON is emitted and `content`
            # comes back empty. We never use the reasoning text. NOTE:
            # Ollama's OpenAI-compat layer honours `think` only
            # intermittently — for a reliable thinking-off path on Ollama,
            # set llm_ollama_native=True (see _chat_native).
            body["response_format"] = {"type": "json_object"}
            body["think"] = False
        payload = await self._post_json("/chat/completions", body)
        choices = payload.get("choices") or []
        if not choices:
            raise LLMOutputInvalidError("response had no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMOutputInvalidError("response choice had no string content")
        return content

    async def _chat_native(
        self,
        messages: Sequence[dict[str, str]],
        temperature: float,
        max_tokens: int,
        json_mode: bool,
    ) -> str:
        """Ollama's native /api/chat. Unlike the OpenAI-compat endpoint it
        honours ``think: false`` reliably, which is what keeps reasoning
        models from burning the whole token budget on a discarded trace.
        Content may still arrive fenced (```json …```); ``_extract_json``
        downstream strips that."""
        body: dict[str, Any] = {
            "model": self._gen,
            "messages": list(messages),
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            body["format"] = "json"
            body["think"] = False
        payload = await self._post_json(
            "/api/chat", body, url=f"{self._native_root}/api/chat"
        )
        content = (payload.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise LLMOutputInvalidError("native chat response had no string content")
        return content

    async def chat_json(
        self,
        messages: Sequence[dict[str, str]],
        schema: type[T],
        *,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        max_retries: int = 1,
    ) -> T:
        """Call chat, parse JSON, validate against ``schema``.

        On parse/validation failure, retries up to ``max_retries`` extra
        times, each time appending the validator error so the model has
        a chance to self-correct. Raises ``LLMOutputInvalidError`` on
        final failure.
        """
        attempt_messages: list[dict[str, str]] = list(messages)
        last_error: str | None = None

        for attempt in range(max_retries + 1):
            if last_error is not None and attempt > 0:
                attempt_messages = [
                    *attempt_messages,
                    {
                        "role": "user",
                        "content": (
                            "Your previous response failed validation: "
                            f"{last_error}. Return ONLY valid JSON matching "
                            "the schema. No prose, no fences."
                        ),
                    },
                ]
            raw = await self.chat(
                attempt_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=True,
            )
            try:
                obj = json.loads(self._extract_json(raw))
                return schema.model_validate(obj)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)[:500]
                log.warning(
                    "llm.json_invalid",
                    extra={
                        "attempt": attempt,
                        "error": last_error,
                        "prompt_hash": self._prompt_hash(attempt_messages),
                    },
                )

        raise LLMOutputInvalidError(last_error or "no detail")

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        body: dict[str, Any] = {"model": self._emb, "input": list(texts)}
        payload = await self._post_json("/embeddings", body)
        data = payload.get("data") or []
        if len(data) != len(texts):
            raise LLMOutputInvalidError(
                f"expected {len(texts)} embeddings, got {len(data)}"
            )
        out: list[list[float]] = []
        for item in data:
            vec = item.get("embedding")
            if not isinstance(vec, list):
                raise LLMOutputInvalidError("embedding entry missing 'embedding' list")
            out.append([float(x) for x in vec])
        return out

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> LLMClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ---------------- internals ----------------

    async def _post_json(
        self, path: str, body: dict[str, Any], *, url: str | None = None
    ) -> dict[str, Any]:
        self._check_breaker()
        # ``path`` is the log label; ``url`` overrides the target for
        # endpoints that don't live under the OpenAI /v1 base (native Ollama).
        url = url or f"{self._base}{path}"
        prompt_hash = self._prompt_hash(body.get("messages") or body.get("input"))

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            start = time.perf_counter()
            try:
                response = await self._http.post(url, json=body)
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                self._on_failure()
                self._log_attempt(path, prompt_hash, attempt, start, error=str(exc))
                await self._sleep_backoff(attempt)
                continue

            if response.status_code in _RETRY_STATUSES:
                last_exc = httpx.HTTPStatusError(
                    f"{response.status_code}",
                    request=response.request,
                    response=response,
                )
                self._on_failure()
                self._log_attempt(
                    path, prompt_hash, attempt, start, status=response.status_code
                )
                await self._sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                # 4xx (non-429): not retryable, fail fast.
                self._on_failure()
                self._log_attempt(
                    path, prompt_hash, attempt, start, status=response.status_code
                )
                raise LLMUnavailableError(
                    f"{response.status_code} {response.text[:200]}"
                )

            payload: dict[str, Any] = response.json()
            self._on_success()
            usage = payload.get("usage") or {}
            self._log_attempt(
                path,
                prompt_hash,
                attempt,
                start,
                status=response.status_code,
                tokens_in=usage.get("prompt_tokens"),
                tokens_out=usage.get("completion_tokens"),
                ok=True,
            )
            return payload

        # exhausted retries
        raise LLMUnavailableError(
            f"all {self._max_retries + 1} attempts failed: {last_exc!r}"
        )

    def _check_breaker(self) -> None:
        if self._opened_at is None:
            return
        elapsed = time.monotonic() - self._opened_at
        if elapsed < self._breaker_cooldown_s:
            raise LLMUnavailableError(
                f"circuit breaker open ({self._breaker_cooldown_s - elapsed:.1f}s left)"
            )
        # Cooldown elapsed — half-open: allow one trial through. If it
        # fails, _on_failure will re-open immediately.
        self._opened_at = None
        self._consecutive_failures = 0

    def _on_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._breaker_threshold:
            self._opened_at = time.monotonic()
            log.warning(
                "llm.breaker_open",
                extra={
                    "failures": self._consecutive_failures,
                    "cooldown_s": self._breaker_cooldown_s,
                },
            )

    def _on_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    async def _sleep_backoff(self, attempt: int) -> None:
        # 1, 2, 4, 8 ... capped, with ±25% jitter.
        # Non-cryptographic randomness is intentional (jitter spread).
        base = min(8.0, 1.0 * (2**attempt))
        await asyncio.sleep(base * (1.0 + random.uniform(-0.25, 0.25)))  # noqa: S311

    def _log_attempt(
        self,
        path: str,
        prompt_hash: str,
        attempt: int,
        start: float,
        *,
        status: int | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        error: str | None = None,
        ok: bool = False,
    ) -> None:
        # Never log raw prompt content — only the hash, status, latency,
        # and token counts. `debug_llm` stays False by default (see
        # class docstring); this method has no branch that logs prompt
        # bodies even when debug_llm is True, by design.
        log.info(
            "llm.attempt",
            extra={
                "path": path,
                "attempt": attempt,
                "status": status,
                "ok": ok,
                "error": error,
                "prompt_hash": prompt_hash,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
        )

    @staticmethod
    def _prompt_hash(payload: object) -> str:
        try:
            blob = json.dumps(payload, sort_keys=True, default=str)
        except TypeError:
            blob = repr(payload)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @staticmethod
    def _extract_json(raw: str) -> str:
        """Best-effort: strip ```json fences and leading/trailing prose.

        Returns the substring most likely to be JSON. Caller still has to
        json.loads it; this just removes the common envelope noise.
        """
        s = raw.strip()
        fence = _FENCE_RE.search(s)
        if fence is not None:
            return fence.group(1).strip()
        # Look for the first '{' / '[' and the matching close, naïvely.
        starts = [s.find("{"), s.find("[")]
        starts = [i for i in starts if i != -1]
        if not starts:
            return s
        start = min(starts)
        ends = [s.rfind("}"), s.rfind("]")]
        end = max(ends)
        if end <= start:
            return s
        return s[start : end + 1]
