"""Unit tests for the FastAPI app — Phase 0 exposes ``/health`` and nothing else.

The lifespan (asyncpg pool, arq, Neo4j) is stubbed out, so these run with no
live services. Job/resume/shortlist routes arrive in Phases 1-6.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.main import app, health

# Template demo routes — Phase 0 deletes the agent-harness app.
DEMO_ROUTES: tuple[str, ...] = (
    "/tasks",
    "/tasks/{task_id}",
    "/tasks/{task_id}/lineage",
    "/memory/similar",
    "/gates/run",
)


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    with patch.object(app.router, "lifespan_context", _noop_lifespan):
        with TestClient(app) as test_client:
            yield test_client


def test_health_endpoint_returns_200_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_handler_returns_ok() -> None:
    assert await health() == {"status": "ok"}


def test_app_is_rebranded() -> None:
    title = app.title.lower()
    assert "recruiter" in title
    assert "harness" not in title


@pytest.mark.parametrize("path", DEMO_ROUTES)
def test_template_demo_routes_are_gone(path: str) -> None:
    paths = {getattr(route, "path", None) for route in app.routes}
    assert path not in paths


def test_health_is_the_only_business_route() -> None:
    paths = {
        getattr(route, "path", "")
        for route in app.routes
        if not getattr(route, "path", "").startswith(("/openapi", "/docs", "/redoc"))
    }
    assert paths == {"/health"}
