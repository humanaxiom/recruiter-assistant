"""RED — ``POST /resumes/{resume_id}/reparse``, the résumé side of the JD
recovery path already shipped as ``POST /jobs/{job_id}/reparse``.

**Why (user-sourced).** On the DTO's real bundle (2026-09-19), 4 of 19 résumé
parses failed: 1 degraded (skills-pass empty-content fallback) and 3 abandoned
by the stalled-parse reconciler. The only recovery today is re-upload.
ROADMAP §5 records the gap explicitly: "No ``POST /resumes/{id}/reparse``
route — a degraded résumé cannot be recovered without re-upload."

Mirrors ``test_route_job_reparse.py``'s shape but through the SERVICE layer
(``resume_service.reset_for_reparse`` / ``resume_service.get_one``), the same
convention ``test_route_resumes_withdraw.py`` uses — this file isolates the
ROUTE contract; the service's own SQL/state-machine behaviour is pinned in
``test_resume_reparse_service.py`` and the real-Postgres
``test_resume_reparse_pg.py``.

None of ``resume_service.reset_for_reparse``, ``ResumeReparseOut`` or the
route itself exist yet — every test below fails today at import
(``ImportError``) or at the first request (404 from FastAPI's own router).
RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import resumes as resumes_routes
from src.errors import AppError, NotFoundError
from src.models.pool import get_db

# ``ResumeReparseOut`` is imported by name, per the task: fail on ImportError
# today rather than the test module silently collecting nothing.
from src.schemas.resumes import (  # noqa: F401
    CandidateInfo,
    ResumeOut,
    ResumeReparseOut,
)

_NOW = dt.datetime(2026, 9, 19, 12, 0, tzinfo=dt.UTC)

_NON_WRITER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)


def _resume_out(
    resume_id: UUID,
    *,
    status: str = "failed",
    withdrawn_at: dt.datetime | None = None,
) -> ResumeOut:
    return ResumeOut(
        id=resume_id,
        job_id=uuid4(),
        original_filename="resume.pdf",
        mime_type="application/pdf",
        file_size_bytes=1234,
        sha256="a" * 64,
        candidate=CandidateInfo(name=None, email=None, phone=None, location=None),
        candidate_email_hash=None,
        parsed=None,
        status=status,  # type: ignore[arg-type]
        uploaded_by="api",
        uploaded_at=_NOW,
        parsed_at=_NOW if status == "parsed" else None,
        failure_reason=(
            "LLMUnavailableError: circuit breaker open" if status == "failed" else None
        ),
        consent_acknowledged=True,
        blinded=True,
        withdrawn_at=withdrawn_at,
    )


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=None)
    return conn


def _build_app(
    conn: MagicMock, *, arq: MagicMock | None = None, role: Role = Role.ADMIN
) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── 202: eligible rows ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reparse_a_failed_resume_returns_202_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(resumes_routes.resume_service, "reset_for_reparse", reset)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 202
    assert resp.json() == {"id": str(resume_id), "status": "queued"}


@pytest.mark.asyncio
async def test_reparse_a_degraded_parsed_resume_returns_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eligible = failed, OR parsed-but-degraded — the other half of the
    incident (1 of 4 dead rows was degraded, not failed)."""
    resume_id = uuid4()
    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(resumes_routes.resume_service, "reset_for_reparse", reset)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 202
    reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_reparse_enqueues_parse_resume_with_no_custom_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service, "reset_for_reparse", AsyncMock(return_value=True)
    )
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("parse_resume", str(resume_id))
    # exactly these positional args, nothing else (no _job_id kwarg)
    call = arq.enqueue_job.await_args
    assert call.args == ("parse_resume", str(resume_id))
    assert call.kwargs == {}


@pytest.mark.asyncio
async def test_reset_happens_before_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same race as ``job_service.clear_parse_failure``: reset FIRST, enqueue
    SECOND, or a fast worker could finish and be immediately wiped."""
    resume_id = uuid4()
    order: list[str] = []

    async def _reset(*_a: Any, **_k: Any) -> bool:
        order.append("reset")
        return True

    async def _enqueue(*_a: Any, **_k: Any) -> None:
        order.append("enqueue")

    monkeypatch.setattr(resumes_routes.resume_service, "reset_for_reparse", _reset)
    arq = MagicMock(enqueue_job=_enqueue)
    app = _build_app(_mock_conn(), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 202
    assert order == ["reset", "enqueue"]


# ── 409: ineligible rows, with a plain per-state reason ──────────────────


@pytest.mark.asyncio
async def test_reparse_409s_for_a_cleanly_parsed_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "reset_for_reparse",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id, status="parsed")),
    )
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 409
    assert "nothing to recover" in resp.json()["detail"].lower()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["uploaded", "parsing"])
async def test_reparse_409s_when_a_parse_is_already_in_flight(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "reset_for_reparse",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_resume_out(resume_id, status=status)),
    )
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 409
    assert "already queued or running" in resp.json()["detail"].lower()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_reparse_409s_for_a_withdrawn_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "reset_for_reparse",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(
            return_value=_resume_out(resume_id, status="failed", withdrawn_at=_NOW)
        ),
    )
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 409
    assert "withdrawn" in resp.json()["detail"].lower()
    arq.enqueue_job.assert_not_awaited()


# ── 404: missing résumé ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reparse_404s_when_the_resume_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "reset_for_reparse",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(side_effect=NotFoundError(f"resume {resume_id} not found")),
    )
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 404
    arq.enqueue_job.assert_not_awaited()


# ── RBAC ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("role", _NON_WRITER_ROLES)
async def test_reparse_403s_for_non_writers_and_never_touches_the_row(
    monkeypatch: pytest.MonkeyPatch, role: Role
) -> None:
    resume_id = uuid4()
    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(resumes_routes.resume_service, "reset_for_reparse", reset)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(), arq=arq, role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 403
    reset.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_reparse_as_recruiter_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Admin is not the only writer — a recruiter owns this workflow too."""
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "reset_for_reparse",
        AsyncMock(return_value=True),
    )
    app = _build_app(_mock_conn(), role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 202
