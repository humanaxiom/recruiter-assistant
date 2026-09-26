"""Offline, OpenAI-compatible fake model the stress/e2e stack points
``LLM_BASE_URL`` at instead of the real tailnet Ollama peer. No outbound
network calls, ever — see ``responses.py`` (canned, schema-valid output) and
``app.py`` (the FastAPI server exposing the OpenAI-compatible routes).
"""

from __future__ import annotations
