"""Slice D1 (RED) -- route-level tests for ``src.api.routes.resume_splits``.

The module does not exist yet, so every test fails (the router is imported
INSIDE ``_build_app``, never at module level, so one missing module fails the
tests individually instead of aborting collection of the whole suite).

Harness: the same shape as ``test_route_resumes.py`` /
``test_route_resumes_session_gate.py`` -- a bare ``FastAPI`` with the router
included at ABSOLUTE paths (no ``/api/v1`` prefix; ``main.py`` adds none for
the sibling ``resumes`` router either), the ``get_db`` / ``get_blob_store`` /
``get_arq`` / ``resolve_role`` / ``resolve_user`` dependencies overridden, and
the collaborators patched AT THEIR DEFINING MODULE
(``src.services.split_request_service``, ``...resume_ingest_service``,
``...audit_service``) so the tests do not care how the route aliases them.

Route contract these tests pin (the coder may add to it, not subtract):

* ``POST /jobs/{job_id}/resumes/split-upload`` -- multipart, repeated
  ``files`` + ``consent_acknowledged`` form field; 202; body mentions the new
  request id; ``split_request_service.create`` once per file; then
  ``arq.enqueue_job("segment_taleo_export", str(split_id))`` AFTER it.
* ``GET /resume-splits/{id}`` / ``POST .../correct`` / ``.../accept`` /
  ``.../discard``; ``correct`` and ``accept`` take JSON ``proposal_version``.
* Rows come back from ``conn.fetchrow`` keyed by the ``resume_split_requests``
  column names (plus ``blind_review`` and ``job_id``).
* The real DB behaviour (SELECT FOR UPDATE, blob deletion, recompute from the
  stored blob) is pinned by ``tests/integration/test_resume_split_routes_pg.py``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import textwrap
from collections.abc import AsyncIterator, Iterator
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import fitz  # type: ignore[import-untyped]
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, get_arq, resolve_role, resolve_user
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.auth import User
from src.schemas.taleo_split import ProposalApplicant, SplitProposal
from src.settings import get_settings
from src.storage.blob_store import get_blob_store

_NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
_FAKE_NAME = "Zzyzxqrst Wibblesworth"
_FAKE_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_UPLOAD_NAME = "zzyzx_wibbles_export.pdf"
_PAD = " ".join(["Experience in recruiting and operations."] * 8)


def _pdf(pages: list[str]) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        lines = [w for ln in text.splitlines() for w in (textwrap.wrap(ln, 80) or [""])]
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + i * 14), line, fontsize=9)
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _export(n_pages: int = 3) -> bytes:
    return _pdf(
        [f"Applicant {i}\napplicant{i}@example.test\n{_PAD}" for i in range(n_pages)]
    )


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _split_row(
    *, status: str = "proposed", version: int = 1, ingest: Any = None
) -> _Row:
    return _Row(
        {
            "id": uuid4(),
            "job_id": uuid4(),
            "status": status,
            "source_blob_key": "split_sources/x.pdf",
            "source_sha256": "a" * 64,
            "source_filename": "export.pdf",
            "page_count": 3,
            "proposal": b"cipher",
            "proposal_version": version,
            "diagnostics": json.dumps({"missing": [], "duplicated": []}),
            "method": "heuristic",
            "fallback_reason": "llm_unavailable",
            "failure_reason": None,
            "consent_acknowledged": True,
            "created_by": "rec",
            "created_at": _NOW,
            "updated_at": _NOW,
            "accepted_by": None,
            "accepted_at": None,
            "ingest_result": json.dumps(ingest) if ingest is not None else None,
            "blind_review": False,
        }
    )


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(row: _Row | None = None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=row)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=uuid4() if row is not None else None)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _mock_store(blob: bytes | None = None) -> MagicMock:
    store = MagicMock(name="blob_store")
    store.put = AsyncMock(return_value=None)
    store.get = AsyncMock(return_value=blob if blob is not None else _export())
    store.delete = AsyncMock(return_value=None)
    return store


def _session_user(*, role: str) -> User:
    return User(
        id=uuid4(),
        cas_username="priya",
        display_name="priya",
        email=None,
        role=role,
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


def _build_app(
    conn: MagicMock,
    *,
    arq: MagicMock | None = None,
    store: MagicMock | None = None,
    key_role: Role = Role.ADMIN,
) -> FastAPI:
    from src.api.routes import resume_splits  # RED: module does not exist yet

    app = FastAPI()
    app.include_router(resume_splits.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store or _mock_store()
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    app.dependency_overrides[resolve_role] = lambda: key_role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _Spies:
    """Every collaborator whose being awaited would prove a write happened."""

    def __init__(self, mp: pytest.MonkeyPatch, split_id: UUID) -> None:
        self.create = AsyncMock(return_value=split_id)
        self.discard = AsyncMock(return_value=True)
        self.mark_accepted = AsyncMock(return_value=True)
        self.store_proposal = AsyncMock(return_value=True)
        self.load_proposal = AsyncMock(
            return_value=SplitProposal(
                applicants=[
                    ProposalApplicant(
                        candidate_name=_FAKE_NAME,
                        resume_pages=[1],
                        cover_letter_pages=[2],
                    )
                ]
            )
        )
        self.ingest = AsyncMock(return_value=[])
        self.audit = AsyncMock(return_value=None)
        base = "src.services.split_request_service"
        mp.setattr(f"{base}.create", self.create)
        mp.setattr(f"{base}.discard", self.discard)
        mp.setattr(f"{base}.mark_accepted", self.mark_accepted)
        mp.setattr(f"{base}.store_proposal", self.store_proposal)
        mp.setattr(f"{base}.load_proposal", self.load_proposal)
        mp.setattr("src.services.resume_ingest_service.ingest_files", self.ingest)
        mp.setattr("src.services.audit_service.record_audit", self.audit)

    def writes(self) -> list[AsyncMock]:
        return [
            self.create,
            self.discard,
            self.mark_accepted,
            self.store_proposal,
            self.ingest,
            self.audit,
        ]


@pytest.fixture
def split_id() -> UUID:
    return uuid4()


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch, split_id: UUID) -> _Spies:
    return _Spies(monkeypatch, split_id)


@pytest.fixture
def small_caps(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("SPLIT_MAX_FILE_MIB", "1")
    monkeypatch.setenv("SPLIT_MAX_PAGES", "3")
    monkeypatch.setenv("SPLIT_MAX_FILES", "2")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _upload_url(job_id: UUID | None = None) -> str:
    return f"/jobs/{job_id or uuid4()}/resumes/split-upload"


def _pdf_part(name: str = _UPLOAD_NAME, data: bytes | None = None) -> Any:
    return ("files", (name, data if data is not None else _export(), "application/pdf"))


# ── upload: happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_returns_202_with_request_id_and_enqueues_segmentation(
    spies: _Spies, split_id: UUID
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(_split_row()), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(), files=[_pdf_part()], data={"consent_acknowledged": "true"}
        )
    assert resp.status_code == 202
    assert str(split_id) in resp.text
    spies.create.assert_awaited_once()
    arq.enqueue_job.assert_awaited_once_with("segment_taleo_export", str(split_id))


@pytest.mark.asyncio
async def test_upload_enqueues_only_after_the_row_is_created(
    spies: _Spies, split_id: UUID
) -> None:
    order: list[str] = []

    async def _create(*_a: Any, **_k: Any) -> UUID:
        order.append("create")
        return split_id

    async def _enqueue(*_a: Any, **_k: Any) -> None:
        order.append("enqueue")

    spies.create.side_effect = _create
    arq = MagicMock(enqueue_job=AsyncMock(side_effect=_enqueue))
    app = _build_app(_mock_conn(_split_row()), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(), files=[_pdf_part()], data={"consent_acknowledged": "true"}
        )
    assert resp.status_code == 202
    assert order == ["create", "enqueue"]


@pytest.mark.asyncio
async def test_upload_audit_carries_counts_and_sha256_but_no_filename_or_text(
    spies: _Spies,
) -> None:
    data = _export()
    app = _build_app(_mock_conn(_split_row()))
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(),
            files=[_pdf_part(data=data)],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    spies.audit.assert_awaited_once()
    assert spies.audit.await_args is not None
    kwargs = spies.audit.await_args.kwargs
    assert kwargs["action"] == "resume_split_upload"
    blob = json.dumps(kwargs["details"]) + str(kwargs.get("context"))
    assert hashlib.sha256(data).hexdigest() in blob
    assert "zzyzx" not in blob.lower()
    assert "applicant0@example.test" not in blob
    assert "Experience in recruiting" not in blob


# ── upload: rejections store and enqueue nothing ─────────────────────────────


@pytest.mark.parametrize(
    ("name", "payload", "mime"),
    [
        ("notes.txt", b"hello world, plainly not a pdf", "text/plain"),
        ("fake.pdf", b"MZ\x90\x00 not a pdf at all " * 20, "application/pdf"),
        ("broken.pdf", b"%PDF-1.4\n" + b"\x00garbage" * 40, "application/pdf"),
        ("batch.zip", b"PK\x03\x04" + b"\x00" * 60, "application/zip"),
    ],
)
@pytest.mark.asyncio
async def test_upload_non_pdf_is_4xx_and_stores_nothing(
    spies: _Spies, name: str, payload: bytes, mime: str
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_store()
    app = _build_app(_mock_conn(_split_row()), arq=arq, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(),
            files=[("files", (name, payload, mime))],
            data={"consent_acknowledged": "true"},
        )
    assert 400 <= resp.status_code < 500
    spies.create.assert_not_awaited()
    store.put.assert_not_called()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_over_file_count_cap_is_4xx_and_stores_nothing(
    spies: _Spies, small_caps: None
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(_split_row()), arq=arq)
    parts = [
        _pdf_part(f"e{i}.pdf", _export(2) + b"\n%" + str(i).encode()) for i in range(3)
    ]
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(), files=parts, data={"consent_acknowledged": "true"}
        )
    assert 400 <= resp.status_code < 500
    spies.create.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_over_page_cap_is_4xx_and_stores_nothing(
    spies: _Spies, small_caps: None
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(_split_row()), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(),
            files=[_pdf_part(data=_export(4))],  # cap is 3 pages
            data={"consent_acknowledged": "true"},
        )
    assert 400 <= resp.status_code < 500
    spies.create.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_over_file_size_cap_is_4xx_and_stores_nothing(
    spies: _Spies, small_caps: None
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_store()
    app = _build_app(_mock_conn(_split_row()), arq=arq, store=store)
    big = _export(2) + b"\n%" + b"x" * (1024 * 1024 + 10)  # cap is 1 MiB
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(),
            files=[_pdf_part(data=big)],
            data={"consent_acknowledged": "true"},
        )
    assert 400 <= resp.status_code < 500
    spies.create.assert_not_awaited()
    store.put.assert_not_called()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_without_consent_is_4xx_and_stores_nothing(
    spies: _Spies,
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(_split_row()), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(), files=[_pdf_part()], data={"consent_acknowledged": "false"}
        )
    assert 400 <= resp.status_code < 500
    spies.create.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


# ── GET ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_unknown_split_is_404(spies: _Spies) -> None:
    app = _build_app(_mock_conn(None))
    async with await _client(app) as client:
        resp = await client.get(f"/resume-splits/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_returns_status_version_method_and_applicant_labels(
    spies: _Spies,
) -> None:
    app = _build_app(_mock_conn(_split_row(version=4)))
    async with await _client(app) as client:
        resp = await client.get(f"/resume-splits/{uuid4()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    assert body["proposal_version"] == 4
    assert body["method"] == "heuristic"
    assert body["fallback_reason"] == "llm_unavailable"
    assert "Applicant 1" in resp.text
    assert body["proposal"]["applicants"][0]["resume_pages"] == [1]
    assert body["proposal"]["applicants"][0]["cover_letter_pages"] == [2]
    assert set(body["diagnostics"]) >= {
        "missing",
        "duplicated",
        "merged",
        "cover_only",
        "low_text",
    }


# ── correct / accept / discard: concurrency + state gates ────────────────────


@pytest.mark.asyncio
async def test_correct_with_stale_version_is_409_and_writes_nothing(
    spies: _Spies,
) -> None:
    app = _build_app(_mock_conn(_split_row(version=3)))
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{uuid4()}/correct",
            json={
                "proposal_version": 2,
                "applicants": [{"resume_pages": [1, 2, 3], "cover_letter_pages": []}],
                "ignored_pages": [],
            },
        )
    assert resp.status_code == 409
    spies.store_proposal.assert_not_awaited()


@pytest.mark.parametrize("status", ["queued", "segmenting", "accepted", "discarded"])
@pytest.mark.asyncio
async def test_accept_when_not_proposed_is_409_and_ingests_nothing(
    spies: _Spies, status: str
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(_split_row(status=status)), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{uuid4()}/accept", json={"proposal_version": 1}
        )
    if status == "accepted":
        # An accepted split replays its stored result: still no second ingest.
        assert resp.status_code == 200
    else:
        assert resp.status_code == 409
    spies.ingest.assert_not_awaited()
    spies.mark_accepted.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_accept_with_stale_version_is_409_and_ingests_nothing(
    spies: _Spies,
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_store()
    app = _build_app(_mock_conn(_split_row(version=5)), arq=arq, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{uuid4()}/accept", json={"proposal_version": 4}
        )
    assert resp.status_code == 409
    spies.ingest.assert_not_awaited()
    spies.mark_accepted.assert_not_awaited()
    store.delete.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_accept_replays_stored_result_without_reingest(
    spies: _Spies,
) -> None:
    stored = [
        {
            "original_filename": "applicant-001_resume.pdf",
            "outcome": "accepted",
            "resume_id": str(uuid4()),
        }
    ]
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(_split_row(status="accepted", ingest=stored)), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{uuid4()}/accept", json={"proposal_version": 1}
        )
    assert resp.status_code == 200
    assert "applicant-001_resume.pdf" in resp.text
    spies.ingest.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_discard_of_terminal_split_is_409(spies: _Spies) -> None:
    spies.discard.return_value = False
    app = _build_app(_mock_conn(_split_row(status="accepted")))
    async with await _client(app) as client:
        resp = await client.post(f"/resume-splits/{uuid4()}/discard")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_discard_of_proposed_split_succeeds_and_is_audited(
    spies: _Spies,
) -> None:
    app = _build_app(_mock_conn(_split_row(status="proposed")))
    async with await _client(app) as client:
        resp = await client.post(f"/resume-splits/{uuid4()}/discard")
    assert 200 <= resp.status_code < 300
    spies.discard.assert_awaited_once()
    assert spies.audit.await_args is not None
    assert spies.audit.await_args.kwargs["action"] == "resume_split_discard"


# ── authorization: every route, both gates ───────────────────────────────────


def _requests(split: UUID, job: UUID) -> list[tuple[str, str, dict[str, Any]]]:
    pdf = {"files": [_pdf_part()], "data": {"consent_acknowledged": "true"}}
    return [
        ("POST", _upload_url(job), pdf),
        ("GET", f"/resume-splits/{split}", {}),
        ("POST", f"/resume-splits/{split}/correct", {"json": {"proposal_version": 1}}),
        ("POST", f"/resume-splits/{split}/accept", {"json": {"proposal_version": 1}}),
        ("POST", f"/resume-splits/{split}/discard", {}),
    ]


@pytest.mark.parametrize("idx", range(5))
@pytest.mark.parametrize("session_role", ["hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_non_writer_session_is_403_on_every_route_and_writes_nothing(
    spies: _Spies, idx: int, session_role: str
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_store()
    conn = _mock_conn(_split_row())
    app = _build_app(conn, arq=arq, store=store, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)
    method, url, kwargs = _requests(uuid4(), uuid4())[idx]
    async with await _client(app) as client:
        resp = await client.request(method, url, **kwargs)
    assert resp.status_code == 403
    for spy in spies.writes():
        spy.assert_not_awaited()
    store.put.assert_not_called()
    store.delete.assert_not_called()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.parametrize("idx", range(5))
@pytest.mark.parametrize("key_role", [Role.HIRING_MANAGER, Role.AUDITOR])
@pytest.mark.asyncio
async def test_non_writer_api_key_is_403_on_every_route(
    spies: _Spies, idx: int, key_role: Role
) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(_split_row()), arq=arq, key_role=key_role)
    method, url, kwargs = _requests(uuid4(), uuid4())[idx]
    async with await _client(app) as client:
        resp = await client.request(method, url, **kwargs)
    assert resp.status_code == 403
    for spy in spies.writes():
        spy.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.parametrize("session_role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_writer_session_passes_the_gate_on_upload(
    spies: _Spies, session_role: str
) -> None:
    app = _build_app(_mock_conn(_split_row()), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(), files=[_pdf_part()], data={"consent_acknowledged": "true"}
        )
    assert resp.status_code == 202
