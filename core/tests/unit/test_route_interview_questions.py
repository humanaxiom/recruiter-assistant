"""RED -- interview-question generator, slice S2: the two API routes on the
shortlist router (``POST|GET /shortlist/{entry_id}/interview-questions``).

The service layer is mocked; this file pins WIRING and CONTRACT (roles, status
codes, ordering, audit shape, response shape). That the SQL behind it holds is
``tests/integration/test_interview_questions_flow_pg.py``'s job.

Contract choices made here where the brief left them open:

* POST, hiring_manager / auditor (key OR session) -> **403**, decided by the
  writer gates BEFORE the entry is resolved -- the same convention as
  ``POST /jobs/{id}/shortlist``. Assignment is never consulted for a write.
* POST body on 202: ``{"status": "pending"}``.
* POST audit: ``subject_type="shortlist_entry"``, ``subject_id=entry_id``,
  ``job_id=job_id``; written for EVERY principal (it is a write, not an
  auditor read) and only after the claim succeeded -- a 409 writes nothing.
* GET with no stored row -> **200** ``{"status": "none", ...}``; an unknown,
  unassigned or WITHDRAWN entry -> **404** (``get_one`` already filters
  withdrawn rows, so a withdrawn candidate's questions are unreachable).
* The row is read only AFTER the entry resolved, so a 404 never reads the
  questions table.
* ``stale = stored gaps_sha256 is not None and != current_gaps_sha256`` --
  including when the current gaps can no longer be computed at all.
* Auditor read audit: action ``read_interview_questions``.

The routes must call ``shortlist_service.get_one`` and the new service through
their MODULE attributes (as ``shortlist.py`` already does), which is what the
``monkeypatch.setattr`` calls below patch.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, get_arq, resolve_role, resolve_user
from src.api.routes import shortlist as shortlist_routes
from src.errors import AppError, NotFoundError
from src.models.pool import get_db
from src.schemas.auth import User
from src.schemas.matching import (
    EvidenceObject,
    EvidenceStatus,
    RequirementEvidence,
    ScoreBreakdown,
    ShortlistEntry,
)
from src.services import audit_service, shortlist_service
from src.services.interview_questions import gaps_hash, select_gaps

_NOW = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.UTC)

QUOTE = "Deployed a pod once in a lab"
REQ_PARTIAL = "Kubernetes operations"
REQ_MISSING = "Budget ownership"
QUESTION_TEXT = "Describe a budget you were accountable for."

_ENQUEUE_NAME = "generate_interview_questions"
_RESPONSE_KEYS = {
    "status",
    "reason",
    "questions",
    "gaps_sha256",
    "current_gaps_sha256",
    "stale",
    "generated_at",
}
_QUESTION_KEYS = {"requirement_index", "requirement", "question", "probes_quote"}


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _aw(mock: Any) -> Any:
    assert mock.await_args is not None
    return mock.await_args


def _iq() -> Any:
    return importlib.import_module("src.services.interview_question_service")


def _req(text: str, status: EvidenceStatus, quote: str = "") -> RequirementEvidence:
    return RequirementEvidence.model_validate(
        {"requirement": text, "status": status, "evidence": quote, "confidence": 0.6}
    )


def _entry(
    *,
    entry_id: UUID,
    job_id: UUID | None = None,
    resume_id: UUID | None = None,
    requirements: list[RequirementEvidence] | None = None,
    evaluated: bool | None = True,
) -> ShortlistEntry:
    reqs = (
        requirements
        if requirements is not None
        else [
            _req("5 years of Python", "met", "Built Python services since 2016"),
            _req(REQ_PARTIAL, "partial", QUOTE),
            _req(REQ_MISSING, "missing"),
        ]
    )
    return ShortlistEntry.model_validate(
        {
            "id": entry_id,
            "job_id": job_id or uuid4(),
            "resume_id": resume_id or uuid4(),
            "rank": 1,
            "score_final": 0.7,
            "score_breakdown": ScoreBreakdown(
                skill=0.8,
                experience=0.7,
                education=0.6,
                seniority=0.5,
                vector=0.4,
                structured=0.65,
            ),
            "evidence": EvidenceObject(requirements=reqs),
            "pipeline_meta": None,
            "generated_at": _NOW,
            "evidence_evaluated": evaluated,
            "display_label": "Candidate A",
        }
    )


def _user(*, role: str, cas_username: str = "alice") -> User:
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


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _conn(*, fetchrow: Any = None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _build_app(
    conn: MagicMock, *, arq: MagicMock | None = None, role: Role = Role.ADMIN
) -> FastAPI:
    app = FastAPI()
    app.include_router(shortlist_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _Wiring:
    """Patched collaborators of one POST/GET scenario."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        entry: ShortlistEntry | None,
        claim: bool = True,
    ) -> None:
        self.order: list[str] = []
        self.entry = entry

        async def _get_one(*_a: Any, **_k: Any) -> ShortlistEntry:
            self.order.append("get_one")
            if entry is None:
                raise NotFoundError("shortlist entry not found")
            return entry

        async def _claim(*_a: Any, **_k: Any) -> bool:
            self.order.append("claim")
            return claim

        self.get_one = AsyncMock(side_effect=_get_one)
        self.claim = AsyncMock(side_effect=_claim)
        self.audit = AsyncMock(return_value=None)
        monkeypatch.setattr(shortlist_service, "get_one", self.get_one)
        monkeypatch.setattr(_iq(), "claim", self.claim)
        monkeypatch.setattr(audit_service, "record_audit", self.audit)

        self.arq = MagicMock()

        async def _enqueue(*_a: Any, **_k: Any) -> None:
            self.order.append("enqueue")

        self.arq.enqueue_job = AsyncMock(side_effect=_enqueue)


