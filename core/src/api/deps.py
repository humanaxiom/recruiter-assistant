"""Cross-cutting API dependencies — auth switch, actor resolution, arq dep.

**The configurable auth switch (decision 1).** ``Settings.api_key`` is the ONE
switch: empty string = auth DISABLED (local dev, fail-open by EXPLICIT
configuration, never by omission-in-code); non-empty = auth ENABLED
(fail-closed — a missing/wrong ``X-API-Key`` header raises 401).
``log_auth_mode`` logs a loud WARNING at API startup when the switch is off,
so a misconfigured deploy is impossible to miss in the logs.
"""

from __future__ import annotations

import logging
import secrets

from arq.connections import ArqRedis
from fastapi import Header, HTTPException, Request

from src.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Cap the attacker-controlled ``X-Actor-Name`` header so an unbounded string
# cannot fill the audit ``created_by``/``uploaded_by`` TEXT columns.
_MAX_ACTOR_NAME_CHARS = 128


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency guarding every business route.

    Disabled (``settings.api_key == ""``) ALWAYS passes, regardless of what
    ``x_api_key`` carries. Enabled: the header must match exactly, else 401.
    """
    settings = get_settings()
    if not settings.api_key:
        return None
    # Constant-time compare — never short-circuit on the first differing byte,
    # so response timing cannot be used to recover the key byte-by-byte.
    if not (x_api_key and secrets.compare_digest(x_api_key, settings.api_key)):
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return None


def resolve_actor(
    x_actor_name: str | None = Header(default=None, alias="X-Actor-Name"),
) -> str:
    """The optional ``X-Actor-Name`` header (truncated to
    ``_MAX_ACTOR_NAME_CHARS``), or the fixed default ``"api"``."""
    if x_actor_name:
        return x_actor_name[:_MAX_ACTOR_NAME_CHARS]
    return "api"


def get_arq(request: Request) -> ArqRedis:
    """FastAPI dependency — hand the lifespan-built arq pool to routes.

    Mirrors ``get_db``/``get_blob_store``: raises ``RuntimeError`` when the
    lifespan has not parked a pool on ``app.state.arq``.
    """
    arq: ArqRedis | None = getattr(request.app.state, "arq", None)
    if arq is None:
        raise RuntimeError("arq pool not initialised (lifespan not running)")
    return arq


def log_auth_mode(settings: Settings) -> None:
    """Called ONCE at API startup. Loud WARNING when auth is disabled, an
    informational line otherwise."""
    if not settings.api_key:
        logger.warning(
            "AUTH DISABLED — api_key is empty; every route is unauthenticated. "
            "Set API_KEY to enable auth."
        )
    else:
        logger.info("auth.enabled")
