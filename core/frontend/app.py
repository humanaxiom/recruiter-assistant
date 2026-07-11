"""Flask frontend — stub.

Phase 0 keeps the service alive with ``/`` and ``/health`` only. The real
read-only viewer (jobs, shortlists, evidence, blind-review redaction) is
Phase 7; it will talk to the FastAPI backend over HTTP.
"""

from __future__ import annotations

from flask import Flask

from src.settings import get_settings

_settings = get_settings()
app = Flask(__name__)
app.secret_key = _settings.flask_secret_key
API = _settings.api_base_url


@app.get("/")
def index() -> str:
    return "recruiter-assistant — viewer arrives in Phase 7."


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
