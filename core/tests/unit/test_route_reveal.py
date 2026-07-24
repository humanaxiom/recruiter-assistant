"""Route tests for ``POST /resumes/{id}/reveal`` on ``src.api.routes.resumes``.

Revealing a candidate is the de-anonymization action, so the route must:
* probe existence FIRST — a missing id 404s and writes NO audit row and never
  decrypts;
* on an existing id, record exactly one audit row, THEN return the
  UN-blinded ``ResumeOut`` (via ``resume_service.get_one(..., reveal=True)``).

``record_audit``/``get_one`` are monkeypatched so this isolates the ROUTE
contract (order, audit-before-return, 404-before-audit, human-attribution
gate); their own behaviour is covered by ``test_audit_service.py`` /
``test_services_reveal.py`` and the résumé-service tests.

**FU-5 slice 8 (ADR-019 §6/§7/§10b) — the reveal -> audit_log cutover, plus
the human-attribution gate.** This SUPERSEDES slice 7's behaviour pinned
below in three ways, per ADR-019:

1. The route no longer writes ``reveal_audit`` (via
   ``reveal_service.record_reveal``) at all on the reveal path. It writes
   exactly one ``audit_log`` row via the NEW ``src.services.audit_service
   .record_audit`` — see the dedicated section below. ``reveal_audit`` and
   ``reveal_service`` are KEPT (read-only, ADR-019 §6) — every test that
   still patches ``reveal_service.record_reveal`` now asserts it is NEVER
   awaited, proving the cutover rather than the old write.
2. A caller for whom NO human identity resolves (``resolve_user`` -> ``None``,
   e.g. a bare service-key caller with CAS enabled) now gets **403**, not the
   old ``"api"``-fallback 200. This REVERSES
   ``test_reveal_actor_falls_back_to_api_when_no_user_resolves`` from slice 7,
   whose own docstring said the human-only gate was "slice 8" scope — it now
   is. See ``test_reveal_case2_no_human_session_403s_writes_no_audit_no_decrypt``.
3. CAS-disabled dev mode (ADR-019 §10b) SKIPS the human gate — the synthetic
   dev-anonymous identity resolves and the audit row is written with
   ``actor_kind='service'`` / ``actor_service='dev-anonymous'`` (never
   ``actor_kind='user'`` against the sentinel id, which is not a real
   ``users`` row and would violate the ``audit_log.actor_user_id`` FK).

**FU-4 (RBAC) — the MOST EXPLICIT coverage in the whole PR, per the locked
decisions doc.** ``POST /resumes/{id}/reveal`` is admin/recruiter ONLY —
narrower than every read route on this router, and per D2, the auditor role
explicitly gets the SAME blind reads as hiring-manager but may NEVER reveal
("Auditor may read jobs / résumés / shortlists in blind form; may NOT
reveal."). Every disallowed-role test below asserts BOTH the 403 AND that no
audit row was written and the résumé was never decrypted — a role check that
runs AFTER the audit/decrypt would still 403 the HTTP response while having
already performed the very de-anonymization it exists to prevent.

**FU-6 slice 6 (ADR-020 §3/§5) — row-scoping for a hiring_manager SESSION.**
``_REVEALERS`` (the API-KEY role gate) stays admin/recruiter ONLY — a raw
``hiring_manager`` KEY is still rejected by ``require_role`` before this
route body ever runs (see ``test_reveal_403s_for_hiring_manager_and_auditor``
above). The scoping this slice adds targets a DIFFERENT, real scenario ADR-020
§3/§4 documents explicitly: the Flask viewer presents the ONE shared
``recruiter`` API key for every signed-in human, so a hiring_manager browsing
through that shared key satisfies ``require_role`` as ``Role.RECRUITER`` —
only the CAS SESSION identity (``resolve_user``'s resolved ``user.role ==
"hiring_manager"``) can still scope such a caller to their own assigned
jobs. Per the task's explicit ordering: human-gate -> scoping-404 -> audit ->
decrypt. An unassigned (or nonexistent) résumé must 404 BEFORE any audit_log
row is written and BEFORE any decryption — observationally identical to a
genuinely nonexistent résumé (ADR-020 §5), so NO NEW distinguishable status
code is introduced; only the WIRING (whether the scoping check blocks the
audit/decrypt at all) is proven here. The real ``EXISTS``-against-
``job_assignees`` filtering is ``test_resume_scoping_pg.py``'s job (real
Postgres, ADR-020 §3, mandatory).
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


def _mock_conn_scoped(*, assigned: bool, resume_id: UUID) -> MagicMock:
    """A mocked conn whose ``fetchval`` mimics the two distinct existence
    probes a scoped route must be able to issue: an UNSCOPED probe (bare
    ``resume_id`` only, one positional bind arg) always finds the row, while
    a SCOPED probe (``resume_id`` PLUS the caller's ``user_id``, two-or-more
    positional bind args) returns ``None`` whenever ``assigned=False`` —
    mirroring the ``EXISTS (... job_assignees ...)`` predicate ADR-020 §3
    mandates without hard-coding its exact SQL text (that structural proof
    against a real query planner belongs to ``test_resume_scoping_pg.py``).

    Against the PRE-slice-6 route (which always issues a single-arg,
    unscoped existence probe regardless of session), this always resolves
    truthy — which is exactly the RED signal for the ``assigned=False``
    tests below: they must see 404, but an unscoped route would 200.
    """

    async def _fetchval(_query: str, *args: Any) -> Any:
        if len(args) >= 2 and not assigned:
            return None
        return resume_id

    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(side_effect=_fetchval)
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


def _tracking_mocks(resume_id: UUID) -> tuple[AsyncMock, AsyncMock, list[str]]:
    """Two AsyncMocks that each append a marker to a shared list, so the
    relative CALL ORDER between them can be asserted (ADR-016's ordering
    guarantee, restated ADR-019 §7: the audit write happens BEFORE decrypt)."""
    order: list[str] = []

    async def _audit_side_effect(*_args: Any, **_kwargs: Any) -> None:
        order.append("audit")

    async def _get_one_side_effect(*_args: Any, **_kwargs: Any) -> ResumeOut:
        order.append("get_one")
        return _unblinded(resume_id)

    audit = AsyncMock(side_effect=_audit_side_effect)
    get_one = AsyncMock(side_effect=_get_one_side_effect)
    return audit, get_one, order


@pytest.mark.asyncio
async def test_reveal_404_on_missing_resume_writes_no_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    legacy_record = AsyncMock(return_value=uuid4())
    audit = AsyncMock(return_value=None)
    get_one = AsyncMock()
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", legacy_record)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(exists=False))
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 404
    legacy_record.assert_not_awaited()  # no legacy reveal_audit row either
    audit.assert_not_awaited()  # no audit row for a nonexistent id
    get_one.assert_not_awaited()  # never decrypts


@pytest.mark.asyncio
async def test_reveal_records_audit_log_row_then_returns_unblinded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FU-5 slice 8 (ADR-019 §6/§7): the reveal -> audit_log cutover. The
    route writes ONE ``audit_log`` row (via ``audit_service.record_audit``,
    forwarding the caller-supplied ``context``) BEFORE returning the
    un-blinded résumé, and NEVER writes to the retired ``reveal_audit`` sink
    any more."""
    resume_id = uuid4()
    audit = AsyncMock(return_value=None)
    get_one = AsyncMock(return_value=_unblinded(resume_id))
    legacy_record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", legacy_record)

    app = _build_app(_mock_conn(exists=True))
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal?context=shortlist")

    assert resp.status_code == 200
    body = resp.json()
    # Un-blinded: real identity is present (this is the whole point of reveal).
    assert body["candidate"]["name"] == "Casey Rivera"
    assert body["candidate"]["email"] == "casey.rivera@example.test"
    # Audited: exactly one audit_log row, keyed on this résumé, with the
    # supplied context.
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["subject_type"] == "resume"
    assert kwargs["subject_id"] == resume_id
    assert kwargs["action"] == "reveal"
    assert kwargs["context"] == "shortlist"
    # Un-blind is via the audited reveal=True read, not a raw blind read.
    get_one.assert_awaited_once()
    assert get_one.await_args.kwargs.get("reveal") is True
    # The retired reveal_audit writer must never be called any more.
    legacy_record.assert_not_awaited()


