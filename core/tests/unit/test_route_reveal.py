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

**FU-5 slice 7 (ADR-019 §8.3/§9.2).** The actor recorded on ``reveal_audit``
now comes from ``src.api.deps.resolve_user`` — never the retired
``X-Actor-Name`` header/``resolve_actor``. See the dedicated section near
the end of this file for the identity-source coverage; the human-only 403
gate on a non-resolving identity is out of scope for this slice (slice 8).

**FU-4 (RBAC) — the MOST EXPLICIT coverage in the whole PR, per the locked
decisions doc.** ``POST /resumes/{id}/reveal`` is admin/recruiter ONLY —
narrower than every read route on this router, and per D2, the auditor role
explicitly gets the SAME blind reads as hiring-manager but may NEVER reveal
("Auditor may read jobs / résumés / shortlists in blind form; may NOT
reveal. Revisit when a ``reveal_audit`` viewing endpoint exists."). Every
disallowed-role test below asserts BOTH the 403 AND that no audit row was
written and the résumé was never decrypted — a role check that runs AFTER
the audit/decrypt would still 403 the HTTP response while having already
performed the very de-anonymization it exists to prevent.
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

from src.api.deps import Role, resolve_role, resolve_user
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.pool import get_db
from src.schemas.auth import User
from src.schemas.resumes import CandidateInfo, ResumeOut

_NOW = dt.datetime(2026, 7, 18, tzinfo=dt.UTC)

# Every role NOT permitted to reveal — includes AUDITOR (D2: same blind reads
# as hiring-manager, but never a reveal) and HIRING_MANAGER.
_NON_REVEALER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)


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


def _build_app(conn: MagicMock, *, role: Role = Role.ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[resolve_role] = lambda: role

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


# ── FU-5 slice 7 (ADR-019 §8.3/§9.2): actor sourced from resolve_user, NOT
#    the deleted X-Actor-Name header ──────────────────────────────────────
#
# REVERSES the deleted ``test_reveal_actor_comes_from_x_actor_name_header``
# (it asserted the retired header populated the audit actor — ADR-019 §8.3
# removes ``X-Actor-Name``/``resolve_actor`` entirely). The reveal route
# still writes ``reveal_audit`` in this slice (the audit_log cutover is
# slice 8) but its actor now comes from ``src.api.deps.resolve_user``: the
# resolved user's ``cas_username`` when a user/dev-anonymous identity
# resolves, and the existing ``"api"`` fallback string when no identity
# resolves at all (NOT a 403 — the human-only reveal gate is slice 8).


def _identity_user(*, cas_username: str = "alice", role: str = "recruiter") -> User:
    return User(
        id=uuid4(),
        cas_username=cas_username,
        display_name=cas_username,
        email=None,
        role=role,
        active=True,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


@pytest.mark.asyncio
async def test_reveal_ignores_x_actor_name_header_and_uses_dev_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CAS disabled is this test suite's ambient default — no
    ``resolve_user`` override is installed, so the REAL dependency resolves
    the synthetic dev-anonymous identity. A leftover ``X-Actor-Name`` header
    must be read nowhere: the recorded actor is ``"dev-anonymous"``, never
    the header's value."""
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
    assert record.await_args.kwargs["actor"] == "dev-anonymous"


@pytest.mark.asyncio
async def test_reveal_actor_is_the_resolved_users_cas_username(
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
    app.dependency_overrides[resolve_user] = lambda: _identity_user(
        cas_username="priya"
    )
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 200
    assert record.await_args.kwargs["actor"] == "priya"


@pytest.mark.asyncio
async def test_reveal_actor_falls_back_to_api_when_no_user_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-019 §9.2/§10b: a bare service-key caller (``resolve_user``
    resolves to ``None``, e.g. CAS enabled with no session) keeps the
    EXISTING ``"api"`` fallback — this slice does NOT add the human-only
    403 gate (that is slice 8), so reveal must still succeed."""
    resume_id = uuid4()
    record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", record)
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_unblinded(resume_id)),
    )

    app = _build_app(_mock_conn(exists=True))
    app.dependency_overrides[resolve_user] = lambda: None
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 200
    assert record.await_args.kwargs["actor"] == "api"


# ── FU-4 (RBAC): admin/recruiter ONLY — the MOST EXPLICIT coverage ──────


@pytest.mark.asyncio
async def test_reveal_as_admin_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.reveal_service, "record_reveal", AsyncMock(return_value=uuid4())
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_unblinded(resume_id)),
    )
    app = _build_app(_mock_conn(exists=True), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_reveal_as_recruiter_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.reveal_service, "record_reveal", AsyncMock(return_value=uuid4())
    )
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_unblinded(resume_id)),
    )
    app = _build_app(_mock_conn(exists=True), role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")
    assert resp.status_code == 200


@pytest.mark.parametrize("role", _NON_REVEALER_ROLES)
@pytest.mark.asyncio
async def test_reveal_403s_for_hiring_manager_and_auditor(
    role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    record = AsyncMock(return_value=uuid4())
    get_one = AsyncMock(return_value=_unblinded(resume_id))
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", record)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(exists=True), role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 403


@pytest.mark.parametrize("role", _NON_REVEALER_ROLES)
@pytest.mark.asyncio
async def test_reveal_403_writes_no_audit_row_for_disallowed_roles(
    role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 403 must happen BEFORE the existence probe / audit write / decrypt
    — a disallowed caller's attempt must leave NO trace of an attempted
    reveal in the audit log and must never decrypt the résumé, even
    transiently. This is the load-bearing test in this file: a role check
    wired in AFTER ``reveal_service.record_reveal`` would still return 403
    to the caller while having already performed the de-anonymization the
    check exists to prevent."""
    resume_id = uuid4()
    record = AsyncMock(return_value=uuid4())
    get_one = AsyncMock(return_value=_unblinded(resume_id))
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", record)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(exists=True), role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 403
    record.assert_not_awaited()
    get_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_reveal_403_for_auditor_even_though_auditor_can_read_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2, pinned explicitly: the auditor role is allowed the SAME blind
    reads as hiring-manager (``GET /resumes/{id}`` succeeds for auditor —
    see ``test_route_resumes.py::test_get_resume_is_readable_by_every_role``)
    but must NEVER be able to reveal. This test exists specifically so a
    future change that accidentally widens the reveal allowed-set to match
    the read allowed-set (an easy copy-paste mistake given every OTHER
    résumé route on this router is open to all four roles) is caught here."""
    resume_id = uuid4()
    record = AsyncMock(return_value=uuid4())
    get_one = AsyncMock(return_value=_unblinded(resume_id))
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", record)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(exists=True), role=Role.AUDITOR)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 403
