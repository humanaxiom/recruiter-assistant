"""The one place this product configures telemetry (ADR-048).

Every process — API, worker, frontend — calls :func:`setup_telemetry` once
from its production entrypoint. Unit tests import the app modules without
calling it, so process-wide logging is never reconfigured under pytest.

Two values are fixed here rather than read from settings, on purpose:

* ``data_class_max="S"`` — this product holds candidate PII.
* ``content_level="minimal"`` — no prompt, résumé, response body or exception
  message ever enters a span or a log attribute. The library refuses content
  capture for class ``S`` anyway; not exposing the knob makes that visible.
"""

from __future__ import annotations

from sfu_telemetry import TelemetryConfig, init

from src.settings import Settings, get_settings

SERVICE_NAMESPACE = "humanaxiom"
PRODUCT = "recruiter"


def telemetry_config(settings: Settings, service: str) -> TelemetryConfig:
    return TelemetryConfig(
        service_name=f"{PRODUCT}-{service}",
        service_namespace=SERVICE_NAMESPACE,
        service_version=settings.telemetry_service_version
        or settings.git_sha
        or "unknown",
        product=PRODUCT,
        environment=settings.telemetry_environment,
        zone=settings.telemetry_zone,
        data_class_max="S",
        content_level="minimal",
        export=settings.telemetry_export,
        otlp_endpoint=settings.telemetry_otlp_endpoint,
        log_format=settings.log_format,
        log_level=settings.log_level,
    )


def setup_telemetry(service: str) -> None:
    """Configure JSON logging, tracing and metrics for this process (idempotent)."""
    init(telemetry_config(get_settings(), service))
