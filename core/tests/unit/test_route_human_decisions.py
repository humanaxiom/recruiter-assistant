"""RED -- human decision record, slice S3: the three API routes.

* ``POST /shortlist/{entry_id}/human-decisions``
* ``GET  /shortlist/{entry_id}/human-decisions``
* ``GET  /jobs/{job_id}/decisions/latest``

The service layer (``src.services.decision_service``) and
``shortlist_service.get_one`` are mocked; this file pins WIRING and CONTRACT
(roles, status codes, scoping arguments, audit shape, response shape). That the
SQL holds is ``tests/integration/test_decision_scoping_pg.py``.

NOT ``/shortlist/{id}/decision`` or ``/shortlist/{id}/stage``: ``test_api.py``
and ``test_route_shortlist.py`` pin those ABSENT and they must stay absent.

Contract choices made here where the plan left them open:

* The routes may live on the existing shortlist router or on a new
  ``src.api.routes.decisions`` router; the app built below mounts both when the
  latter exists.
* The routes must call the service and ``shortlist_service.get_one`` through
  MODULE attributes (what ``monkeypatch.setattr`` patches), and
  ``decision_service.record(db, entry=..., user=..., body=<DecisionIn>)``.
* ``history`` is called as ``history(db, job_id, resume_id)``; the GET route
  builds ``DecisionHistory(latest=history[0] or None, history=history)``.
* ``latest_for_job(db, job_id, user_id)`` -- ``user_id`` is the HM's id for a
  hiring_manager session and ``None`` for everyone else.
* Auditor reads: ``read_decisions`` (subject_type ``resume``, subject_id the
  entry's resume_id, job_id the entry's job) and ``read_decisions_latest``
  (subject_type ``job``, subject_id = job_id, job_id = job_id).
* Role gate order: the dependency 403s (key role, session role) fire BEFORE the
  body is validated, so a non-decider sending junk gets 403, not 422.
"""

from __future__ import annotations

import datetime as dt
import importlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, get_arq, resolve_role, resolve_user
from src.api.routes import shortlist as shortlist_routes
from src.errors import AppError, NotFoundError
from src.models.pool import get_db
from src.schemas.auth import User
from src.schemas.matching import ShortlistEntry
from src.services import audit_service, shortlist_service

_NOW = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.UTC)
_RATIONALE = "Strong systems background, fits the team."
_BODY = {"decision": "accept", "rationale": _RATIONALE}


def _ds() -> Any:
    return importlib.import_module("src.services.decision_service")


def _schemas() -> Any:
    return importlib.import_module("src.schemas.decisions")


def _routers() -> list[APIRouter]:
    routers = [shortlist_routes.router]
    try:
        mod = importlib.import_module("src.api.routes.decisions")
    except ImportError:
        return routers
    routers.append(mod.router)
    return routers


def _entry(
    *, entry_id: UUID | None = None, job_id: UUID | None = None
) -> ShortlistEntry:
    return ShortlistEntry.model_validate(
        {
            "id": entry_id or uuid4(),
            "job_id": job_id or uuid4(),
            "resume_id": uuid4(),
            "rank": 2,
            "score_final": 0.7,
            "score_breakdown": {
                "skill": 0.8,
                "experience": 0.7,
                "education": 0.6,
                "seniority": 0.5,
                "vector": 0.4,
                "structured": 0.65,
            },
            "evidence": {"requirements": []},
            "pipeline_meta": None,
            "generated_at": _NOW,
            "display_label": "Candidate A",
        }
    )


def _user(
    *, role: str | None, cas_username: str = "alice", active: bool = True
) -> User:
    return User(
        id=uuid4(),
        cas_username=cas_username,
        display_name=cas_username,
        email=None,
        role=role,
        active=active,
        created_at=_NOW,
        last_seen_at=_NOW,
    )