# ── POST: the happy path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_returns_202_pending_and_enqueues_ids_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id, job_id, resume_id = uuid4(), uuid4(), uuid4()
    w = _Wiring(
        monkeypatch, entry=_entry(entry_id=entry_id, job_id=job_id, resume_id=resume_id)
    )
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 202
    assert resp.json()["status"] == "pending"
    w.arq.enqueue_job.assert_awaited_once_with(
        _ENQUEUE_NAME, str(job_id), str(resume_id)
    )


@pytest.mark.asyncio
async def test_post_sets_pending_before_it_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(role="recruiter")

    async with await _client(app) as client:
        await client.post(f"/shortlist/{entry_id}/interview-questions")

    assert w.order == ["get_one", "claim", "enqueue"]


@pytest.mark.asyncio
async def test_post_claims_for_the_entrys_job_and_resume_as_the_session_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id, job_id, resume_id = uuid4(), uuid4(), uuid4()
    w = _Wiring(
        monkeypatch, entry=_entry(entry_id=entry_id, job_id=job_id, resume_id=resume_id)
    )
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(
        role="recruiter", cas_username="alice"
    )

    async with await _client(app) as client:
        await client.post(f"/shortlist/{entry_id}/interview-questions")

    call = _aw(w.claim)
    assert tuple(call.args[1:4]) == (job_id, resume_id, "alice")


@pytest.mark.asyncio
async def test_post_resolves_the_entry_unscoped_for_a_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(role="recruiter")

    async with await _client(app) as client:
        await client.post(f"/shortlist/{entry_id}/interview-questions")

    call = _aw(w.get_one)
    assert entry_id in call.args
    assert call.kwargs.get("user_id") is None


