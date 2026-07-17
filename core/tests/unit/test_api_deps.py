"""Unit tests for Phase 6's cross-cutting API dependencies —
``src.api.deps`` (new module; does not exist yet). Every test below fails at
collection (``ModuleNotFoundError``). RED half of the TDD cycle.

**Ambiguities this file locks (decision 1, the "configurable auth switch"):**

* ONE settings field is the switch: ``Settings.api_key: str = ""``. Empty
  string = auth DISABLED (local dev, fail-open by explicit configuration,
  never by omission-in-code); non-empty = auth ENABLED (fail-closed).
* ``require_api_key(x_api_key: str | None = Header(alias="X-API-Key")) ->
  None`` — a FastAPI dependency:
  - enabled + ``x_api_key == settings.api_key`` -> returns ``None``
    (passes).
  - enabled + missing/wrong key -> raises ``fastapi.HTTPException`` with
    ``status_code == 401`` (fail-CLOSED).
  - disabled (``settings.api_key == ""``) -> ALWAYS passes, regardless of
    what ``x_api_key`` carries (bypassed, per the locked decision).
* ``resolve_actor(x_actor_name: str | None = Header(alias="X-Actor-Name")) ->
  str`` — returns ``x_actor_name`` verbatim when present, else the fixed
  default label ``"api"`` (populates the nullable ``created_by``/
  ``uploaded_by`` TEXT columns).
* ``get_arq(request: Request) -> ArqRedis`` mirrors ``get_db``/
  ``get_blob_store``'s "not initialised" contract exactly: raises
  ``RuntimeError`` when the lifespan never parked a pool on
  ``app.state.arq``.
* ``log_auth_mode(settings: Settings) -> None`` is called ONCE at API
  startup (the lifespan, mirroring the existing ``SKILL_HASH_SALT``/
  ``PII_KEY`` loud-startup-log discipline in ``src.api.main``/
  ``src.worker.main``) and logs a LOUD, unmistakable warning
  (``logging.WARNING``, message mentions "AUTH" and "DISABLED") when
  ``settings.api_key`` is empty; an informational (non-warning) log line
  otherwise. This is a plain log-emitting function so it is testable in
  isolation from the full lifespan.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException

from src.settings import Settings


def _settings(api_key: str = "") -> Settings:
    return Settings(api_key=api_key, skill_hash_salt="test-salt", pii_key="test-key")


# ── require_api_key: enabled ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_require_api_key_passes_with_the_correct_key(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings(api_key="secret123"))
    result = await deps.require_api_key(x_api_key="secret123")
    assert result is None


@pytest.mark.asyncio
async def test_require_api_key_rejects_a_wrong_key(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings(api_key="secret123"))
    with pytest.raises(HTTPException) as excinfo:
        await deps.require_api_key(x_api_key="wrong")
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_require_api_key_rejects_a_missing_key(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings(api_key="secret123"))
    with pytest.raises(HTTPException) as excinfo:
        await deps.require_api_key(x_api_key=None)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_require_api_key_rejects_a_non_ascii_key_with_401_not_500(
    monkeypatch: Any,
) -> None:
    """Security re-audit LOW: Starlette latin-1-decodes header bytes, so a
    non-ASCII ``X-API-Key`` header is a valid ``str`` containing non-ASCII
    code points. ``secrets.compare_digest`` on two ``str`` requires both to be
    ASCII-only or it raises ``TypeError`` — which previously propagated as an
    unhandled 500 instead of the intended fail-closed 401."""
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings(api_key="secret123"))
    with pytest.raises(HTTPException) as excinfo:
        await deps.require_api_key(x_api_key="clé-secrète-café")
    assert excinfo.value.status_code == 401


# ── require_api_key: disabled (empty key) ───────────────────────────────


@pytest.mark.asyncio
async def test_require_api_key_bypasses_when_disabled(monkeypatch: Any) -> None:
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings(api_key=""))
    # No key at all still passes when the switch is off.
    result = await deps.require_api_key(x_api_key=None)
    assert result is None


@pytest.mark.asyncio
async def test_require_api_key_bypasses_even_with_a_bogus_header_when_disabled(
    monkeypatch: Any,
) -> None:
    from src.api import deps

    monkeypatch.setattr(deps, "get_settings", lambda: _settings(api_key=""))
    result = await deps.require_api_key(x_api_key="anything-at-all")
    assert result is None


# ── resolve_actor ────────────────────────────────────────────────────────


def test_resolve_actor_returns_the_header_value_when_present() -> None:
    from src.api import deps

    assert deps.resolve_actor(x_actor_name="alice") == "alice"


def test_resolve_actor_defaults_to_api_when_absent() -> None:
    from src.api import deps

    assert deps.resolve_actor(x_actor_name=None) == "api"


def test_resolve_actor_defaults_to_api_for_an_empty_header() -> None:
    from src.api import deps

    assert deps.resolve_actor(x_actor_name="") == "api"


def test_resolve_actor_caps_an_overlong_header() -> None:  # SEC-4
    from src.api import deps

    overlong = "z" * 5000
    result = deps.resolve_actor(x_actor_name=overlong)
    assert len(result) <= 128
    assert result == overlong[:128]


# ── get_arq ──────────────────────────────────────────────────────────────


def _request_for(app: FastAPI) -> MagicMock:
    request = MagicMock()
    request.app = app
    return request


def test_get_arq_returns_the_pool_from_app_state() -> None:
    from src.api.deps import get_arq

    app = FastAPI()
    sentinel = object()
    app.state.arq = sentinel
    assert get_arq(_request_for(app)) is sentinel


def test_get_arq_raises_a_clear_runtime_error_when_absent() -> None:
    from src.api.deps import get_arq

    app = FastAPI()
    with pytest.raises(RuntimeError, match="(?i)arq"):
        get_arq(_request_for(app))


# ── log_auth_mode ────────────────────────────────────────────────────────


def test_log_auth_mode_warns_loudly_when_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings(api_key=""))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "expected at least one WARNING-level log record"
    joined = " ".join(r.getMessage().upper() for r in warnings)
    assert "AUTH" in joined
    assert "DISABLED" in joined


def test_log_auth_mode_does_not_warn_when_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from src.api.deps import log_auth_mode

    caplog.set_level(logging.WARNING)
    log_auth_mode(_settings(api_key="secret123"))
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []
