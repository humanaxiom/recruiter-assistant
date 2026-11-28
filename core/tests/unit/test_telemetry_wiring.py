"""sfu-telemetry pilot (ADR-048) — wiring of the shared telemetry library.

What these pin:

* Telemetry EXPORT is off unless an operator configures it, so a fresh
  checkout, a CI run and an airgapped deploy never open a telemetry socket.
* The data class is ``S`` and content capture is ``minimal`` for every
  process, with no setting that can raise it: candidate data never enters
  telemetry, whatever ``.env`` says.
* ``init()`` happens only in the production entrypoints
  (``telemetry_lifespan``, ``startup_with_telemetry``, ``frontend.wsgi``), so
  importing the app or calling ``lifespan``/``startup`` in a unit test keeps
  process-wide logging untouched.
* Trace context crosses every hop: browser -> Flask BFF (httpx hook) -> API
  (ASGI middleware) -> arq queue (Redis carrier) -> worker (job hooks).
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from src.settings import Settings


def test_telemetry_export_defaults_to_off() -> None:
    s = Settings()
    assert s.telemetry_export == "off"
    assert s.log_format == "json"


def test_telemetry_config_is_sensitive_minimal_and_named_per_process() -> None:
    from src.telemetry import telemetry_config

    cfg = telemetry_config(
        Settings(
            telemetry_export="otlp",
            telemetry_otlp_endpoint="http://otel-collector:4318",
            telemetry_environment="pilot",
            telemetry_zone="hr-enclave",
            telemetry_service_version="abc1234",
        ),
        "worker",
    )
    assert cfg.service_name == "recruiter-worker"
    assert cfg.service_namespace == "humanaxiom"
    assert cfg.product == "recruiter"
    assert cfg.data_class_max == "S"
    assert cfg.content_level == "minimal"
    assert not cfg.captures_content
    assert (cfg.export, cfg.otlp_endpoint) == ("otlp", "http://otel-collector:4318")
    assert (cfg.environment, cfg.zone, cfg.service_version) == (
        "pilot",
        "hr-enclave",
        "abc1234",
    )


def test_no_setting_can_turn_on_content_capture() -> None:
    names = set(Settings.model_fields)
    assert not {n for n in names if "content" in n and "telemetry" in n}
    assert not {n for n in names if "data_class" in n}


def test_export_modes_are_exactly_the_library_modes() -> None:
    from sfu_telemetry.config import ExportMode

    annotation = Settings.model_fields["telemetry_export"].annotation
    assert set(get_args(annotation)) == set(get_args(ExportMode))


def test_api_app_carries_the_telemetry_middleware() -> None:
    from sfu_telemetry.asgi import TelemetryMiddleware

    from src.api.main import app

    assert any(m.cls is TelemetryMiddleware for m in app.user_middleware)


@pytest.mark.asyncio
async def test_telemetry_lifespan_initialises_then_delegates() -> None:
    from src.api import main

    calls: list[str] = []

    @asynccontextmanager
    async def fake_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        calls.append("lifespan")
        yield

    with (
        patch.object(main, "setup_telemetry", lambda svc: calls.append(f"init:{svc}")),
        patch.object(main, "lifespan", fake_lifespan),
    ):
        async with main.telemetry_lifespan(FastAPI()):
            pass
    assert calls == ["init:api", "lifespan"]


@pytest.mark.asyncio
async def test_api_lifespan_instruments_its_arq_pool(tmp_path: Any) -> None:
    from src.api.main import lifespan

    pool = MagicMock()
    pool.enqueue_job = AsyncMock()
    pool.close = AsyncMock()
    driver = MagicMock()
    driver.close = AsyncMock()
    fresh = FastAPI()
    settings = Settings(storage_dir=str(tmp_path), skill_hash_salt="a-real-salt")
    with (
        patch("src.api.main.get_settings", return_value=settings),
        patch("src.api.main.init_pool", AsyncMock()),
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.close_pool", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=driver),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=pool)),
    ):
        async with lifespan(fresh):
            assert getattr(fresh.state.arq.enqueue_job, "_sfu_instrumented", False) is True


def test_worker_uses_telemetry_startup_and_job_hooks() -> None:
    from src.worker.main import WorkerSettings, startup_with_telemetry

    assert WorkerSettings.on_startup is startup_with_telemetry
    assert callable(getattr(WorkerSettings, "on_job_start", None))
    assert callable(getattr(WorkerSettings, "on_job_end", None))


@pytest.mark.asyncio
async def test_startup_with_telemetry_initialises_then_runs_startup() -> None:
    from src.worker import main

    calls: list[str] = []

    async def fake_startup(ctx: dict[str, Any]) -> None:
        calls.append("startup")

    with (
        patch.object(main, "setup_telemetry", lambda svc: calls.append(f"init:{svc}")),
        patch.object(main, "startup", fake_startup),
    ):
        await main.startup_with_telemetry({})
    assert calls == ["init:worker", "startup"]


@pytest.mark.asyncio
async def test_worker_startup_instruments_the_enqueue_pool(tmp_path: Any) -> None:
    from src.worker.main import startup

    pool = MagicMock()
    pool.enqueue_job = AsyncMock()
    driver = MagicMock()
    ctx: dict[str, Any] = {}
    settings = Settings(
        storage_dir=str(tmp_path),
        pii_key="a-real-secret-key",
        skill_hash_salt="a-real-salt",
    )
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch("src.worker.main.asyncpg.create_pool", AsyncMock(return_value=MagicMock())),
        patch("src.worker.main.init_schema", AsyncMock()),
        patch("src.worker.main.AsyncGraphDatabase.driver", return_value=driver),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.worker.main.create_pool", AsyncMock(return_value=pool)),
    ):
        await startup(ctx)
    assert getattr(ctx["arq"].enqueue_job, "_sfu_instrumented", False) is True


def test_frontend_app_is_instrumented() -> None:
    from frontend.app import app

    hooks = [f.__name__ for f in app.before_request_funcs.get(None, [])]
    assert "_start" in hooks
    assert any(f.__name__ == "_end" for f in app.teardown_request_funcs.get(None, []))


def test_frontend_client_propagates_trace_context() -> None:
    from sfu_telemetry.httpx_hooks import inject_trace_headers

    from frontend.api_client import build_client

    client = build_client()
    try:
        assert inject_trace_headers in client.event_hooks["request"]
    finally:
        client.close()


def test_frontend_wsgi_entrypoint_initialises_telemetry() -> None:
    calls: list[str] = []
    sys.modules.pop("frontend.wsgi", None)
    with patch("src.telemetry.setup_telemetry", lambda svc: calls.append(svc)):
        wsgi = importlib.import_module("frontend.wsgi")
    from frontend.app import app

    assert wsgi.app is app
    assert calls == ["frontend"]
    sys.modules.pop("frontend.wsgi", None)


def test_compose_runs_the_frontend_through_the_wsgi_entrypoint() -> None:
    from pathlib import Path

    compose = (Path(__file__).parents[3] / "docker-compose.yml").read_text()
    assert "flask --app frontend.wsgi run" in compose