def _out(entry: ShortlistEntry, decision: str = "accept") -> Any:
    return _schemas().DecisionOut(
        id=uuid4(),
        job_id=entry.job_id,
        resume_id=entry.resume_id,
        decision=decision,
        rationale=_RATIONALE,
        decided_by="alice",
        decided_by_role="recruiter",
        decided_at=_NOW,
        snap_rank=entry.rank,
        snap_score_final=entry.score_final,
        snap_blind_review=False,
    )


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _build_app(*, key_role: Role = Role.ADMIN) -> FastAPI:
    app = FastAPI()
    for router in _routers():
        app.include_router(router)
    conn = _conn()

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())
    app.dependency_overrides[resolve_role] = lambda: key_role

    @app.exception_handler(AppError)
    async def _handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _Wiring:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        entry: ShortlistEntry | None,
        created: bool = True,
        history: list[Any] | None = None,
        badges: list[Any] | None = None,
    ) -> None:
        self.entry = entry

        async def _get_one(*_a: Any, **_k: Any) -> ShortlistEntry:
            if entry is None:
                raise NotFoundError("shortlist entry not found")
            return entry

        self.get_one = AsyncMock(side_effect=_get_one)
        self.audit = AsyncMock(return_value=None)
        self.record = AsyncMock(
            side_effect=lambda *_a, **_k: (_out(entry or _entry()), created)
        )
        self.history = AsyncMock(return_value=history or [])
        self.latest_for_job = AsyncMock(return_value=badges or [])
        monkeypatch.setattr(shortlist_service, "get_one", self.get_one)
        monkeypatch.setattr(audit_service, "record_audit", self.audit)
        ds = _ds()
        monkeypatch.setattr(ds, "record", self.record)
        monkeypatch.setattr(ds, "history", self.history)
        monkeypatch.setattr(ds, "latest_for_job", self.latest_for_job)


def _app(user: User | None, *, key_role: Role = Role.RECRUITER) -> FastAPI:
    app = _build_app(key_role=key_role)
    app.dependency_overrides[resolve_user] = lambda: user
    return app


def _all_args(mock: AsyncMock) -> list[Any]:
    assert mock.await_args is not None
    return [*mock.await_args.args, *mock.await_args.kwargs.values()]


# -- the route table -------------------------------------------------------


def test_the_three_new_routes_exist_with_the_right_methods() -> None:
    seen: set[tuple[str, str]] = set()
    for router in _routers():
        for route in router.routes:
            for method in getattr(route, "methods", None) or ():
                seen.add((method, getattr(route, "path", "")))
    assert ("POST", "/shortlist/{entry_id}/human-decisions") in seen
    assert ("GET", "/shortlist/{entry_id}/human-decisions") in seen
    assert ("GET", "/jobs/{job_id}/decisions/latest") in seen


def test_no_put_patch_or_delete_exists_for_decisions() -> None:
    for router in _routers():
        for route in router.routes:
            path = getattr(route, "path", "")
            if "decisions" in path:
                assert not (
                    set(getattr(route, "methods", ()) or ())
                    & {"PUT", "PATCH", "DELETE"}
                )


@pytest.mark.parametrize("suffix", ["decision", "stage"])
def test_the_cut_review_workflow_routes_stay_absent(suffix: str) -> None:
    paths = {getattr(r, "path", "") for router in _routers() for r in router.routes}
    assert f"/shortlist/{{entry_id}}/{suffix}" not in paths
    shortlist_paths = [p for p in paths if p.startswith("/shortlist")]
    assert not any(p.endswith(f"/{suffix}") for p in shortlist_paths)


