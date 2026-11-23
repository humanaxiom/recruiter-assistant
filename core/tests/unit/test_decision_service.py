"""RED -- human decision record, slice S2: ``src.services.decision_service``.

Everything external is mocked (asyncpg connection, ``audit_service``). The SQL
actually holding is ``tests/integration/test_decision_records_pg.py`` /
``test_decision_scoping_pg.py``; this file pins the service's SHAPE.

Contract decisions made here where the plan left them open:

* ``record(conn, *, entry, user, body)`` -- ``body`` is a ``DecisionIn``. The
  plan lists only ``entry`` and ``user``; the decision and rationale have to
  arrive somehow, and a validated ``DecisionIn`` is the narrowest carrier.
* Inside ONE ``conn.transaction()``: first ``conn.fetchrow`` reads the latest
  row for (job, resume); if it matches decided_by + decision + rationale it is
  returned with ``created=False`` and NOTHING else happens; otherwise the
  second ``conn.fetchrow`` is the ``INSERT ... RETURNING`` and
  ``audit_service.record_audit`` runs before the transaction exits.
* ``history`` and ``latest_for_job`` use ``conn.fetch``.
* The dev sentinel user (all-zero id) is stored with ``decided_by_user_id`` NULL.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.schemas.auth import User
from src.schemas.matching import ShortlistEntry
from src.services import audit_service

_NOW = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.UTC)
_SENTINEL = UUID("00000000-0000-0000-0000-000000000000")
_RATIONALE = "Strong systems background, fits the team."
_SECRET_NAME = "Jane Smith"


def _svc() -> Any:
    return importlib.import_module("src.services.decision_service")


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _user(*, role: str = "recruiter", cas_username: str = "alice") -> User:
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


def _entry(
    *, blinded: bool = False, rank: int = 4, score: float = 0.62
) -> ShortlistEntry:
    return ShortlistEntry.model_validate(
        {
            "id": uuid4(),
            "job_id": uuid4(),
            "resume_id": uuid4(),
            "rank": rank,
            "score_final": score,
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
            "blinded": blinded,
            "display_label": "Candidate A",
        }
    )


def _row(
    entry: ShortlistEntry,
    *,
    decision: str = "accept",
    rationale: str = _RATIONALE,
    decided_by: str = "alice",
    role: str = "recruiter",
) -> _Row:
    return _Row(
        id=uuid4(),
        seq=1,
        job_id=entry.job_id,
        resume_id=entry.resume_id,
        decision=decision,
        rationale=rationale,
        decided_by=decided_by,
        decided_by_user_id=None,
        decided_by_role=role,
        decided_at=_NOW,
        snap_rank=entry.rank,
        snap_score_final=entry.score_final,
        snap_entry_id=entry.id,
        snap_blind_review=entry.blinded,
    )


class _Txn:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self) -> None:
        self.events.append("txn_enter")

    async def __aexit__(self, *_a: Any) -> bool:
        self.events.append("txn_exit")
        return False


def _conn(
    *, fetchrow: list[Any] | None = None, fetch: list[Any] | None = None
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.events = []
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow or [None]))
    conn.fetch = AsyncMock(return_value=list(fetch or []))
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(side_effect=lambda: _Txn(conn.events))
    return conn


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(audit_service, "record_audit", mock)
    return mock


def _body(decision: str = "accept", rationale: str = _RATIONALE) -> Any:
    model = importlib.import_module("src.schemas.decisions").DecisionIn
    return model.model_validate({"decision": decision, "rationale": rationale})


def _flat_args(conn: MagicMock) -> list[Any]:
    out: list[Any] = []
    for name in ("execute", "fetch", "fetchrow", "fetchval"):
        for call in getattr(conn, name).await_args_list:
            out.extend(call.args[1:])
            out.extend(call.kwargs.values())
    return out


def _sql(mock: AsyncMock, index: int = -1) -> str:
    return re.sub(r"\s+", " ", mock.await_args_list[index].args[0]).lower()


# -- record: insert path ---------------------------------------------------


@pytest.mark.asyncio
async def test_record_inserts_and_returns_created_true(audit: AsyncMock) -> None:
    entry = _entry()
    conn = _conn(fetchrow=[None, _row(entry)])
    out, created = await _svc().record(conn, entry=entry, user=_user(), body=_body())
    assert created is True
    assert out.decision == "accept"
    assert out.rationale == _RATIONALE
    assert out.resume_id == entry.resume_id


@pytest.mark.asyncio
async def test_record_insert_carries_session_identity_not_body(
    audit: AsyncMock,
) -> None:
    entry = _entry()
    user = _user(role="hiring_manager", cas_username="hm-bob")
    inserted = _row(entry, decided_by="hm-bob", role="hiring_manager")
    conn = _conn(fetchrow=[None, inserted])
    await _svc().record(conn, entry=entry, user=user, body=_body())
    args = _flat_args(conn)
    assert "hm-bob" in args
    assert "hiring_manager" in args
    assert user.id in args


@pytest.mark.asyncio
async def test_record_insert_snapshots_rank_score_entry_and_blind(
    audit: AsyncMock,
) -> None:
    entry = _entry(blinded=True, rank=7, score=0.4321)
    conn = _conn(fetchrow=[None, _row(entry)])
    await _svc().record(conn, entry=entry, user=_user(), body=_body())
    insert_args = conn.fetchrow.await_args_list[-1].args[1:]
    assert 7 in insert_args
    assert 0.4321 in insert_args
    assert entry.id in insert_args
    assert True in insert_args
    assert entry.job_id in insert_args and entry.resume_id in insert_args


@pytest.mark.asyncio
async def test_record_insert_is_parameterised_never_interpolated(
    audit: AsyncMock,
) -> None:
    entry = _entry()
    conn = _conn(fetchrow=[None, _row(entry)])
    await _svc().record(conn, entry=entry, user=_user(), body=_body())
    sql = _sql(conn.fetchrow)
    assert sql.startswith("insert into decision_records")
    assert _RATIONALE.lower() not in sql
    assert "returning" in sql


@pytest.mark.asyncio
async def test_record_dev_sentinel_user_stores_null_user_id(audit: AsyncMock) -> None:
    entry = _entry()
    dev = _user(role="admin", cas_username="dev-anonymous")
    dev = dev.model_copy(update={"id": _SENTINEL})
    conn = _conn(fetchrow=[None, _row(entry, decided_by="dev-anonymous", role="admin")])
    await _svc().record(conn, entry=entry, user=dev, body=_body())
    assert _SENTINEL not in _flat_args(conn)
    assert "dev-anonymous" in _flat_args(conn)


# -- record: transaction + audit -------------------------------------------


@pytest.mark.asyncio
async def test_record_runs_in_one_transaction_with_the_audit_inside(
    audit: AsyncMock,
) -> None:
    entry = _entry()
    conn = _conn(fetchrow=[None, _row(entry)])

    async def _audit(*_a: Any, **_k: Any) -> None:
        conn.events.append("audit")

    audit.side_effect = _audit
    await _svc().record(conn, entry=entry, user=_user(), body=_body())
    assert conn.transaction.call_count == 1
    assert conn.events == ["txn_enter", "audit", "txn_exit"]


@pytest.mark.asyncio
async def test_record_audit_arguments(audit: AsyncMock) -> None:
    entry = _entry(blinded=True, rank=4, score=0.62)
    user = _user()
    inserted = _row(entry)
    conn = _conn(fetchrow=[None, inserted])
    await _svc().record(conn, entry=entry, user=user, body=_body())
    audit.assert_awaited_once()
    assert audit.await_args is not None
    assert audit.await_args.args[0] is conn
    kw = audit.await_args.kwargs
    assert kw["action"] == "record_decision"
    assert kw["actor_kind"] == "user"
    assert kw["actor_user_id"] == user.id
    assert kw["actor_service"] is None
    assert kw["subject_type"] == "resume"
    assert kw["subject_id"] == entry.resume_id
    assert kw["job_id"] == entry.job_id
    assert kw["details"] == {
        "decision_id": str(inserted["id"]),
        "decision": "accept",
        "rank": 4,
        "score_final": 0.62,
        "superseded_decision_id": None,
        "blind_review": True,
    }


@pytest.mark.asyncio
async def test_record_audit_names_the_superseded_decision(audit: AsyncMock) -> None:
    entry = _entry()
    previous = _row(entry, decision="hold", rationale="Waiting on the second round.")
    conn = _conn(fetchrow=[previous, _row(entry, decision="reject")])
    await _svc().record(conn, entry=entry, user=_user(), body=_body("reject"))
    assert audit.await_args is not None
    details = audit.await_args.kwargs["details"]
    assert details["superseded_decision_id"] == str(previous["id"])
    assert details["decision"] == "reject"


@pytest.mark.asyncio
async def test_record_audit_details_never_hold_rationale_or_candidate_text(
    audit: AsyncMock,
) -> None:
    entry = _entry()
    secret = f"{_SECRET_NAME} called me at 604-555-0100"
    conn = _conn(fetchrow=[None, _row(entry, rationale=secret)])
    await _svc().record(conn, entry=entry, user=_user(), body=_body(rationale=secret))
    assert audit.await_args is not None
    kw = audit.await_args.kwargs
    blob = json.dumps(kw["details"]) + json.dumps(
        {k: str(v) for k, v in kw.items() if k != "details"}
    )
    for forbidden in (secret, _SECRET_NAME, "604-555-0100", "Candidate A"):
        assert forbidden not in blob
    assert set(kw["details"]) == {
        "decision_id",
        "decision",
        "rank",
        "score_final",
        "superseded_decision_id",
        "blind_review",
    }


@pytest.mark.asyncio
async def test_record_dev_sentinel_audits_as_a_service_actor(audit: AsyncMock) -> None:
    entry = _entry()
    dev = _user(role="admin", cas_username="dev-anonymous").model_copy(
        update={"id": _SENTINEL}
    )
    conn = _conn(fetchrow=[None, _row(entry, decided_by="dev-anonymous", role="admin")])
    await _svc().record(conn, entry=entry, user=dev, body=_body())
    assert audit.await_args is not None
    kw = audit.await_args.kwargs
    assert kw["actor_kind"] == "service"
    assert kw["actor_user_id"] is None
    assert kw["actor_service"] == "dev-anonymous"


@pytest.mark.asyncio
async def test_record_insert_failure_writes_no_audit(audit: AsyncMock) -> None:
    entry = _entry()
    conn = _conn(fetchrow=[None])
    conn.fetchrow = AsyncMock(side_effect=[None, RuntimeError("insert failed")])
    with pytest.raises(RuntimeError):
        await _svc().record(conn, entry=entry, user=_user(), body=_body())
    audit.assert_not_awaited()


# -- record: idempotency ---------------------------------------------------


@pytest.mark.asyncio
async def test_record_exact_repeat_returns_existing_without_insert_or_audit(
    audit: AsyncMock,
) -> None:
    entry = _entry()
    existing = _row(entry)
    conn = _conn(fetchrow=[existing])
    out, created = await _svc().record(conn, entry=entry, user=_user(), body=_body())
    assert created is False
    assert out.id == existing["id"]
    assert conn.fetchrow.await_count == 1
    conn.execute.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize(
    "changed",
    [
        {"decision": "reject"},
        {"rationale": "A different and equally long rationale."},
        {"decided_by": "someone-else"},
    ],
)
@pytest.mark.asyncio
async def test_record_any_difference_is_a_new_row(
    changed: dict[str, str], audit: AsyncMock
) -> None:
    entry = _entry()
    latest = _row(entry, **changed)
    conn = _conn(fetchrow=[latest, _row(entry)])
    _out, created = await _svc().record(conn, entry=entry, user=_user(), body=_body())
    assert created is True
    assert conn.fetchrow.await_count == 2
    audit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_idempotency_compares_the_stripped_rationale(
    audit: AsyncMock,
) -> None:
    entry = _entry()
    conn = _conn(fetchrow=[_row(entry, rationale=_RATIONALE)])
    _out, created = await _svc().record(
        conn, entry=entry, user=_user(), body=_body(rationale=f"  {_RATIONALE}\n")
    )
    assert created is False


@pytest.mark.asyncio
async def test_record_read_of_latest_is_scoped_to_job_and_resume(
    audit: AsyncMock,
) -> None:
    entry = _entry()
    conn = _conn(fetchrow=[_row(entry)])
    await _svc().record(conn, entry=entry, user=_user(), body=_body())
    first = conn.fetchrow.await_args_list[0]
    assert entry.job_id in first.args[1:] and entry.resume_id in first.args[1:]
    sql = re.sub(r"\s+", " ", first.args[0]).lower()
    assert "from decision_records" in sql
    assert "order by seq desc" in sql
    assert "limit 1" in sql


# -- history ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_returns_decisions_in_the_order_the_query_gave() -> None:
    entry = _entry()
    rows = [_row(entry, decision="reject"), _row(entry, decision="accept")]
    conn = _conn(fetch=rows)
    out = await _svc().history(conn, entry.job_id, entry.resume_id)
    assert [d.decision for d in out] == ["reject", "accept"]


@pytest.mark.asyncio
async def test_history_sql_orders_by_seq_desc_for_this_pair() -> None:
    entry = _entry()
    conn = _conn(fetch=[])
    assert await _svc().history(conn, entry.job_id, entry.resume_id) == []
    sql = _sql(conn.fetch)
    assert "from decision_records" in sql
    assert "order by seq desc" in sql
    args = conn.fetch.await_args_list[0].args[1:]
    assert entry.job_id in args and entry.resume_id in args


# -- latest_for_job --------------------------------------------------------


def _badge_row(resume_id: UUID, decision: str = "hold") -> _Row:
    return _Row(
        resume_id=resume_id, decision=decision, decided_by="alice", decided_at=_NOW
    )


@pytest.mark.asyncio
async def test_latest_for_job_returns_badges_without_rationale() -> None:
    rid = uuid4()
    conn = _conn(fetch=[_badge_row(rid)])
    out = await _svc().latest_for_job(conn, uuid4(), None)
    assert len(out) == 1
    assert out[0].resume_id == rid
    assert "rationale" not in out[0].model_dump()


@pytest.mark.asyncio
async def test_latest_for_job_is_distinct_on_resume_newest_seq_first() -> None:
    conn = _conn()
    await _svc().latest_for_job(conn, uuid4(), None)
    sql = _sql(conn.fetch)
    assert "distinct on (resume_id)" in sql
    assert "order by resume_id, seq desc" in sql


@pytest.mark.asyncio
async def test_latest_for_job_has_no_join_to_resumes_or_shortlist_entries() -> None:
    conn = _conn()
    await _svc().latest_for_job(conn, uuid4(), None)
    sql = _sql(conn.fetch)
    assert " join " not in sql
    assert "shortlist_entries" not in sql
    assert "candidate_name" not in sql


@pytest.mark.asyncio
async def test_latest_for_job_excludes_withdrawn_resumes_by_subselect() -> None:
    conn = _conn()
    await _svc().latest_for_job(conn, uuid4(), None)
    sql = _sql(conn.fetch)
    assert "withdrawn_at is null" in sql
    assert "exists" in sql


@pytest.mark.asyncio
async def test_latest_for_job_unscoped_has_no_assignee_filter() -> None:
    conn = _conn()
    job_id = uuid4()
    await _svc().latest_for_job(conn, job_id, None)
    assert "job_assignees" not in _sql(conn.fetch)
    assert job_id in conn.fetch.await_args_list[0].args[1:]


@pytest.mark.asyncio
async def test_latest_for_job_scoped_filters_by_assignment() -> None:
    conn = _conn()
    job_id, user_id = uuid4(), uuid4()
    await _svc().latest_for_job(conn, job_id, user_id)
    sql = _sql(conn.fetch)
    assert "exists" in sql and "job_assignees" in sql
    args = conn.fetch.await_args_list[0].args[1:]
    assert job_id in args and user_id in args


@pytest.mark.asyncio
async def test_latest_for_job_empty_result_is_an_empty_list() -> None:
    assert await _svc().latest_for_job(_conn(), uuid4(), uuid4()) == []