# ── FU-5 slice 8 (ADR-019 §7/§10b): the three reveal cases ────────────────
#
# Case 1: cas_enabled=True AND a real human session resolves -> actor_kind
#         'user', audit BEFORE decrypt.
# Case 2: cas_enabled=True AND no human session (resolve_user -> None) -> 403,
#         no decrypt, no audit row at all.
# Case 3: cas_enabled=False (dev mode, this suite's ambient default) -> the
#         human gate is SKIPPED; audit is written as actor_kind='service' /
#         actor_service='dev-anonymous'.


@pytest.mark.asyncio
async def test_reveal_case1_human_session_writes_user_actor_audit_before_decrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    user = _identity_user(cas_username="priya")
    audit, get_one, order = _tracking_mocks(resume_id)
    legacy_record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", legacy_record)

    app = _build_app(_mock_conn(exists=True))
    app.dependency_overrides[resolve_user] = lambda: user
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 200
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "user"
    assert kwargs["actor_user_id"] == user.id
    assert kwargs.get("actor_service") is None
    assert kwargs["action"] == "reveal"
    assert kwargs["subject_type"] == "resume"
    assert kwargs["subject_id"] == resume_id
    # ADR-016's ordering guarantee, restated ADR-019 §7: audit BEFORE decrypt.
    assert order == ["audit", "get_one"]
    legacy_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_reveal_case2_no_human_session_403s_writes_no_audit_no_decrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-019 §7 — REVERSES the deleted slice-7
    ``test_reveal_actor_falls_back_to_api_when_no_user_resolves`` (that
    test's own docstring said the human-only gate was out of its scope; it is
    THIS slice's scope). A service-key-only caller (``resolve_user`` -> None,
    e.g. CAS enabled with no session) must now 403 — no fallback identity,
    no decryption, and no audit row of any kind."""
    resume_id = uuid4()
    audit, get_one, order = _tracking_mocks(resume_id)
    legacy_record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", legacy_record)

    app = _build_app(_mock_conn(exists=True))
    app.dependency_overrides[resolve_user] = lambda: None
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 403
    audit.assert_not_awaited()
    get_one.assert_not_awaited()
    legacy_record.assert_not_awaited()
    assert order == []


@pytest.mark.asyncio
async def test_reveal_case3_cas_disabled_writes_service_actor_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-019 §10b — CAS disabled, no session possible. This test suite's
    ambient default (no ``resolve_user`` override) resolves the REAL
    dependency's synthetic dev-anonymous identity. The human-only gate is
    SKIPPED, and the audit row is written with ``actor_kind='service'`` /
    ``actor_service='dev-anonymous'`` — never ``actor_kind='user'`` against
    the sentinel id (which is not a real ``users`` row and would violate the
    ``audit_log.actor_user_id`` FK)."""
    resume_id = uuid4()
    audit, get_one, order = _tracking_mocks(resume_id)
    legacy_record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", legacy_record)

    app = _build_app(_mock_conn(exists=True))
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 200
    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "service"
    assert kwargs["actor_service"] == "dev-anonymous"
    assert kwargs.get("actor_user_id") is None
    assert order == ["audit", "get_one"]
    legacy_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_reveal_ignores_x_actor_name_header_and_uses_dev_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover ``X-Actor-Name`` header (retired since slice 7) must be
    read nowhere: the audit_log row's ``actor_service`` is
    ``"dev-anonymous"``, never the header's value, and the retired
    ``reveal_audit`` writer is never called."""
    resume_id = uuid4()
    audit = AsyncMock(return_value=None)
    legacy_record = AsyncMock(return_value=uuid4())
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_unblinded(resume_id)),
    )
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", legacy_record)

    app = _build_app(_mock_conn(exists=True))
    async with await _client(app) as client:
        resp = await client.post(
            f"/resumes/{resume_id}/reveal",
            headers={"X-Actor-Name": "alice@example.test"},
        )

    assert resp.status_code == 200
    kwargs = audit.await_args.kwargs
    assert kwargs["actor_kind"] == "service"
    assert kwargs["actor_service"] == "dev-anonymous"
    legacy_record.assert_not_awaited()


@pytest.mark.asyncio
async def test_reveal_route_always_supplies_exactly_one_actor_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Light touch on the ``audit_log_actor_identity`` CHECK contract
    (enforced for real by Postgres — see
    ``tests/integration/test_users_audit_pg.py`` and
    ``tests/integration/test_reveal_audit_log_pg.py``). Whichever reveal case
    fires, the route's call into ``audit_service.record_audit`` must supply
    EXACTLY ONE of ``actor_user_id`` / ``actor_service`` — never both, never
    neither. Exercised back to back for case 3 (service) and case 1 (user)
    against the same mock."""
    resume_id = uuid4()
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(
        resumes_routes.resume_service,
        "get_one",
        AsyncMock(return_value=_unblinded(resume_id)),
    )

    app = _build_app(_mock_conn(exists=True))

    # Case 3: no override -> dev-anonymous synthetic identity -> service actor.
    async with await _client(app) as client:
        await client.post(f"/resumes/{resume_id}/reveal")
    service_kwargs = audit.await_args.kwargs
    assert (service_kwargs.get("actor_user_id") is None) != (
        service_kwargs.get("actor_service") is None
    )

    # Case 1: a real human session -> user actor.
    app.dependency_overrides[resolve_user] = lambda: _identity_user()
    async with await _client(app) as client:
        await client.post(f"/resumes/{resume_id}/reveal")
    user_kwargs = audit.await_args.kwargs
    assert (user_kwargs.get("actor_user_id") is None) != (
        user_kwargs.get("actor_service") is None
    )


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
    reveal in the audit log (the NEW ``audit_log`` sink, as of slice 8, as
    well as the legacy ``reveal_audit`` one) and must never decrypt the
    résumé, even transiently. This is the load-bearing test in this file: a
    role check wired in AFTER the audit write would still return 403 to the
    caller while having already performed the de-anonymization the check
    exists to prevent."""
    resume_id = uuid4()
    legacy_record = AsyncMock(return_value=uuid4())
    audit = AsyncMock(return_value=None)
    get_one = AsyncMock(return_value=_unblinded(resume_id))
    monkeypatch.setattr(resumes_routes.reveal_service, "record_reveal", legacy_record)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    app = _build_app(_mock_conn(exists=True), role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 403
    legacy_record.assert_not_awaited()
    audit.assert_not_awaited()
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


# ── FU-6 slice 6 (ADR-020 §3/§5) — scoped hiring_manager SESSION reveal ────
#
# The RBAC key-role gate (``_REVEALERS = (admin, recruiter)``) is untouched —
# see ``test_reveal_403s_for_hiring_manager_and_auditor`` above for the raw
# hiring_manager-KEY 403, which stays exactly as it was. The scenario below
# is the ADR-020 §3/§4 "shared browser key" trap: the caller's KEY resolves
# to ``Role.RECRUITER`` (so RBAC alone lets the request through, satisfied by
# ``_build_app``'s default ``role=Role.ADMIN``/``Role.RECRUITER``), but the
# CAS SESSION identity is a real hiring_manager — only the row-scoping check
# may still block such a caller, keyed on THEIR OWN assigned jobs.


@pytest.mark.asyncio
async def test_reveal_scoped_hiring_manager_unassigned_404s_writes_no_audit_no_decrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scoped hiring_manager session revealing a résumé under a job they
    are NOT assigned to must 404 — and, critically, must write NO audit_log
    row and never decrypt — even though an UNSCOPED existence probe alone
    would find the résumé (``_mock_conn_scoped(assigned=False, ...)``
    returns truthy for a bare, single-arg probe). Against the pre-slice-6
    route (which never looks at the session for this check), the mocked
    ``fetchval`` only ever receives a single positional arg and therefore
    always resolves truthy — so this test is RED until the route forwards
    the scoped identity into an existence check that can actually block it,
    per the mandated order: human-gate -> scoping-404 -> audit -> decrypt."""
    resume_id = uuid4()
    hm = _identity_user(cas_username="hm", role="hiring_manager")
    audit, get_one, order = _tracking_mocks(resume_id)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    conn = _mock_conn_scoped(assigned=False, resume_id=resume_id)
    app = _build_app(conn, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: hm
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 404
    audit.assert_not_awaited()
    get_one.assert_not_awaited()
    assert order == []


@pytest.mark.asyncio
async def test_reveal_scoped_hiring_manager_assigned_writes_audit_then_decrypts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pairing test to the one above: a scoped hiring_manager session
    revealing a résumé under a job they ARE assigned to must still succeed —
    exactly one audit row, written BEFORE the decrypting read."""
    resume_id = uuid4()
    hm = _identity_user(cas_username="hm", role="hiring_manager")
    audit, get_one, order = _tracking_mocks(resume_id)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    conn = _mock_conn_scoped(assigned=True, resume_id=resume_id)
    app = _build_app(conn, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: hm
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 200
    audit.assert_awaited_once()
    get_one.assert_awaited_once()
    assert order == ["audit", "get_one"]


@pytest.mark.asyncio
async def test_reveal_scoped_hiring_manager_unassigned_403s_before_human_gate_never_applies(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordering pin: the EXISTING human-attribution gate (``user is None`` ->
    403) still runs FIRST — a caller with no session at all must 403
    regardless of scoping, and must never reach the scoping check, the audit
    write, or the decrypt. Reuses the assigned=True mocked conn (so if the
    human gate were accidentally skipped, this would incorrectly 200) to
    prove the gate, not the scoping predicate, is what fires here."""
    resume_id = uuid4()
    audit, get_one, order = _tracking_mocks(resume_id)
    monkeypatch.setattr(resumes_routes.audit_service, "record_audit", audit)
    monkeypatch.setattr(resumes_routes.resume_service, "get_one", get_one)

    conn = _mock_conn_scoped(assigned=True, resume_id=resume_id)
    app = _build_app(conn, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None
    async with await _client(app) as client:
        resp = await client.post(f"/resumes/{resume_id}/reveal")

    assert resp.status_code == 403
    audit.assert_not_awaited()
    get_one.assert_not_awaited()
    assert order == []