# -- POST: happy path ------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_post_201_for_admin_and_recruiter_sessions(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    app = _app(_user(role=role))
    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert resp.status_code == 201
    body = resp.json()
    assert body["decision"] == "accept"
    assert body["rationale"] == _RATIONALE
    assert body["resume_id"] == str(entry.resume_id)
    assert isinstance(body["decided_at"], str)
    w.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_201_for_an_assigned_hiring_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    hm = _user(role="hiring_manager", cas_username="hm-bob")
    app = _app(hm, key_role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert resp.status_code == 201
    assert w.get_one.await_args is not None
    assert w.get_one.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.parametrize("role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_post_resolves_the_entry_unscoped_for_admin_and_recruiter(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role=role))) as client:
        await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert w.get_one.await_args is not None
    assert entry.id in w.get_one.await_args.args
    assert w.get_one.await_args.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_post_200_not_201_on_an_idempotent_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry, created=False)
    async with await _client(_app(_user(role="recruiter"))) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert resp.status_code == 200
    assert resp.json()["decision"] == "accept"
    w.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_passes_entry_session_user_and_validated_body_to_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    user = _user(role="recruiter", cas_username="alice")
    async with await _client(_app(user)) as client:
        await client.post(
            f"/shortlist/{entry.id}/human-decisions",
            json={"decision": "hold", "rationale": f"   {_RATIONALE}   "},
        )
    assert w.record.await_args is not None
    kw = w.record.await_args.kwargs
    assert kw["entry"] == entry
    assert kw["user"] == user
    assert kw["body"].decision == "hold"
    assert kw["body"].rationale == _RATIONALE


@pytest.mark.asyncio
async def test_post_with_cas_off_dev_admin_can_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    app = _build_app(key_role=Role.ADMIN)  # no resolve_user override: dev admin
    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert resp.status_code == 201
    w.record.assert_awaited_once()


# -- POST: refusals --------------------------------------------------------


@pytest.mark.asyncio
async def test_post_404_for_a_missing_withdrawn_or_unassigned_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _Wiring(monkeypatch, entry=None)
    hm = _user(role="hiring_manager")
    async with await _client(_app(hm)) as client:
        resp = await client.post(f"/shortlist/{uuid4()}/human-decisions", json=_BODY)
    assert resp.status_code == 404
    w.record.assert_not_awaited()
    w.audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_unassigned_hiring_manager_is_404_never_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-020 s5: no existence oracle. get_one 404s for an entry the HM is not
    assigned to, and that surfaces unchanged."""
    w = _Wiring(monkeypatch, entry=None)
    hm = _user(role="hiring_manager")
    async with await _client(_app(hm, key_role=Role.HIRING_MANAGER)) as client:
        resp = await client.post(f"/shortlist/{uuid4()}/human-decisions", json=_BODY)
    assert resp.status_code == 404
    w.record.assert_not_awaited()


@pytest.mark.parametrize("role", ["auditor", "read_only", ""])
@pytest.mark.asyncio
async def test_post_403_for_a_non_decider_session(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role=role))) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert resp.status_code == 403
    w.get_one.assert_not_awaited()
    w.record.assert_not_awaited()
    w.audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_403_for_a_session_with_no_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role=None))) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert resp.status_code == 403
    w.record.assert_not_awaited()


@pytest.mark.parametrize("role", ["admin", "recruiter", "hiring_manager"])
@pytest.mark.asyncio
async def test_post_403_for_an_inactive_account(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role=role, active=False))) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert resp.status_code == 403
    w.record.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_403_with_no_session_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(None)) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert resp.status_code == 403
    w.record.assert_not_awaited()


# -- POST: key role vs session role ---------------------------------------


@pytest.mark.parametrize(
    ("key_role", "session_role", "expected"),
    [
        (Role.ADMIN, "admin", 201),
        (Role.RECRUITER, "recruiter", 201),
        (Role.RECRUITER, "hiring_manager", 201),  # the shared BFF key + an HM session
        (Role.HIRING_MANAGER, "hiring_manager", 201),
        (Role.RECRUITER, "auditor", 403),  # shared key must not launder an auditor
        (Role.AUDITOR, "recruiter", 403),  # an auditor key is never a decider
        (Role.AUDITOR, "admin", 403),
        (Role.AUDITOR, "hiring_manager", 403),
    ],
)
@pytest.mark.asyncio
async def test_post_role_matrix_key_versus_session(
    key_role: Role, session_role: str, expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    app = _app(_user(role=session_role), key_role=key_role)
    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    assert resp.status_code == expected
    if expected == 403:
        w.record.assert_not_awaited()


# -- POST: validation ------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"decision": "accept", "rationale": "too short"},
        {"decision": "accept", "rationale": "x" * 1001},
        {"decision": "accept", "rationale": "   "},
        {"decision": "approve", "rationale": _RATIONALE},
        {"decision": "accept"},
        {"rationale": _RATIONALE},
        {},
        {"decision": "accept", "rationale": _RATIONALE, "decided_by": "mallory"},
        {"decision": "accept", "rationale": f"bad {chr(0)} byte here"},
    ],
)
@pytest.mark.asyncio
async def test_post_422_for_a_bad_body_and_nothing_is_written(
    payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role="recruiter"))) as client:
        resp = await client.post(f"/shortlist/{entry.id}/human-decisions", json=payload)
    assert resp.status_code == 422
    w.record.assert_not_awaited()
    w.audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_a_client_supplied_decided_by_never_reaches_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role="recruiter"))) as client:
        resp = await client.post(
            f"/shortlist/{entry.id}/human-decisions",
            json=_BODY | {"decided_by": "mallory"},
        )
    assert resp.status_code == 422
    assert w.record.await_count == 0


@pytest.mark.asyncio
async def test_post_non_decider_with_a_junk_body_gets_403_not_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role="auditor"))) as client:
        resp = await client.post(
            f"/shortlist/{entry.id}/human-decisions", json={"junk": 1}
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_post_route_never_writes_an_audit_row_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision audit row is written INSIDE the service's transaction (one
    commit with the insert). The route must not write a second, autocommitted
    one."""
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role="recruiter"))) as client:
        await client.post(f"/shortlist/{entry.id}/human-decisions", json=_BODY)
    w.audit.assert_not_awaited()


