"""Route-level tests for Phase 6's résumé routes —
``src.api.routes.resumes`` (new module; does not exist yet). The whole file
fails at collection (``ModuleNotFoundError``) — RED half of the TDD cycle.

Reverse-match subresource routes (``POST /resumes/{id}/match-jobs`` /
``GET /resumes/{id}/match-results``) are exercised separately in
``test_route_reverse_match.py`` — this file covers upload + list + get only.

**Ambiguities this file locks:**

* ``src.api.routes.resumes`` exposes ``router: APIRouter`` (absolute paths,
  no router-level prefix): ``POST /jobs/{job_id}/resumes`` (upload),
  ``GET /jobs/{job_id}/resumes`` (list), ``GET /resumes/{resume_id}``
  (get one, optional ``?reveal=true`` query param, default ``false``).
* Upload is ``multipart/form-data``: a REPEATED ``files`` field (one or more
  résumé files, individually OR one entry ending ``.zip`` which is expanded
  via ``src.services.zip_upload.expand_zip_entries`` and merged into the
  same accepted/rejected accounting), a ``consent_acknowledged`` form field
  (``"true"``/``"false"``), and an optional ``cover_letter_text`` form
  field (pasted paste-only cover letter — no file-cover-letter case tested
  here). Returns **202** with a JSON array of ``ResumeUploadResult`` rows and
  enqueues ``arq.enqueue_job("parse_resume", str(resume_id))`` ONCE PER
  ACCEPTED résumé (duplicates/rejections are never enqueued).
* **Locked ID-generation design** (resolves what would otherwise be an
  underspecified DB-round-trip ambiguity): ``resume_id`` is minted by the
  SERVICE layer itself (``uuid4()``) BEFORE any I/O — the same value backs
  both the inserted row's primary key and the server-generated blob key
  stem (``resumes/{resume_id}.{ext}``), so the route can enqueue the correct
  id without a second DB round trip.
* A zip containing a path-traversal or over-cap entry is rejected for the
  WHOLE request with a 4xx (``src.services.zip_upload.ZipRejected``
  surfaced through the same ``AppError``-style handler, or a plain
  ``HTTPException`` — either way, status in ``{400, 422}``) — nothing at all
  is enqueued for that request, not even the valid entries alongside it.
* ``GET /resumes/{resume_id}`` — 404 (``NotFoundError`` -> the ``AppError``
  handler) when the id doesn't resolve. **The redaction-boundary byte-scan
  (ADR-006 §4, at the HTTP layer)**: under a BLIND job, none of the
  candidate's real name/email/phone literal byte-sequences appear ANYWHERE
  in the raw response body (not just absent from specific fields); under a
  NON-blind job, at least the email is present verbatim — proving the route
  actually goes through ``resume_service.get_one``'s redaction, not a
  raw re-query.
"""

from __future__ import annotations

import datetime as dt
import json
import zipfile
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import get_arq, require_api_key
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.pool import get_db
from src.storage.blob_store import get_blob_store

_NOW = dt.datetime(2026, 7, 16, tzinfo=dt.UTC)
_PDF_MAGIC = b"%PDF-1.4\nresume content\n" + b"x" * 500

_NAME = "Zzyzxqrst Wibblesworth"
_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_PHONE = "604-555-0192"


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _full_parsed(*, name: str, email: str, phone: str) -> dict[str, Any]:
    return {
        "candidate": {"name": name, "email": email, "phone": phone, "location": None},
        "summary": f"{name} is a senior engineer reachable at {email} or {phone}.",
        "total_years_experience": 8,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [
            {
                "id": "c_001",
                "section": "header",
                "page": 0,
                "text": f"{name} | {email} | {phone}",
            }
        ],
        "cover_letter_chunks": [],
    }


