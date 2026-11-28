"""Shared in-memory OpenTelemetry provider for the telemetry unit tests.

OpenTelemetry's global tracer provider can be set once per process, so the
first test module that imports this installs an SDK provider with an
in-memory exporter; every later import reuses it. Production code never
imports this — it gets its provider from ``src.telemetry.setup_telemetry``.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

SPANS = InMemorySpanExporter()

if not isinstance(trace.get_tracer_provider(), TracerProvider):
    _provider = TracerProvider()
    _provider.add_span_processor(SimpleSpanProcessor(SPANS))
    trace.set_tracer_provider(_provider)