# -- GET history -----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_history_returns_latest_and_newest_first_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    newer, older = _out(entry, "reject"), _out(entry, "accept")
    w = _Wiring(monkeypatch, entry=entry, history=[newer, older])
    async with await _client(_app(_user(role="recruiter"))) as client:
        resp = await client.get(f"/shortlist/{entry.id}/human-decisions")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"latest", "history"}
    assert body["latest"]["decision"] == "reject"
    assert [d["decision"] for d in body["history"]] == ["reject", "accept"]
    args = _all_args(w.history)
    assert entry.job_id in args and entry.resume_id in args


@pytest.mark.asyncio
async def test_get_history_with_no_decisions_is_200_with_null_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    _Wiring(monkeypatch, entry=entry, history=[])
    async with await _client(_app(_user(role="recruiter"))) as client:
        resp = await client.get(f"/shortlist/{entry.id}/human-decisions")
    assert resp.status_code == 200
    assert resp.json() == {"latest": None, "history": []}


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor", "hiring_manager"])
@pytest.mark.asyncio
async def test_get_history_is_open_to_every_role(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role=role))) as client:
        resp = await client.get(f"/shortlist/{entry.id}/human-decisions")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_history_scopes_a_hiring_manager_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    hm = _user(role="hiring_manager")
    async with await _client(_app(hm)) as client:
        await client.get(f"/shortlist/{entry.id}/human-decisions")
    assert w.get_one.await_args is not None
    assert w.get_one.await_args.kwargs.get("user_id") == hm.id


@pytest.mark.asyncio
async def test_get_history_404_when_the_entry_does_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _Wiring(monkeypatch, entry=None)
    async with await _client(_app(_user(role="hiring_manager"))) as client:
        resp = await client.get(f"/shortlist/{uuid4()}/human-decisions")
    assert resp.status_code == 404
    w.history.assert_not_awaited()
    w.audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_history_403_for_a_hiring_manager_key_without_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(None, key_role=Role.HIRING_MANAGER)) as client:
        resp = await client.get(f"/shortlist/{entry.id}/human-decisions")
    assert resp.status_code == 403
    w.history.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_history_403_for_a_deactivated_hiring_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    hm = _user(role="hiring_manager", active=False)
    async with await _client(_app(hm)) as client:
        resp = await client.get(f"/shortlist/{entry.id}/human-decisions")
    assert resp.status_code == 403
    w.history.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_history_logs_a_real_auditor_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    auditor = _user(role="auditor")
    async with await _client(_app(auditor)) as client:
        resp = await client.get(f"/shortlist/{entry.id}/human-decisions")
    assert resp.status_code == 200
    w.audit.assert_awaited_once()
    assert w.audit.await_args is not None
    kw = w.audit.await_args.kwargs
    assert kw["action"] == "read_decisions"
    assert kw["subject_type"] == "resume"
    assert kw["subject_id"] == entry.resume_id
    assert kw["job_id"] == entry.job_id
    assert kw["actor_user_id"] == auditor.id