@pytest.mark.parametrize("session_role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_post_is_open_to_admin_and_recruiter_sessions(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 202


# ── POST: refusals ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_409_when_a_fresh_pending_row_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id), claim=False)
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 409
    w.arq.enqueue_job.assert_not_awaited()
    w.audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_404_for_a_missing_entry_and_nothing_is_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _Wiring(monkeypatch, entry=None)
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{uuid4()}/interview-questions")

    assert resp.status_code == 404
    w.claim.assert_not_awaited()
    w.arq.enqueue_job.assert_not_awaited()
    w.audit.assert_not_awaited()


@pytest.mark.parametrize("key_role", [Role.HIRING_MANAGER, Role.AUDITOR])
@pytest.mark.asyncio
async def test_post_403_for_a_non_writer_api_key(
    key_role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    app = _build_app(_conn(), arq=w.arq, role=key_role)
    app.dependency_overrides[resolve_user] = lambda: _user(role="recruiter")

    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 403
    w.get_one.assert_not_awaited()
    w.claim.assert_not_awaited()
    w.arq.enqueue_job.assert_not_awaited()


@pytest.mark.parametrize("session_role", ["hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_post_403_for_a_non_writer_session_behind_the_shared_recruiter_key(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(role=session_role)

    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 403
    w.get_one.assert_not_awaited()
    w.claim.assert_not_awaited()
    w.arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_403_with_no_session_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 403
    w.claim.assert_not_awaited()
    w.arq.enqueue_job.assert_not_awaited()


# ── POST: audit ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_writes_one_audit_row_for_a_real_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id, job_id = uuid4(), uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id, job_id=job_id))
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    recruiter = _user(role="recruiter")
    app.dependency_overrides[resolve_user] = lambda: recruiter

    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 202
    w.audit.assert_awaited_once()
    kw = _aw(w.audit).kwargs
    assert kw["action"] == "generate_interview_questions"
    assert kw["actor_kind"] == "user"
    assert kw["actor_user_id"] == recruiter.id
    assert kw["actor_service"] is None
    assert kw["subject_type"] == "shortlist_entry"
    assert kw["subject_id"] == entry_id
    assert kw["job_id"] == job_id


@pytest.mark.asyncio
async def test_post_audits_the_dev_anonymous_admin_as_a_service_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    # No resolve_user override: CAS is off in the unit env, so the synthetic
    # dev-anonymous admin resolves and passes the session gate.
    app = _build_app(_conn(), arq=w.arq, role=Role.ADMIN)

    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 202
    w.audit.assert_awaited_once()
    kw = _aw(w.audit).kwargs
    assert kw["actor_kind"] == "service"
    assert kw["actor_user_id"] is None
    assert kw["action"] == "generate_interview_questions"


@pytest.mark.asyncio
async def test_post_audit_details_hold_no_candidate_or_question_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    app = _build_app(_conn(), arq=w.arq, role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(role="recruiter")

    async with await _client(app) as client:
        await client.post(f"/shortlist/{entry_id}/interview-questions")

    details = _aw(w.audit).kwargs.get("details")
    if details is None:
        return
    assert isinstance(details, dict)
    for value in details.values():
        assert value is None or isinstance(value, int | str | bool | float)
    blob = json.dumps(details)
    for forbidden in (QUOTE, REQ_PARTIAL, REQ_MISSING, "Candidate A"):
        assert forbidden not in blob


# ── GET: shape ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review round 1 (F7): GET loads the candidate identifiers through
    ``interview_question_service.load_identifiers`` to hash the scrubbed gaps;
    these mocked-connection tests have no candidate row."""
    monkeypatch.setattr(
        _iq(),
        "load_identifiers",
        AsyncMock(return_value=(None, None, None)),
        raising=False,
    )


def _ready_row(*, gaps_sha: str | None, status: str = "ready") -> _Row:
    stored = [
        {
            "requirement_index": 2,
            "requirement": REQ_MISSING,
            "question": QUESTION_TEXT,
            "probes_quote": None,
        },
        {
            "requirement_index": 1,
            "requirement": REQ_PARTIAL,
            "question": "Walk me through the lab deployment.",
            "probes_quote": QUOTE,
        },
    ]
    return _Row(
        status=status,
        reason=None,
        questions=json.dumps(stored),
        gaps_sha256=gaps_sha,
        prompt_version="interview_questions_v1",
        model="m-test",
        generated_at=_NOW,
        requested_at=_NOW,
        created_by="alice",
    )


def _current_hash(entry: ShortlistEntry) -> str:
    return gaps_hash(select_gaps(entry))


@pytest.mark.asyncio
async def test_get_returns_the_documented_shape_for_a_ready_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    entry = _entry(entry_id=entry_id)
    _Wiring(monkeypatch, entry=entry)
    current = _current_hash(entry)
    app = _build_app(_conn(fetchrow=_ready_row(gaps_sha=current)))

    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == _RESPONSE_KEYS
    assert body["status"] == "ready"
    assert body["reason"] is None
    assert body["gaps_sha256"] == current
    assert body["current_gaps_sha256"] == current
    assert body["stale"] is False
    assert (
        dt.datetime.fromisoformat(body["generated_at"].replace("Z", "+00:00")) == _NOW
    )
    assert [set(q) for q in body["questions"]] == [_QUESTION_KEYS, _QUESTION_KEYS]
    assert body["questions"][0] == {
        "requirement_index": 2,
        "requirement": REQ_MISSING,
        "question": QUESTION_TEXT,
        "probes_quote": None,
    }
    assert body["questions"][1]["probes_quote"] == QUOTE


@pytest.mark.asyncio
async def test_get_marks_stale_when_the_stored_gap_hash_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    entry = _entry(entry_id=entry_id)
    _Wiring(monkeypatch, entry=entry)
    app = _build_app(_conn(fetchrow=_ready_row(gaps_sha="0" * 64)))

    async with await _client(app) as client:
        body = (await client.get(f"/shortlist/{entry_id}/interview-questions")).json()

    assert body["stale"] is True
    assert body["gaps_sha256"] == "0" * 64
    assert body["current_gaps_sha256"] == _current_hash(entry)
    # Stale questions are still returned -- the banner is the UI's job.
    assert len(body["questions"]) == 2


@pytest.mark.asyncio
async def test_get_is_stale_when_the_current_gaps_can_no_longer_be_computed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    all_met = _entry(
        entry_id=entry_id,
        requirements=[_req("5 years of Python", "met", "Built Python services")],
    )
    _Wiring(monkeypatch, entry=all_met)
    app = _build_app(_conn(fetchrow=_ready_row(gaps_sha="a" * 64)))

    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 200
    body = resp.json()
    assert body["current_gaps_sha256"] is None
    assert body["stale"] is True


@pytest.mark.asyncio
async def test_get_with_no_row_is_200_status_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    entry = _entry(entry_id=entry_id)
    _Wiring(monkeypatch, entry=entry)
    app = _build_app(_conn(fetchrow=None))

    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == _RESPONSE_KEYS
    assert body["status"] == "none"
    assert body["questions"] == []
    assert body["gaps_sha256"] is None
    assert body["generated_at"] is None
    assert body["stale"] is False
    assert body["current_gaps_sha256"] == _current_hash(entry)


@pytest.mark.parametrize(
    "status,reason",
    [("pending", None), ("failed", "invalid_output"), ("refused", "no_gaps")],
)
@pytest.mark.asyncio
async def test_get_reports_non_ready_statuses_with_their_fixed_reason(
    status: str, reason: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    row = _Row(
        status=status,
        reason=reason,
        questions="[]",
        gaps_sha256=None,
        generated_at=None,
    )
    app = _build_app(_conn(fetchrow=row))

    async with await _client(app) as client:
        body = (await client.get(f"/shortlist/{entry_id}/interview-questions")).json()

    assert body["status"] == status
    assert body["reason"] == reason
    assert body["questions"] == []
    assert body["stale"] is False


# ── GET: scoping, 404, withdrawn ─────────────────────────────────────────


@pytest.mark.parametrize(
    "key_role,session_role",
    [
        (Role.ADMIN, "admin"),
        (Role.RECRUITER, "recruiter"),
        (Role.HIRING_MANAGER, "hiring_manager"),
        (Role.AUDITOR, "auditor"),
    ],
)
@pytest.mark.asyncio
async def test_get_is_readable_by_every_role(
    key_role: Role, session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    entry = _entry(entry_id=entry_id)
    _Wiring(monkeypatch, entry=entry)
    app = _build_app(
        _conn(fetchrow=_ready_row(gaps_sha=_current_hash(entry))), role=key_role
    )
    app.dependency_overrides[resolve_user] = lambda: _user(role=session_role)

    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_row_scopes_a_hiring_manager_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    manager = _user(role="hiring_manager", cas_username="hm")
    app = _build_app(_conn(fetchrow=None), role=Role.HIRING_MANAGER)
    app.dependency_overrides[resolve_user] = lambda: manager

    async with await _client(app) as client:
        await client.get(f"/shortlist/{entry_id}/interview-questions")

    assert _aw(w.get_one).kwargs.get("user_id") == manager.id


@pytest.mark.parametrize("session_role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_get_does_not_scope_unscoped_roles(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    app = _build_app(_conn(fetchrow=None), role=Role.RECRUITER)
    app.dependency_overrides[resolve_user] = lambda: _user(role=session_role)

    async with await _client(app) as client:
        await client.get(f"/shortlist/{entry_id}/interview-questions")

    assert _aw(w.get_one).kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_get_403_for_a_hiring_manager_key_with_no_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    w = _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    conn = _conn(fetchrow=_ready_row(gaps_sha=None))
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    app.dependency_overrides[resolve_user] = lambda: None

    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 403
    w.get_one.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_404_for_an_unassigned_unknown_or_withdrawn_entry_reads_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_one`` raises NotFoundError for all three (scope miss, no such id,
    ``withdrawn_at IS NOT NULL``). The stored questions must not be read, let
    alone returned, even though a ready row exists for that résumé."""
    _Wiring(monkeypatch, entry=None)
    conn = _conn(fetchrow=_ready_row(gaps_sha="a" * 64))
    app = _build_app(conn, role=Role.HIRING_MANAGER)
    app.dependency_overrides[resolve_user] = lambda: _user(role="hiring_manager")

    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{uuid4()}/interview-questions")

    assert resp.status_code == 404
    assert QUESTION_TEXT not in resp.text
    conn.fetchrow.assert_not_awaited()


# ── GET: auditor read-log ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_by_a_real_auditor_writes_one_read_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    entry = _entry(entry_id=entry_id)
    w = _Wiring(monkeypatch, entry=entry)
    auditor = _user(role="auditor", cas_username="audrey")
    app = _build_app(
        _conn(fetchrow=_ready_row(gaps_sha=_current_hash(entry))), role=Role.AUDITOR
    )
    app.dependency_overrides[resolve_user] = lambda: auditor

    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 200
    w.audit.assert_awaited_once()
    kw = _aw(w.audit).kwargs
    assert kw["action"] == "read_interview_questions"
    assert kw["actor_kind"] == "user"
    assert kw["actor_user_id"] == auditor.id
    assert kw["subject_type"] == "shortlist_entry"
    assert kw["subject_id"] == entry_id


@pytest.mark.parametrize("session_role", ["admin", "recruiter", "hiring_manager"])
@pytest.mark.asyncio
async def test_get_by_a_non_auditor_writes_no_read_row(
    session_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    entry = _entry(entry_id=entry_id)
    w = _Wiring(monkeypatch, entry=entry)
    app = _build_app(
        _conn(fetchrow=_ready_row(gaps_sha=_current_hash(entry))),
        role=Role.HIRING_MANAGER if session_role == "hiring_manager" else Role.ADMIN,
    )
    app.dependency_overrides[resolve_user] = lambda: _user(role=session_role)

    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}/interview-questions")

    assert resp.status_code == 200
    w.audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_404_writes_no_read_row_even_for_a_real_auditor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _Wiring(monkeypatch, entry=None)
    app = _build_app(_conn(fetchrow=None), role=Role.AUDITOR)
    app.dependency_overrides[resolve_user] = lambda: _user(role="auditor")

    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{uuid4()}/interview-questions")

    assert resp.status_code == 404
    w.audit.assert_not_awaited()
