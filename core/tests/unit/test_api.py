"""Unit tests for the FastAPI app — Phase 0 exposes ``/health`` and nothing else.

The lifespan (asyncpg pool, arq, Neo4j) is stubbed out, so these run with no
live services. Job/resume/shortlist routes arrive in Phases 1-6.

Phase 1 adds the storage wiring: the lifespan builds a ``BlobStore`` rooted at
``settings.storage_dir`` and parks it on ``app.state.blob_store``, and the
``get_blob_store`` dependency hands it to routes (mirroring ``get_db``). Those
tests import the still-unwritten storage module lazily so this file keeps
collecting the Phase 0 tests until the implementation lands.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


# Phase 6 (API routes): this guard's invariant genuinely changes here — the
# app now legitimately exposes the job/résumé/shortlist business routes, not
# just /health. Updated to a POSITIVE set-equality against the exact route
# table Phase 6 wires (src.api.routes.{jobs,resumes,shortlist}), so the guard
# still catches an ACCIDENTAL route addition/removal — it just no longer
# claims /health is the ONLY business route, because that claim is no longer
# true by design, not because the guard was weakened.
_PHASE_6_ROUTES: frozenset[str] = frozenset(
    {
        "/health",
        "/jobs",
        "/jobs/jd-extract",
        "/jobs/{job_id}",
        "/jobs/{job_id}/status",
        "/jobs/{job_id}/resumes",
        "/resumes/{resume_id}",
        "/resumes/{resume_id}/match-jobs",
        "/resumes/{resume_id}/match-results",
        "/jobs/{job_id}/shortlist",
        "/jobs/{job_id}/shortlist/export",
        "/shortlist/{entry_id}",
    }
)


def _all_route_paths(routes: object) -> set[str]:
    """Flatten a ``routes`` iterable into the full set of registered path
    strings, recursively.

    The installed FastAPI wraps every ``include_router(...)`` target as an
    opaque ``_IncludedRouter`` (its own ``.path`` is ``None``; the real
    ``APIRoute``s live on ``.original_router.routes``) rather than flattening
    them directly onto ``app.routes`` — so a shallow ``{r.path for r in
    app.routes}`` silently drops every Phase-6 route behind an
    ``include_router`` call. Walk recursively so the guard actually sees them.
    """
    paths: set[str] = set()
    for route in routes:  # type: ignore[attr-defined]
        sub_router = getattr(route, "original_router", None)
        if sub_router is not None:
            paths |= _all_route_paths(getattr(sub_router, "routes", ()))
            continue
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
    return paths


def test_health_is_the_only_business_route() -> None:
    paths = {
        p
        for p in _all_route_paths(app.routes)
        if not p.startswith(("/openapi", "/docs", "/redoc"))
    }
    assert paths == _PHASE_6_ROUTES


def test_the_cut_review_workflow_routes_are_absent() -> None:
    """hris's shortlist decision/stage routes are deliberately never wired —
    there is no review pipeline in this project."""
    paths = _all_route_paths(app.routes)
    assert "/shortlist/{entry_id}/decision" not in paths
    assert "/shortlist/{entry_id}/stage" not in paths


# ── Phase 1 — BlobStore wiring ──────────────────────────────────────────────


def _request_for(app_: FastAPI) -> MagicMock:
    request = MagicMock()
    request.app = app_
    return request


@pytest.mark.asyncio
async def test_lifespan_parks_a_blob_store_on_app_state(tmp_path: Path) -> None:
    """The real lifespan (external IO mocked) must set ``app.state.blob_store``.

    The store is proved to be rooted at ``settings.storage_dir`` functionally:
    a ``put`` through it lands under ``tmp_path`` — no dependence on the
    implementation's private root attribute.
    """
    from src.api.main import lifespan
    from src.settings import Settings
    from src.storage.blob_store import BlobStore

    fresh = FastAPI()
    arq = MagicMock()
    arq.close = AsyncMock()
    driver = MagicMock()
    driver.close = AsyncMock()

    with (
        patch(
            "src.api.main.get_settings",
            return_value=Settings(storage_dir=str(tmp_path)),
        ),
        patch("src.api.main.init_pool", AsyncMock(return_value=MagicMock())),
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=driver),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=arq)),
    ):
        async with lifespan(fresh):
            store = getattr(fresh.state, "blob_store", None)
            assert isinstance(store, BlobStore)
            await store.put("wire.txt", b"ok")

    assert (tmp_path / "wire.txt").read_bytes() == b"ok"


# ── L3 (security re-audit round 2): SKILL_HASH_SALT symmetry with the worker ──
#
# ``src.worker.main.startup`` already refuses to start on an empty
# ``skill_hash_salt`` (ADR-008). The API lifespan never hashes a skill name
# itself today, so this is currently latent — but it must fail exactly as
# loud, for exactly the same reason (an unsalted hash of a non-vocab skill
# name is dictionary-attackable), should a future code path reach it from
# this process, or should the salt only be misconfigured for the API.


@pytest.mark.asyncio
async def test_lifespan_raises_when_skill_hash_salt_is_empty(tmp_path: Path) -> None:
    from src.api.main import lifespan
    from src.settings import Settings

    fresh = FastAPI()
    settings = Settings(storage_dir=str(tmp_path), skill_hash_salt="")

    with (
        patch("src.api.main.get_settings", return_value=settings),
        patch(
            "src.api.main.init_pool", AsyncMock(return_value=MagicMock())
        ) as init_pool,
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=MagicMock()),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=MagicMock())),
    ):
        with pytest.raises(RuntimeError, match="SKILL_HASH_SALT"):
            async with lifespan(fresh):
                pass

    init_pool.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_does_not_raise_when_skill_hash_salt_is_configured(
    tmp_path: Path,
) -> None:
    from src.api.main import lifespan
    from src.settings import Settings

    fresh = FastAPI()
    arq = MagicMock()
    arq.close = AsyncMock()
    driver = MagicMock()
    driver.close = AsyncMock()
    settings = Settings(storage_dir=str(tmp_path), skill_hash_salt="a-real-salt")

    with (
        patch("src.api.main.get_settings", return_value=settings),
        patch("src.api.main.init_pool", AsyncMock(return_value=MagicMock())),
        patch("src.api.main.init_schema", AsyncMock()),
        patch("src.api.main.AsyncGraphDatabase.driver", return_value=driver),
        patch("src.api.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.api.main.create_pool", AsyncMock(return_value=arq)),
    ):
        async with lifespan(fresh):
            pass  # must not raise


def test_get_blob_store_returns_the_store_from_app_state() -> None:
    from src.storage.blob_store import get_blob_store

    fresh = FastAPI()
    sentinel = object()
    fresh.state.blob_store = sentinel
    assert get_blob_store(_request_for(fresh)) is sentinel


def test_get_blob_store_raises_a_clear_error_when_absent() -> None:
    """Mirrors the ``get_db`` missing-pool test — a distinct, clear failure."""
    from src.storage.blob_store import get_blob_store

    fresh = FastAPI()
    with pytest.raises(RuntimeError, match="(?i)blob"):
        get_blob_store(_request_for(fresh))