@pytest.mark.parametrize("role", ["admin", "recruiter", "hiring_manager"])
@pytest.mark.asyncio
async def test_get_history_does_not_log_non_auditor_reads(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    w = _Wiring(monkeypatch, entry=entry)
    async with await _client(_app(_user(role=role))) as client:
        await client.get(f"/shortlist/{entry.id}/human-decisions")
    w.audit.assert_not_awaited()


# -- GET latest per job ----------------------------------------------------


def _badge(resume_id: UUID, decision: str = "hold") -> Any:
    return _schemas().DecisionBadge(
        resume_id=resume_id, decision=decision, decided_by="alice", decided_at=_NOW
    )


@pytest.mark.asyncio
async def test_latest_returns_the_badge_list_without_rationale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, r1, r2 = uuid4(), uuid4(), uuid4()
    _Wiring(monkeypatch, entry=None, badges=[_badge(r1), _badge(r2, "reject")])
    async with await _client(_app(_user(role="recruiter"))) as client:
        resp = await client.get(f"/jobs/{job_id}/decisions/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert [b["resume_id"] for b in body] == [str(r1), str(r2)]
    for b in body:
        assert set(b) == {"resume_id", "decision", "decided_by", "decided_at"}
        assert isinstance(b["decided_at"], str)


@pytest.mark.asyncio
async def test_latest_for_an_unassigned_or_unknown_job_is_200_empty_never_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Wiring(monkeypatch, entry=None, badges=[])
    async with await _client(_app(_user(role="hiring_manager"))) as client:
        resp = await client.get(f"/jobs/{uuid4()}/decisions/latest")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_latest_scopes_a_hiring_manager_session_by_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    w = _Wiring(monkeypatch, entry=None)
    hm = _user(role="hiring_manager")
    async with await _client(_app(hm)) as client:
        await client.get(f"/jobs/{job_id}/decisions/latest")
    args = _all_args(w.latest_for_job)
    assert job_id in args and hm.id in args


@pytest.mark.parametrize("role", ["admin", "recruiter", "auditor"])
@pytest.mark.asyncio
async def test_latest_is_unscoped_for_other_roles(
    role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    w = _Wiring(monkeypatch, entry=None)
    async with await _client(_app(_user(role=role))) as client:
        resp = await client.get(f"/jobs/{job_id}/decisions/latest")
    assert resp.status_code == 200
    args = _all_args(w.latest_for_job)
    assert job_id in args
    assert args[-1] is None


@pytest.mark.asyncio
async def test_latest_403_for_a_hiring_manager_key_without_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _Wiring(monkeypatch, entry=None)
    async with await _client(_app(None, key_role=Role.HIRING_MANAGER)) as client:
        resp = await client.get(f"/jobs/{uuid4()}/decisions/latest")
    assert resp.status_code == 403
    w.latest_for_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_latest_logs_a_real_auditor_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    w = _Wiring(monkeypatch, entry=None)
    auditor = _user(role="auditor")
    async with await _client(_app(auditor)) as client:
        await client.get(f"/jobs/{job_id}/decisions/latest")
    w.audit.assert_awaited_once()
    assert w.audit.await_args is not None
    kw = w.audit.await_args.kwargs
    assert kw["action"] == "read_decisions_latest"
    assert kw["subject_type"] == "job"
    assert kw["subject_id"] == job_id
    assert kw["job_id"] == job_id


@pytest.mark.asyncio
async def test_latest_does_not_log_a_recruiter_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = _Wiring(monkeypatch, entry=None)
    async with await _client(_app(_user(role="recruiter"))) as client:
        await client.get(f"/jobs/{uuid4()}/decisions/latest")
    w.audit.assert_not_awaited()