def _get_row(
    *,
    resume_id: UUID,
    job_id: UUID | None = None,
    parsed: dict[str, Any] | None = None,
    candidate_name: str | None = None,
    candidate_email: str | None = None,
    candidate_phone: str | None = None,
) -> _Row:
    return _Row(
        {
            "id": resume_id,
            "job_id": job_id or uuid4(),
            "original_filename": "resume.pdf",
            "mime_type": "application/pdf",
            "file_size_bytes": 12345,
            "sha256": "deadbeef",
            "c_name": candidate_name,
            "c_email": candidate_email,
            "c_phone": candidate_phone,
            "cl_text": None,
            "cover_letter_parsed": None,
            "candidate_email_hash": None,
            "parsed": json.dumps(parsed) if parsed is not None else None,
            "status": "parsed",
            "uploaded_by": None,
            "uploaded_at": _NOW,
            "parsed_at": _NOW,
            "failure_reason": None,
            "consent_acknowledged": True,
        }
    )


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(
    *,
    fetchrow: _Row | None = None,
    fetch: list[_Row] | None = None,
    fetchval: Any = None,
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _mock_blob_store() -> MagicMock:
    store = MagicMock(name="blob_store")
    store.put = AsyncMock(return_value=None)
    return store


def _build_app(
    conn: MagicMock, *, arq: MagicMock | None = None, store: MagicMock | None = None
) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store or _mock_blob_store()
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    app.dependency_overrides[require_api_key] = lambda: None

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, content in entries:
            zf.writestr(zipfile.ZipInfo(filename=name), content)
    return buf.getvalue()


# ── upload: multi-file ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_multi_file_returns_202_and_enqueues_per_file() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[
                ("files", ("a.pdf", _PDF_MAGIC, "application/pdf")),
                ("files", ("b.pdf", _PDF_MAGIC, "application/pdf")),
            ],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert len(body) == 2
    assert arq.enqueue_job.await_count == 2
    for call in arq.enqueue_job.await_args_list:
        assert call.args[0] == "parse_resume"


@pytest.mark.asyncio
async def test_upload_resumes_cover_letter_file_is_read_and_stored() -> None:
    """The route reads the optional ``cover_letter_file`` part and forwards it;
    the service stores it under a server-generated ``cover_letters/`` blob key."""
    conn = _mock_conn()
    store = _mock_blob_store()
    app = _build_app(conn, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[
                ("files", ("a.pdf", _PDF_MAGIC, "application/pdf")),
                ("cover_letter_file", ("cover.pdf", _PDF_MAGIC, "application/pdf")),
            ],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    cover_keys = [
        c.args[0]
        for c in store.put.await_args_list
        if c.args[0].startswith("cover_letters/")
    ]
    assert len(cover_keys) == 1


# ── upload: file-count cap (SEC-2, memory-exhaustion DoS) ────────────────


@pytest.mark.asyncio
async def test_upload_resumes_over_max_count_rejected_nothing_enqueued() -> None:
    from src.services.zip_upload import _MAX_ZIP_ENTRIES

    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    files = [
        ("files", (f"r{i}.pdf", _PDF_MAGIC, "application/pdf"))
        for i in range(_MAX_ZIP_ENTRIES + 1)
    ]
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=files,
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (413, 422)
    arq.enqueue_job.assert_not_awaited()
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_over_max_count_rejected_before_bodies_processed(
    monkeypatch: Any,
) -> None:
    from src.services.zip_upload import _MAX_ZIP_ENTRIES

    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    # Spy the service: the batch must be rejected on count BEFORE any body is
    # read/expanded and BEFORE the service is ever invoked.
    spy = AsyncMock()
    monkeypatch.setattr(resumes_routes.resume_service, "upload_resumes", spy)
    files = [
        ("files", (f"r{i}.pdf", _PDF_MAGIC, "application/pdf"))
        for i in range(_MAX_ZIP_ENTRIES + 1)
    ]
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=files,
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (413, 422)
    spy.assert_not_awaited()
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_over_max_count_never_reads_any_body(
    monkeypatch: Any,
) -> None:
    """Regression guard (security re-audit LOW): pin that the >50-file cap is
    rejected BEFORE any file body is read into memory. A mutation that moves
    the count check to after the ``for f in files: await f.read()`` loop
    would still pass ``test_upload_resumes_over_max_count_rejected_*`` above
    (those only assert nothing was enqueued/persisted downstream) — this test
    spies directly on ``UploadFile.read`` and asserts it is never awaited on
    the over-cap path.
    """
    from starlette.datastructures import UploadFile as StarletteUploadFile

    from src.services.zip_upload import _MAX_ZIP_ENTRIES

    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)

    read_calls = 0
    original_read = StarletteUploadFile.read

    async def _spy_read(self: Any, *args: Any, **kwargs: Any) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return await original_read(self, *args, **kwargs)  # type: ignore[no-any-return]

    monkeypatch.setattr(StarletteUploadFile, "read", _spy_read)

    files = [
        ("files", (f"r{i}.pdf", _PDF_MAGIC, "application/pdf"))
        for i in range(_MAX_ZIP_ENTRIES + 1)
    ]
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=files,
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (413, 422)
    assert read_calls == 0, "over-cap batch must be rejected before any body is read"


