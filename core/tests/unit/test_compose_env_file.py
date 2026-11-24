"""Every service that runs the app image must load ``.env``.

A ``.env`` setting reaches a container only if compose lets it. ``api`` and
``frontend`` declared ``env_file: [.env]`` but ``worker`` did not, so a value
set only in ``.env`` (``TALEO_ENABLED=true``) applied to the API and never to
the worker, where ``sync_taleo_jobs`` runs; the Taleo import silently did
nothing. The app services are derived from the build context, not hard-coded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_COMPOSE_FILES = ["docker-compose.yml", "compose.cas.yml"]


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((_REPO / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _env_files(service: dict[str, Any]) -> list[str]:
    raw = service.get("env_file", [])
    if isinstance(raw, str):
        return [raw]
    return [e if isinstance(e, str) else str(e.get("path")) for e in raw]


def _app_services() -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = _load("docker-compose.yml")["services"]
    out: dict[str, dict[str, Any]] = {}
    for name, svc in services.items():
        build = svc.get("build")
        if isinstance(build, dict) and build.get("context") == "./core":
            out[name] = svc
    return out


def test_app_service_set_includes_worker() -> None:
    names = set(_app_services())
    assert {"api", "worker", "frontend"} <= names


@pytest.mark.parametrize("name", ["api", "worker", "frontend"])
def test_named_app_services_load_dotenv(name: str) -> None:
    files = _env_files(_app_services()[name])
    assert ".env" in files, f"{name} lacks env_file .env"


def test_every_derived_app_service_loads_dotenv() -> None:
    missing = [n for n, s in _app_services().items() if ".env" not in _env_files(s)]
    assert not missing, f"services running the app image without .env: {missing}"


@pytest.mark.parametrize("name", _COMPOSE_FILES[1:])
def test_overrides_never_drop_dotenv_for_app_services(name: str) -> None:
    """Overrides may add env_file entries but must not re-declare it without .env.

    docker-compose.stress.yml is skipped: it uses ``!override`` tags that
    safe_load rejects, and it declares no env_file.
    """
    app = set(_app_services())
    for svc_name, svc in _load(name).get("services", {}).items():
        if svc_name in app and "env_file" in svc:
            assert ".env" in _env_files(svc)
