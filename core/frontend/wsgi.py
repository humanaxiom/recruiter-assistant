"""Production entrypoint for the Flask BFF (ADR-048).

``flask --app frontend.wsgi run`` (docker-compose.yml) imports this: it is
``frontend.app`` plus process-wide telemetry setup (JSON logs with trace ids;
span/metric export only when ``TELEMETRY_EXPORT`` is set). Tests import
``frontend.app`` directly, so importing the app never reconfigures logging.
"""

from __future__ import annotations

from frontend.app import app
from src import telemetry

telemetry.setup_telemetry("frontend")

__all__ = ["app"]