@pytest.mark.asyncio
async def test_upload_resumes_at_max_count_is_accepted() -> None:
    from src.services.zip_upload import _MAX_ZIP_ENTRIES

    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    files = [
        ("files", (f"r{i}.pdf", _PDF_MAGIC, "application/pdf"))
        for i in range(_MAX_ZIP_ENTRIES)
    ]
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=files,
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202


# ── upload: zip expansion ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_resumes_zip_expands_and_enqueues_per_entry() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    archive = _zip_bytes([("alice.pdf", _PDF_MAGIC), ("bob.pdf", _PDF_MAGIC)])
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("batch.zip", archive, "application/zip"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    assert arq.enqueue_job.await_count == 2


@pytest.mark.asyncio
async def test_upload_resumes_traversal_zip_rejected_and_nothing_enqueued() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    archive = _zip_bytes([("../evil.pdf", _PDF_MAGIC)])
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("batch.zip", archive, "application/zip"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (400, 422)
    arq.enqueue_job.assert_not_awaited()
    store.put.assert_not_called()


@pytest.mark.asyncio
async def test_upload_resumes_zip_bomb_rejected() -> None:
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_blob_store()
    app = _build_app(conn, arq=arq, store=store)
    huge = b"\x00" * (11 * 1024 * 1024)
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo(filename="bomb.pdf"), huge)
    archive = buf.getvalue()
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", ("batch.zip", archive, "application/zip"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code in (400, 422)
    arq.enqueue_job.assert_not_awaited()


# ── upload: server-generated blob key, retained filename ────────────────


@pytest.mark.asyncio
async def test_upload_resumes_crafted_filename_blob_key_server_generated() -> None:
    conn = _mock_conn()
    store = _mock_blob_store()
    app = _build_app(conn, store=store)
    crafted = "Jane_Doe_Confidential_Salary_Resume.pdf"
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes",
            files=[("files", (crafted, _PDF_MAGIC, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    body = resp.json()
    assert body[0]["original_filename"] == crafted
    key = store.put.await_args.args[0]
    assert "jane" not in key.lower()
    assert "doe" not in key.lower()
    assert "confidential" not in key.lower()


# ── GET /jobs/{job_id}/resumes (list) ────────────────────────────────────


@pytest.mark.asyncio
async def test_list_resumes_returns_200() -> None:
    conn = _mock_conn(fetchval=False, fetch=[])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resumes")
    assert resp.status_code == 200
    assert resp.json() == []


# ── GET /resumes/{id} — 404 + redaction byte-scan ───────────────────────


@pytest.mark.asyncio
async def test_get_resume_404_when_missing() -> None:
    conn = _mock_conn(fetchrow=None)
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_resume_under_blind_job_never_leaks_pii_bytes() -> None:
    resume_id = uuid4()
    parsed = _full_parsed(name=_NAME, email=_EMAIL, phone=_PHONE)
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name=_NAME,
        candidate_email=_EMAIL,
        candidate_phone=_PHONE,
    )
    conn = _mock_conn(fetchrow=row, fetchval=True)  # blind job
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")
    assert resp.status_code == 200
    raw = resp.text
    assert _NAME not in raw
    assert _EMAIL not in raw
    assert _PHONE not in raw


@pytest.mark.asyncio
async def test_get_resume_under_non_blind_job_reveals_pii() -> None:
    resume_id = uuid4()
    parsed = _full_parsed(name=_NAME, email=_EMAIL, phone=_PHONE)
    row = _get_row(
        resume_id=resume_id,
        parsed=parsed,
        candidate_name=_NAME,
        candidate_email=_EMAIL,
        candidate_phone=_PHONE,
    )
    conn = _mock_conn(fetchrow=row, fetchval=False)  # non-blind job
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/resumes/{resume_id}")
    assert resp.status_code == 200
    assert _EMAIL in resp.text
