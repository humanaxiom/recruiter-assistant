"""Route tests for FU-1's AUDITED reveal — ``POST /resumes/{id}/reveal`` on
``src.api.routes.resumes``.

Revealing a candidate is the de-anonymization action, so the route must:
* probe existence FIRST — a missing id 404s and writes NO audit row and never
  decrypts;
* on an existing id, record exactly one ``reveal_audit`` row (via
  ``reveal_service.record_reveal``) with the resolved actor, THEN return the
  UN-blinded ``ResumeOut`` (via ``resume_service.get_one(..., reveal=True)``).

``record_reveal`` and ``get_one`` are monkeypatched so this isolates the ROUTE
contract (order, audit-before-return, 404-before-audit); their own behaviour is
covered by ``test_services_reveal.py`` and the résumé-service tests.
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

from src.api.deps import require_api_key
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.resumes import CandidateInfo, ResumeOut

_NOW = dt.datetime(2026, 7, 18, tzinfo=dt.UTC)


def _unblinded(resume_id: UUID) -> ResumeOut:
    """A reveal returns full candidate PII — name/email/phone populated."""
    return ResumeOut(
        id=resume_id,
        job_id=uuid4(),
        original_filename="Casey_Rivera_Resume.pdf",
        mime_type="application/pdf",
        file_size_bytes=1234,
        sha256="a" * 64,
        candidate=CandidateInfo(
            name="Casey Rivera",
            email="casey.rivera@example.test",
            phone="+1-555-0100",
            location="Seattle, WA",
        ),
        candidate_email_hash=None,
        parsed=None,
        status="parsed",
        uploaded_by="api",
        uploaded_at=_NOW,
        parsed_at=_NOW,
        failure_reason=None,
        consent_acknowledged=True,
        blinded=False,
    )


def _mock_conn(*, exists: bool) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=(uuid4() if exists else None))
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    return conn


def _build_app(conn: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[require_api_key] = lambda: None

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_reveal_404_on_missing_resume_writes_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    record = AsyncMock(return_value=uuid4())
    get_one = AsyncMock()
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", record)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(exists=False))
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 404
    record.assert_not_awaited()  # no audit row for a nonexistent id
    get_one.assert_not_awaited()  # never decrypts


@pytest.mark.asyncio
async def test_reveal_records_audit_then_returns_unblinded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    record = AsyncMock(return_value=uuid4())
    get_one = AsyncMock(return_value=_unblinded(resume_id))
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", record)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(exists=True))
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal?context=shortlist")

    assert resp.status_code == 200
    body = resp.json()
    # Un-blinded: real identity is present (this is the whole point of reveal).
    assert body["candidate"]["name"] == "Casey Rivera"
    assert body["candidate"]["email"] == "casey.rivera@example.test"
    # Audited: exactly one row, keyed on this résumé, with the context.
    record.assert_awaited_once()
    kwargs = record.await_args.kwargs
    assert kwargs["resume_id"] == resume_id
    assert kwargs["context"] == "shortlist"
    # Un-blind is via the audited reveal=True read, not a raw blind read.
    get_one.assert_awaited_once()
    assert get_one.await_args.kwargs.get("reveal") is True


@pytest.mark.asyncio
async def test_reveal_actor_comes_from_x_actor_name_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", record)
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_unblinded(resume_id)),
    )

    app = _build_app(_mock_conn(exists=True))
    async with await _client(app) as client:
        resp = await client.post(
            f"/resumes/{resume_id}/reveal",
            headers={"X-Actor-Name": "alice@example.test"},
        )

    assert resp.status_code == 200
    assert record.await_args.kwargs["actor"] == "alice@example.test"
