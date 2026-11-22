"""fix/cas-local-origin — one deployment, two browser origins.

The pilot box is served publicly at ``https://sfuai.ca:8000`` (nginx → app),
and the operator ALSO uses ``http://localhost:29500`` on the box itself —
``sfuai.ca`` is not reachable from inside the LAN (no hairpin NAT). CAS needs
a browser-reachable service URL, and before this fix that was a single static
setting: pointing it at sfuai.ca broke localhost, pointing it at localhost
broke every outside user (2026-10-08 → 10-14, both happened).

``cas_local_service_base_url`` / ``cas_local_frontend_base_url`` (both empty
= feature off, behaviour unchanged) are used ONLY when the request's own
``Host`` is a loopback name. Through nginx the API's ``Host`` is the literal
``$server_name`` and Flask's is the ProxyFix'd ``sfuai.ca:8000``, so outside
traffic can never select the local pair; a forged non-loopback ``Host`` falls
back to the static public value, never to anything request-derived.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse, urlsplit
from uuid import uuid4

import flask
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app as flask_app
from src.api.routes import auth as auth_routes
from src.models.pool import get_db
from src.services.cas_origin import is_loopback_host
from src.settings import Settings

PUBLIC = "https://sfuai.ca:8000"
LOCAL_API = "http://localhost:29800"
LOCAL_UI = "http://localhost:29500"


def _settings(*, local: bool = True) -> Settings:
    return Settings(
        cas_enabled=True,
        cas_server_url="https://cas.example.edu/cas",
        cas_service_base_url=PUBLIC,
        cas_frontend_base_url=PUBLIC,
        cas_local_service_base_url=LOCAL_API if local else "",
        cas_local_frontend_base_url=LOCAL_UI if local else "",
        skill_hash_salt="test-salt",
        pii_key="test-key",
    )


class _NoDb:
    async def fetchrow(self, *a: Any, **k: Any) -> None:
        return None

    async def execute(self, *a: Any, **k: Any) -> str:
        return "OK"


def _api() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)

    async def _db() -> AsyncIterator[Any]:
        yield _NoDb()

    app.dependency_overrides[get_db] = _db
    return app


def _client(base_url: str) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_api()), base_url=base_url)


def _service_of(location: str) -> str:
    return parse_qs(urlparse(location).query)["service"][0]


# ── the loopback predicate ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost:29800", "127.0.0.1:29500", "[::1]:29500", "LOCALHOST:1"],
)
def test_loopback_hosts_are_recognised(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "",
        "sfuai.ca",
        "sfuai.ca:8000",
        "localhost.evil.com",
        "evil.com:29800",
        "127.0.0.1.evil.com",
        "192.168.1.80:29500",
    ],
)
def test_non_loopback_hosts_are_rejected(host: str) -> None:
    assert not is_loopback_host(host)


# ── API: /auth/cas/login ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_from_localhost_uses_the_local_service_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: _settings())
    async with _client(LOCAL_API) as c:
        resp = await c.get("/auth/cas/login", params={"next": "/jobs"})
    assert resp.status_code == 302
    assert _service_of(resp.headers["location"]).startswith(
        f"{LOCAL_API}/auth/cas/validate?"
    )


@pytest.mark.asyncio
async def test_login_through_the_proxy_keeps_the_public_service_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: _settings())
    # nginx sends Host: $server_name, i.e. the bare "sfuai.ca".
    async with _client("http://sfuai.ca") as c:
        resp = await c.get("/auth/cas/login", params={"next": "/jobs"})
    assert _service_of(resp.headers["location"]).startswith(
        f"{PUBLIC}/auth/cas/validate?"
    )


@pytest.mark.asyncio
async def test_a_forged_host_falls_back_to_the_public_base_not_the_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: _settings())
    async with _client("http://evil.example:29800") as c:
        resp = await c.get("/auth/cas/login")
    service = _service_of(resp.headers["location"])
    assert service.startswith(f"{PUBLIC}/")
    assert "evil" not in service


@pytest.mark.asyncio
async def test_unconfigured_local_pair_leaves_localhost_on_the_static_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: _settings(local=False))
    async with _client(LOCAL_API) as c:
        resp = await c.get("/auth/cas/login")
    assert _service_of(resp.headers["location"]).startswith(f"{PUBLIC}/")


# ── API: /auth/cas/validate ──────────────────────────────────────────────


def _mock_validate(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    validate = AsyncMock(return_value="alice")
    monkeypatch.setattr(auth_routes.cas_service, "validate_ticket", validate)
    monkeypatch.setattr(
        auth_routes.user_service,
        "provision_or_get",
        AsyncMock(return_value=SimpleNamespace(id=uuid4())),
    )
    monkeypatch.setattr(
        auth_routes.session_service,
        "create_session",
        AsyncMock(return_value=SimpleNamespace(id="tok")),
    )
    return validate


@pytest.mark.asyncio
async def test_validate_from_localhost_presents_the_local_service_and_lands_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: _settings())
    validate = _mock_validate(monkeypatch)
    async with _client(LOCAL_API) as c:
        resp = await c.get(
            "/auth/cas/validate", params={"ticket": "ST-1", "next": "/jobs"}
        )
    assert resp.status_code == 302
    assert resp.headers["location"] == f"{LOCAL_UI}/jobs"
    service = validate.await_args.kwargs["service_url"]
    assert service.startswith(f"{LOCAL_API}/auth/cas/validate?")


@pytest.mark.asyncio
async def test_validate_through_the_proxy_lands_on_the_public_frontend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: _settings())
    validate = _mock_validate(monkeypatch)
    async with _client("http://sfuai.ca") as c:
        resp = await c.get(
            "/auth/cas/validate", params={"ticket": "ST-1", "next": "/jobs"}
        )
    assert resp.headers["location"] == f"{PUBLIC}/jobs"
    assert validate.await_args.kwargs["service_url"].startswith(f"{PUBLIC}/")


# ── API: /auth/cas/logout ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_logout_from_localhost_returns_to_the_local_frontend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: _settings())
    async with _client(LOCAL_API) as c:
        resp = await c.get("/auth/cas/logout")
    assert _service_of(resp.headers["location"]) == LOCAL_UI


@pytest.mark.asyncio
async def test_logout_through_the_proxy_returns_to_the_public_frontend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: _settings())
    async with _client("http://sfuai.ca") as c:
        resp = await c.get("/auth/cas/logout")
    assert _service_of(resp.headers["location"]) == PUBLIC


# ── Flask frontend: the CAS gate and the logout link ─────────────────────


@pytest.fixture
def flask_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        api_client,
        "get_cas_user",
        MagicMock(
            return_value={"authenticated": False, "username": None, "cas_enabled": True}
        ),
    )
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _origin(location: str) -> str:
    parsed = urlsplit(location)
    return f"{parsed.scheme}://{parsed.netloc}"


def test_gate_on_localhost_sends_the_browser_to_the_local_api(
    flask_client: Any,
) -> None:
    resp = flask_client.get("/", base_url=LOCAL_UI)
    assert resp.status_code == 302
    assert _origin(resp.headers["Location"]) == LOCAL_API
    assert urlsplit(resp.headers["Location"]).path == "/auth/cas/login"


def test_gate_through_the_proxy_sends_the_browser_to_the_public_origin(
    flask_client: Any,
) -> None:
    resp = flask_client.get("/", base_url=PUBLIC)
    assert resp.status_code == 302
    assert _origin(resp.headers["Location"]) == PUBLIC


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [(LOCAL_UI, f"{LOCAL_API}/auth/cas/logout"), (PUBLIC, f"{PUBLIC}/auth/cas/logout")],
)
def test_logout_link_follows_the_origin_the_page_was_served_on(
    monkeypatch: pytest.MonkeyPatch, base_url: str, expected: str
) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _settings())
    with flask_app.test_request_context("/", base_url=base_url):
        flask.g.cas_user = {"authenticated": True, "username": "alice", "role": "admin"}
        ctx: dict[str, Any] = {}
        flask_app.update_template_context(ctx)
    assert ctx["logout_url"] == expected
