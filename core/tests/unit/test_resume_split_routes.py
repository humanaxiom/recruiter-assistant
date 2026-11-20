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


# ═════════════════════════════════════════════════════════════════════════════
# C+D review fixes (RED). Each block below pins one numbered finding.
# ═════════════════════════════════════════════════════════════════════════════

_SECRET = "SECRET-leak-xyz-Wibblesworth"


@pytest.fixture
def env_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Lets a test set SPLIT_* env vars; settings cache cleared both sides."""
    get_settings.cache_clear()
    try:
        yield monkeypatch
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _patch_correction(mp: pytest.MonkeyPatch, version: int = 2) -> AsyncMock:
    spy = AsyncMock(return_value=version)
    mp.setattr("src.services.split_request_service.store_correction", spy)
    return spy


def _two_applicant_proposal() -> SplitProposal:
    return SplitProposal(
        applicants=[
            ProposalApplicant(resume_pages=[1]),
            ProposalApplicant(resume_pages=[2]),
        ]
    )


def _call_body(kind: str) -> tuple[str, dict[str, Any]]:
    """URL suffix + JSON body for a CLEAN two-applicant, two-page export."""
    if kind == "correct":
        return "correct", {
            "proposal_version": 1,
            "applicants": [{"resume_pages": [1]}, {"resume_pages": [2]}],
            "ignored_pages": [],
        }
    return "accept", {"proposal_version": 1}


async def _post_kind(app: FastAPI, kind: str) -> Any:
    suffix, body = _call_body(kind)
    async with await _client(app) as client:
        return await client.post(f"/resume-splits/{uuid4()}/{suffix}", json=body)


def _arrange_clean(spies: _Spies, mp: pytest.MonkeyPatch) -> AsyncMock:
    spies.load_proposal.return_value = _two_applicant_proposal()
    return _patch_correction(mp)


# ── 1. cover-page hijack: accept is blocked ──────────────────────────────────


def _a_resume() -> str:
    return f"Applicant A\na@example.test\n604-555-0100\n{_PAD}"


def _b_resume(tag: str) -> str:
    return f"Applicant B {tag}\nb@example.test\n604-555-0101\n{_PAD}"


def _genuine_cover() -> str:
    return (
        "a@example.test\nDear Hiring Manager,\nI am writing to apply.\n"
        f"{_PAD}\nSincerely,\nA"
    )


@pytest.mark.asyncio
async def test_accept_blocks_a_foreign_resume_page_filed_as_a_cover_letter(
    spies: _Spies,
) -> None:
    spies.load_proposal.return_value = SplitProposal(
        applicants=[
            ProposalApplicant(resume_pages=[1], cover_letter_pages=[2]),
            ProposalApplicant(resume_pages=[3]),
        ]
    )
    store = _mock_store(_pdf([_a_resume(), _b_resume("p1"), _b_resume("p2")]))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(_split_row()), arq=arq, store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{uuid4()}/accept", json={"proposal_version": 1}
        )
    assert resp.status_code == 409
    spies.ingest.assert_not_awaited()
    spies.mark_accepted.assert_not_awaited()
    store.delete.assert_not_awaited()
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_accept_allows_a_genuine_cover_letter_that_carries_an_email(
    spies: _Spies,
) -> None:
    spies.load_proposal.return_value = SplitProposal(
        applicants=[
            ProposalApplicant(resume_pages=[1], cover_letter_pages=[2]),
            ProposalApplicant(resume_pages=[3]),
        ]
    )
    store = _mock_store(_pdf([_a_resume(), _genuine_cover(), _b_resume("p1")]))
    app = _build_app(_mock_conn(_split_row()), store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{uuid4()}/accept", json={"proposal_version": 1}
        )
    assert resp.status_code == 200
    spies.ingest.assert_awaited_once()


@pytest.mark.asyncio
async def test_correct_reports_misfiled_cover_in_the_diagnostics(
    spies: _Spies, env_settings: pytest.MonkeyPatch
) -> None:
    store_correction = _patch_correction(env_settings)
    store = _mock_store(_pdf([_a_resume(), _b_resume("p1"), _b_resume("p2")]))
    app = _build_app(_mock_conn(_split_row()), store=store)
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{uuid4()}/correct",
            json={
                "proposal_version": 1,
                "applicants": [
                    {"resume_pages": [1], "cover_letter_pages": [2]},
                    {"resume_pages": [3]},
                ],
                "ignored_pages": [],
            },
        )
    assert resp.status_code == 200
    assert resp.json()["diagnostics"]["misfiled_cover"] == [2]
    stored = store_correction.await_args.kwargs["diagnostics"]
    assert stored.misfiled_cover == [2]


# ── 2. hostile PDF bounds: extract BEFORE the lock, bounded in size + time ───


def _ordered_app(
    mp: pytest.MonkeyPatch, events: list[str]
) -> tuple[FastAPI, MagicMock]:
    from src.services import taleo_split_service

    real_extract = taleo_split_service.extract_page_texts

    def _extract(pdf: bytes) -> list[str]:
        events.append("extract")
        return real_extract(pdf)

    mp.setattr(taleo_split_service, "extract_page_texts", _extract)
    row = _split_row()
    conn = _mock_conn(row)

    async def _fetchrow(sql: str, *_a: Any) -> _Row:
        events.append("lock" if "FOR UPDATE" in sql else "select")
        return row

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)

    async def _get(_key: str) -> bytes:
        events.append("blob_get")
        return _export(2)

    store = _mock_store()
    store.get = AsyncMock(side_effect=_get)
    return _build_app(conn, store=store), store


@pytest.mark.parametrize("kind", ["correct", "accept"])
@pytest.mark.asyncio
async def test_blob_read_and_text_extraction_happen_before_the_row_lock(
    spies: _Spies, env_settings: pytest.MonkeyPatch, kind: str
) -> None:
    _arrange_clean(spies, env_settings)
    events: list[str] = []
    app, _store = _ordered_app(env_settings, events)
    resp = await _post_kind(app, kind)
    assert resp.status_code == 200, resp.text
    assert "lock" in events and "extract" in events and "blob_get" in events
    assert events.index("blob_get") < events.index("extract") < events.index("lock")


def test_extraction_bound_settings_have_the_planned_defaults() -> None:
    from src.settings import Settings

    s = Settings()
    assert s.split_max_text_chars == 2_000_000
    assert s.split_extract_timeout_s == 120


@pytest.mark.parametrize("kind", ["correct", "accept"])
@pytest.mark.asyncio
async def test_extracted_text_over_the_char_cap_is_422_and_ingests_nothing(
    spies: _Spies, env_settings: pytest.MonkeyPatch, kind: str
) -> None:
    env_settings.setenv("SPLIT_MAX_TEXT_CHARS", "100")
    get_settings.cache_clear()
    correction = _arrange_clean(spies, env_settings)
    store = _mock_store(_export(2))  # ~700 chars of text > 100
    app = _build_app(_mock_conn(_split_row()), store=store)
    resp = await _post_kind(app, kind)
    assert resp.status_code == 422
    assert "applicant0@example.test" not in resp.text
    assert "Experience in recruiting" not in resp.text
    spies.ingest.assert_not_awaited()
    spies.mark_accepted.assert_not_awaited()
    correction.assert_not_awaited()
    store.delete.assert_not_awaited()


@pytest.mark.parametrize("kind", ["correct", "accept"])
@pytest.mark.asyncio
async def test_extracted_text_under_the_char_cap_is_not_rejected(
    spies: _Spies, env_settings: pytest.MonkeyPatch, kind: str
) -> None:
    env_settings.setenv("SPLIT_MAX_TEXT_CHARS", "100000")
    get_settings.cache_clear()
    _arrange_clean(spies, env_settings)
    app = _build_app(_mock_conn(_split_row()), store=_mock_store(_export(2)))
    resp = await _post_kind(app, kind)
    assert resp.status_code == 200


@pytest.mark.parametrize("kind", ["correct", "accept"])
@pytest.mark.asyncio
async def test_a_slow_extraction_times_out_as_422_instead_of_hanging(
    spies: _Spies, env_settings: pytest.MonkeyPatch, kind: str
) -> None:
    import time

    from src.services import taleo_split_service

    env_settings.setenv("SPLIT_EXTRACT_TIMEOUT_S", "0.3")
    get_settings.cache_clear()
    correction = _arrange_clean(spies, env_settings)

    def _slow(_pdf: bytes) -> list[str]:
        time.sleep(1.5)
        return ["x", "y"]

    env_settings.setattr(taleo_split_service, "extract_page_texts", _slow)
    app = _build_app(_mock_conn(_split_row()), store=_mock_store(_export(2)))
    started = time.monotonic()
    resp = await _post_kind(app, kind)
    assert time.monotonic() - started < 1.2
    assert resp.status_code == 422
    spies.ingest.assert_not_awaited()
    spies.mark_accepted.assert_not_awaited()
    correction.assert_not_awaited()


# ── 3. _CorrectBody lists are bounded by split_max_pages ─────────────────────


@pytest.fixture
def three_page_cap(env_settings: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    env_settings.setenv("SPLIT_MAX_PAGES", "3")
    get_settings.cache_clear()
    return env_settings


def _correct_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "proposal_version": 1,
        "applicants": [{"resume_pages": [1]}],
        "ignored_pages": [],
    }
    body.update(over)
    return body


_TOO_LONG: list[tuple[str, dict[str, Any]]] = [
    (
        "applicants",
        _correct_body(applicants=[{"resume_pages": [1]} for _ in range(4)]),
    ),
    ("resume_pages", _correct_body(applicants=[{"resume_pages": [1, 1, 1, 1]}])),
    (
        "cover_letter_pages",
        _correct_body(
            applicants=[{"resume_pages": [1], "cover_letter_pages": [2, 2, 2, 2]}]
        ),
    ),
    ("ignored_pages", _correct_body(ignored_pages=[3, 3, 3, 3])),
]

_AT_CAP: list[tuple[str, dict[str, Any]]] = [
    (
        "applicants",
        _correct_body(applicants=[{"resume_pages": [i]} for i in (1, 2, 3)]),
    ),
    ("resume_pages", _correct_body(applicants=[{"resume_pages": [1, 2, 3]}])),
    (
        "cover_letter_pages",
        _correct_body(
            applicants=[{"resume_pages": [1], "cover_letter_pages": [2, 3, 3]}]
        ),
    ),
    ("ignored_pages", _correct_body(ignored_pages=[3, 3, 3])),
]


@pytest.mark.parametrize(("field", "body"), _TOO_LONG, ids=[f for f, _ in _TOO_LONG])
@pytest.mark.asyncio
async def test_correct_rejects_a_list_longer_than_the_page_cap(
    spies: _Spies, three_page_cap: pytest.MonkeyPatch, field: str, body: dict[str, Any]
) -> None:
    correction = _patch_correction(three_page_cap)
    app = _build_app(_mock_conn(_split_row()), store=_mock_store(_export(3)))
    async with await _client(app) as client:
        resp = await client.post(f"/resume-splits/{uuid4()}/correct", json=body)
    assert resp.status_code == 422, field
    correction.assert_not_awaited()


@pytest.mark.parametrize(("field", "body"), _AT_CAP, ids=[f for f, _ in _AT_CAP])
@pytest.mark.asyncio
async def test_correct_accepts_a_list_exactly_at_the_page_cap(
    spies: _Spies, three_page_cap: pytest.MonkeyPatch, field: str, body: dict[str, Any]
) -> None:
    correction = _patch_correction(three_page_cap)
    app = _build_app(_mock_conn(_split_row()), store=_mock_store(_export(3)))
    async with await _client(app) as client:
        resp = await client.post(f"/resume-splits/{uuid4()}/correct", json=body)
    assert resp.status_code == 200, (field, resp.text)
    correction.assert_awaited_once()


# ── 4. unreadable source -> fixed 409, class-name-only logging ───────────────


class _PdfBoomError(Exception):
    pass


@pytest.mark.parametrize("case", ["blob_missing", "parse_error"])
@pytest.mark.parametrize("kind", ["correct", "accept"])
@pytest.mark.asyncio
async def test_unavailable_source_is_a_fixed_409_not_a_500(
    spies: _Spies,
    env_settings: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    case: str,
) -> None:
    import logging

    from src.services import taleo_split_service
    from src.storage.blob_store import BlobNotFound

    caplog.set_level(logging.DEBUG)
    correction = _arrange_clean(spies, env_settings)
    store = _mock_store()
    if case == "blob_missing":
        store.get = AsyncMock(side_effect=BlobNotFound(f"split_sources/{_SECRET}"))
    else:

        def _boom(_pdf: bytes) -> list[str]:
            raise _PdfBoomError(f"bad xref near {_SECRET}")

        env_settings.setattr(taleo_split_service, "extract_page_texts", _boom)
    app = _build_app(_mock_conn(_split_row()), store=store)
    resp = await _post_kind(app, kind)
    assert resp.status_code == 409
    assert "source export unavailable" in resp.text.lower()
    assert _SECRET not in resp.text
    spies.ingest.assert_not_awaited()
    spies.mark_accepted.assert_not_awaited()
    correction.assert_not_awaited()
    haystack = caplog.text + " ".join(
        str(v) for r in caplog.records for v in r.__dict__.values()
    )
    assert _SECRET not in haystack
    assert ("BlobNotFound" if case == "blob_missing" else "_PdfBoomError") in haystack


# ── 9. multi-file upload: enqueue each id right after its own row ────────────


@pytest.mark.asyncio
async def test_upload_enqueues_each_file_right_after_its_own_row_is_created(
    spies: _Spies,
) -> None:
    ids = [uuid4(), uuid4()]
    order: list[str] = []
    created: list[UUID] = []

    async def _create(*_a: Any, **_k: Any) -> UUID:
        sid = ids[len(created)]
        created.append(sid)
        order.append(f"create{len(created)}")
        return sid

    async def _enqueue(_fn: str, sid: str, *_a: Any, **_k: Any) -> None:
        order.append(f"enqueue{ids.index(UUID(sid)) + 1}")

    spies.create.side_effect = _create
    arq = MagicMock(enqueue_job=AsyncMock(side_effect=_enqueue))
    app = _build_app(_mock_conn(_split_row()), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(),
            files=[_pdf_part("a.pdf", _export(2)), _pdf_part("b.pdf", _export(3))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    assert order == ["create1", "enqueue1", "create2", "enqueue2"]


@pytest.mark.asyncio
async def test_upload_failure_on_file_two_leaves_file_one_enqueued_and_errors(
    spies: _Spies,
) -> None:
    from src.errors import ConflictError

    first = uuid4()
    calls = {"n": 0}

    async def _create(*_a: Any, **_k: Any) -> UUID:
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConflictError("second create failed")
        return first

    spies.create.side_effect = _create
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(_mock_conn(_split_row()), arq=arq)
    async with await _client(app) as client:
        resp = await client.post(
            _upload_url(),
            files=[_pdf_part("a.pdf", _export(2)), _pdf_part("b.pdf", _export(3))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code >= 400
    arq.enqueue_job.assert_awaited_once_with("segment_taleo_export", str(first))


# ═════════════════════════════════════════════════════════════════════════════
# Slice E5 (RED): GET /jobs/{job_id}/resume-splits -- the pending-split listing.
# The row/SQL behaviour is pinned against real Postgres in
# tests/integration/test_resume_split_routes_pg.py; these pin the gates.
# ═════════════════════════════════════════════════════════════════════════════


def _list_url(job_id: UUID | None = None) -> str:
    return f"/jobs/{job_id or uuid4()}/resume-splits"


@pytest.mark.parametrize("session_role", ["hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_listing_non_writer_session_is_403(
    spies: _Spies, session_role: str
) -> None:
    conn = _mock_conn(_split_row())
    app = _build_app(conn, key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)
    async with await _client(app) as client:
        resp = await client.get(_list_url())
    assert resp.status_code == 403
    conn.fetch.assert_not_awaited()


@pytest.mark.parametrize("key_role", [Role.HIRING_MANAGER, Role.AUDITOR])
@pytest.mark.asyncio
async def test_listing_non_writer_api_key_is_403(spies: _Spies, key_role: Role) -> None:
    conn = _mock_conn(_split_row())
    app = _build_app(conn, key_role=key_role)
    async with await _client(app) as client:
        resp = await client.get(_list_url())
    assert resp.status_code == 403
    conn.fetch.assert_not_awaited()


@pytest.mark.parametrize("session_role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_listing_writer_session_gets_a_json_list(
    spies: _Spies, session_role: str
) -> None:
    app = _build_app(_mock_conn(_split_row()), key_role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _session_user(role=session_role)
    async with await _client(app) as client:
        resp = await client.get(_list_url())
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_listing_unknown_job_is_404(spies: _Spies) -> None:
    app = _build_app(_mock_conn(None))
    async with await _client(app) as client:
        resp = await client.get(_list_url())
    assert resp.status_code == 404
    assert resp.json()["code"] == "resource.not_found"  # not a missing route


@pytest.mark.asyncio
async def test_listing_is_read_only_and_audits_nothing(spies: _Spies) -> None:
    arq = MagicMock(enqueue_job=AsyncMock())
    store = _mock_store()
    app = _build_app(_mock_conn(_split_row()), arq=arq, store=store)
    async with await _client(app) as client:
        resp = await client.get(_list_url())
    assert resp.status_code == 200
    for spy in spies.writes():
        spy.assert_not_awaited()
    store.put.assert_not_called()
    store.delete.assert_not_called()
    arq.enqueue_job.assert_not_awaited()
