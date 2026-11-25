"""The localhost CAS origin must survive ANY ``docker compose up``, overlay or not.

PR #111 (fix/cas-local-origin) let the pilot box serve both https://sfuai.ca:8000
and http://localhost:29500. The two ``CAS_LOCAL_*`` values it needs were set only
in ``compose.cas.yml``. Every deploy since that ran a plain
``docker compose up -d --build api worker frontend`` (the recipe the hand-over
docs gave) recreated the containers WITHOUT them, so localhost logins were sent
to sfuai.ca, which does not resolve back to the box from inside the LAN. It
regressed on 2026-11-24 and was reported 2026-11-25 as "keeps coming back".

The fix puts the defaults in ``docker-compose.yml`` itself, where no file set
can drop them. These tests pin that, and that the overlay still agrees.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]

# service -> the CAS_LOCAL_* keys it reads (mirrors compose.cas.yml)
_NEEDS = {
    "api": {
        "CAS_LOCAL_SERVICE_BASE_URL": "API_PORT",
        "CAS_LOCAL_FRONTEND_BASE_URL": "FRONTEND_PORT",
    },
    "frontend": {"CAS_LOCAL_SERVICE_BASE_URL": "API_PORT"},
}


def _env(compose: str, service: str) -> dict[str, Any]:
    data = yaml.safe_load((_REPO / compose).read_text(encoding="utf-8"))
    env = data["services"][service].get("environment") or {}
    assert isinstance(env, dict), f"{compose}:{service} environment must be a map"
    return env


@pytest.mark.parametrize(
    ("service", "key", "port_var"),
    [(s, k, p) for s, keys in _NEEDS.items() for k, p in keys.items()],
)
def test_base_compose_sets_the_localhost_cas_origin(
    service: str, key: str, port_var: str
) -> None:
    value = _env("docker-compose.yml", service).get(key)
    assert value, f"docker-compose.yml {service} lacks {key}; a plain 'up' drops it"
    text = str(value)
    # .env still wins (interpolated), and the default follows the published port.
    assert f"${{{key}:-" in text, text
    assert "http://localhost:" in text and port_var in text, text


@pytest.mark.parametrize(
    ("service", "key"),
    [(s, k) for s, keys in _NEEDS.items() for k in keys],
)
def test_overlay_and_base_agree(service: str, key: str) -> None:
    base = _env("docker-compose.yml", service).get(key)
    overlay = _env("compose.cas.yml", service).get(key)
    if overlay is not None:
        assert overlay == base, f"{service}.{key}: overlay {overlay!r} != base {base!r}"
