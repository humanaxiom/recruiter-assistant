"""sfu-telemetry pilot (ADR-048) — fixes from the review and security audit.

* The browser-facing BFF is a public edge: it must NOT continue a client's
  ``traceparent`` (a client could otherwise pick the trace id written next to
  its audit rows). The API trusts the BFF's header: its callers are the BFF
  and key-holding services.
* The worker's job hooks read the carrier from ``ctx["arq"]`` (not the
  embedding cache's ``ctx["redis"]``) and give ``project_to_graph``'s 5-second
  cron no root trace.
* An open circuit breaker is still a traced, failed model call.
* ``compose.telemetry.yml`` opts the app containers' logs in and does NOT put
  the PII-bearing Postgres (or any datastore) on the shared telemetry network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from opentelemetry import propagate, trace

from src.pipeline.llm.client import LLMClient, LLMUnavailableError
from tests.unit.telemetry_support import SPANS

REPO = Path(__file__).parents[3]
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@pytest.fixture(autouse=True)
def _clear() -> None:
    SPANS.clear()


def test_api_trusts_incoming_trace_context_from_the_bff() -> None:
    from sfu_telemetry.asgi import TelemetryMiddleware

    from src.api.main import app

    (mw,) = [m for m in app.user_middleware if m.cls is TelemetryMiddleware]
    assert mw.kwargs.get("trust_incoming_context") is True


def test_frontend_ignores_a_clients_traceparent() -> None:
    from frontend.app import app

    resp = app.test_client().get(
        "/static/app.css", headers={"traceparent": TRACEPARENT}
    )
    assert resp.status_code == 200
    (span,) = SPANS.get_finished_spans()
    assert span.parent is None
    assert format(span.context.trace_id, "032x") != TRACEPARENT.split("-")[1]


class _FakeRedis:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    async def get(self, name: str) -> str | None:
        return self.store.get(name)


def _carrier_for(job_id: str) -> dict[str, str]:
    carrier: dict[str, str] = {}
    with trace.get_tracer("t").start_as_current_span("POST /jobs"):
        propagate.inject(carrier)
    return {
        f"sfu:traceparent:{job_id}": json.dumps({"carrier": carrier, "fn": "parse_job"})
    }


@pytest.mark.asyncio
async def test_job_hooks_read_the_carrier_from_ctx_arq() -> None:
    from src.worker.main import WorkerSettings

    store = _carrier_for("j1")
    parent_trace = json.loads(next(iter(store.values())))["carrier"]["traceparent"]
    SPANS.clear()
    ctx: dict[str, Any] = {"job_id": "j1", "job_try": 1, "arq": _FakeRedis(store)}
    await WorkerSettings.on_job_start(ctx)
    await WorkerSettings.on_job_end(ctx)
    (span,) = SPANS.get_finished_spans()
    assert span.name == "arq.job parse_job"
    assert format(span.context.trace_id, "032x") == parent_trace.split("-")[1]


@pytest.mark.asyncio
async def test_job_hooks_ignore_the_embedding_cache_client() -> None:
    from src.worker.main import WorkerSettings

    ctx: dict[str, Any] = {
        "job_id": "j2",
        "job_try": 1,
        "redis": _FakeRedis(_carrier_for("j2")),
    }
    SPANS.clear()
    await WorkerSettings.on_job_start(ctx)
    await WorkerSettings.on_job_end(ctx)
    (span,) = SPANS.get_finished_spans()
    assert span.parent is None and span.name == "arq.job unknown"


@pytest.mark.asyncio
async def test_project_to_graph_cron_gets_no_root_trace() -> None:
    from src.worker.main import WorkerSettings

    for job_id in (
        "cron:project_to_graph:1700000000000",
        "cron:reconcile_stalled_parses:1",
    ):
        ctx: dict[str, Any] = {"job_id": job_id, "job_try": 1}
        await WorkerSettings.on_job_start(ctx)
        await WorkerSettings.on_job_end(ctx)
    assert [s.name for s in SPANS.get_finished_spans()] == [
        "arq.job reconcile_stalled_parses"
    ]


@pytest.mark.asyncio
async def test_an_open_breaker_is_a_traced_failed_call() -> None:
    client = LLMClient("http://aria-gb10:11434/v1", "gpt-oss:20b", "nomic-embed-text")
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    )
    client._opened_at = time.monotonic()
    with pytest.raises(LLMUnavailableError):
        await client.chat([{"role": "user", "content": "x"}])
    (span,) = SPANS.get_finished_spans()
    a = dict(span.attributes or {})
    assert a["error.type"] == "LLMUnavailableError"
    assert "sfu.llm.attempts" not in a  # refused before any HTTP attempt


def _overlay() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((REPO / "compose.telemetry.yml").read_text())
    return data


@pytest.mark.parametrize("name", ["api", "worker", "frontend"])
def test_overlay_opts_app_container_logs_in(name: str) -> None:
    svc = _overlay()["services"][name]
    assert svc["labels"]["sfu.telemetry"] == "opt-in"
    assert "sfu.telemetry" in svc["logging"]["options"]["labels"].split(",")


def test_overlay_never_puts_a_datastore_on_the_telemetry_network() -> None:
    services = _overlay()["services"]
    for name in ("postgres", "neo4j", "redis"):
        assert name not in services, f"{name} must not appear in compose.telemetry.yml"
