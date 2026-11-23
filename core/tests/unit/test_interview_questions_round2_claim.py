"""RED -- interview-question generator, review round 2, G1 and G2 (unit side).

CONTRACT (decisions of this RED commit; the coder implements exactly this):

G1 -- claim-scoped settles
* New nullable column ``interview_questions.claim_id UUID`` (in the CREATE
  TABLE and an idempotent ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS claim_id
  uuid``). Every successful ``claim()`` sets a FRESH value.
* ``claim(conn, job_id, resume_id, created_by) -> UUID | None``: the new
  claim's id, or ``None`` when a fresh pending row exists (was ``bool``).
* ``record_ready(conn, job_id, resume_id, *, claim_id, questions, gaps, model,
  blind_review=False) -> bool``; ``record_failed(conn, job_id, resume_id,
  reason, *, claim_id) -> bool``; ``record_refused(conn, job_id, resume_id,
  reason, *, claim_id) -> bool``. Each is ONE ``conn.execute`` of an UPDATE
  guarded by ``claim_id = $n AND status = 'pending'`` and returns True iff the
  command tag is ``UPDATE 1``. A mismatch is a no-op (False), never a raise.
  Reason validation still raises ValueError BEFORE any SQL.
* ``heartbeat(conn, job_id, resume_id, *, claim_id) -> bool``: one
  ``conn.execute`` of ``UPDATE ... SET requested_at = now() WHERE ... claim_id
  = $n AND status = 'pending'``; True iff ``UPDATE 1``.
* POST enqueues ``generate_interview_questions(job_id, resume_id, claim_id)``,
  all ``str``. The worker is ``(ctx, job_id, resume_id, claim_id)``, calls
  ``heartbeat`` once at the start of EVERY try (before ``get_one`` and the LLM
  call), passes ``claim_id=UUID`` to every ``record_*``, and when a settle is a
  no-op (returns False) it logs a WARNING containing the word ``superseded``
  and still returns the status it meant to record (no raise).

G2 -- honest stall window
* ``effective_stall_minutes(settings: Settings | None = None) -> int`` in
  ``src.services.interview_question_service`` (None -> the module's
  ``get_settings()``), a pure function of settings:

      per_try_defer = 30 * (resume_parse_max_tries - 1)        # seconds
      floor = ceil(((llm_max_retries + 1) * llm_timeout_s + per_try_defer) / 60)
              + 5                                              # margin, minutes
      effective = max(interview_questions_stall_minutes, floor)

  (30 s is the worker's ``_RETRY_DEFER_SECONDS`` step; the largest defer is on
  the last retry; the margin is 5 minutes.) Defaults give floor 13 -> 20.
* ``claim()`` binds that value as the stall window and the GET route's
  ``_is_stalled`` uses the same helper.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.models.ddl import _STATEMENTS
from src.services.interview_questions import (
    Gap,
    InterviewQuestionItem,
    RefusalReason,
)
from src.settings import Settings
from tests.unit.test_route_interview_questions import (
    _build_app,
    _entry,
    _Wiring,
)
from tests.unit.test_route_interview_questions import _conn as _route_conn
from tests.unit.test_route_interview_questions_round1 import _get, _pending
from tests.unit.test_worker_interview_tasks import (
    _FakeLLM,
    _Harness,
    _ingest,
    _q,
)


def _svc() -> Any:
    return importlib.import_module("src.services.interview_question_service")


def _conn(tag: str = "UPDATE 1", fetchval: Any = None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value=tag)
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    return conn


def _sql(conn: MagicMock) -> str:
    return re.sub(r"\s+", " ", str(conn.execute.await_args.args[0])).lower()


def _gaps() -> list[Gap]:
    return [Gap(index=2, requirement="Budget ownership", status="missing")]


def _items() -> list[InterviewQuestionItem]:
    return [InterviewQuestionItem(requirement_index=2, question="Describe a budget.")]


# -- DDL --------------------------------------------------------------------


def _table_sql() -> str:
    for stmt in _STATEMENTS:
        if re.search(
            r"create\s+table\s+if\s+not\s+exists\s+interview_questions\b", stmt, re.I
        ):
            return re.sub(r"\s+", "", stmt).lower()
    raise AssertionError("no interview_questions CREATE TABLE")


def test_ddl_declares_a_nullable_claim_id_uuid_column() -> None:
    sql = _table_sql()
    assert "claim_iduuid," in sql or "claim_iduuid)" in sql
    assert "claim_iduuidnotnull" not in sql


def test_a_dedicated_idempotent_alter_adds_claim_id_to_a_live_table() -> None:
    alters = " ".join(
        re.sub(r"\s+", "", s).lower()
        for s in _STATEMENTS
        if re.search(r"alter\s+table\s+interview_questions\b", s, re.I)
    )
    assert "addcolumnifnotexistsclaim_iduuid" in alters


# -- claim ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_returns_the_claim_id_it_minted() -> None:
    minted = uuid4()
    conn = _conn(fetchval=minted)
    got = await _svc().claim(conn, uuid4(), uuid4(), "alice")
    assert got == minted
    assert isinstance(got, UUID)


@pytest.mark.asyncio
async def test_claim_returns_none_when_a_fresh_pending_row_exists() -> None:
    assert await _svc().claim(_conn(fetchval=None), uuid4(), uuid4(), "a") is None


@pytest.mark.asyncio
async def test_claim_sql_sets_and_returns_a_fresh_claim_id() -> None:
    conn = _conn(fetchval=uuid4())
    await _svc().claim(conn, uuid4(), uuid4(), "alice")
    sql = re.sub(r"\s+", " ", str(conn.fetchval.await_args.args[0])).lower()
    assert "claim_id" in sql
    assert re.search(r"returning\s+claim_id", sql)
    assert "gen_random_uuid()" in sql or "$5" in sql


# -- record_* are claim-scoped ---------------------------------------------


def _ready_kwargs(cid: UUID) -> dict[str, Any]:
    return {"claim_id": cid, "questions": _items(), "gaps": _gaps(), "model": "m"}


@pytest.mark.parametrize("tag", ["UPDATE 1", "UPDATE 0"])
@pytest.mark.asyncio
async def test_record_ready_reports_whether_the_row_was_written(tag: str) -> None:
    cid = uuid4()
    conn = _conn(tag)
    got = await _svc().record_ready(conn, uuid4(), uuid4(), **_ready_kwargs(cid))
    assert got is (tag == "UPDATE 1")
    sql = _sql(conn)
    assert "claim_id" in sql and "pending" in sql
    assert "update" in sql
    assert cid in conn.execute.await_args.args[1:]


@pytest.mark.parametrize("tag", ["UPDATE 1", "UPDATE 0"])
@pytest.mark.asyncio
async def test_record_failed_reports_whether_the_row_was_written(tag: str) -> None:
    cid = uuid4()
    conn = _conn(tag)
    got = await _svc().record_failed(
        conn, uuid4(), uuid4(), "llm_unavailable", claim_id=cid
    )
    assert got is (tag == "UPDATE 1")
    sql = _sql(conn)
    assert "claim_id" in sql and "pending" in sql and "update" in sql
    assert cid in conn.execute.await_args.args[1:]


@pytest.mark.parametrize("tag", ["UPDATE 1", "UPDATE 0"])
@pytest.mark.asyncio
async def test_record_refused_reports_whether_the_row_was_written(tag: str) -> None:
    cid = uuid4()
    conn = _conn(tag)
    got = await _svc().record_refused(
        conn, uuid4(), uuid4(), RefusalReason.NO_GAPS, claim_id=cid
    )
    assert got is (tag == "UPDATE 1")
    sql = _sql(conn)
    assert "claim_id" in sql and "pending" in sql and "update" in sql
    assert cid in conn.execute.await_args.args[1:]


@pytest.mark.parametrize("name", ["record_ready", "record_failed", "record_refused"])
def test_claim_id_is_a_required_keyword_only_parameter(name: str) -> None:
    param = inspect.signature(getattr(_svc(), name)).parameters["claim_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


@pytest.mark.parametrize("tag", ["UPDATE 1", "UPDATE 0"])
@pytest.mark.asyncio
async def test_heartbeat_refreshes_requested_at_for_its_claim_only(tag: str) -> None:
    cid = uuid4()
    conn = _conn(tag)
    got = await _svc().heartbeat(conn, uuid4(), uuid4(), claim_id=cid)
    assert got is (tag == "UPDATE 1")
    sql = _sql(conn)
    assert "requested_at" in sql and "now()" in sql
    assert "claim_id" in sql and "pending" in sql and "update" in sql
    assert cid in conn.execute.await_args.args[1:]


# -- G2: the effective stall window ----------------------------------------


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({}, 20),
        ({"llm_timeout_s": 900.0, "llm_max_retries": 2}, 52),
        ({"llm_timeout_s": 300.0, "llm_max_retries": 2}, 22),
        (
            {
                "interview_questions_stall_minutes": 1,
                "llm_timeout_s": 900.0,
                "llm_max_retries": 0,
                "resume_parse_max_tries": 1,
            },
            20,
        ),
        (
            {
                "interview_questions_stall_minutes": 1,
                "llm_timeout_s": 900.0,
                "llm_max_retries": 0,
                "resume_parse_max_tries": 5,
            },
            22,
        ),
        (
            {
                "interview_questions_stall_minutes": 90,
                "llm_timeout_s": 900.0,
                "llm_max_retries": 2,
            },
            90,
        ),
    ],
)
def test_effective_stall_minutes_formula(kw: dict[str, Any], expected: int) -> None:
    assert _svc().effective_stall_minutes(Settings(**kw)) == expected


def test_effective_stall_minutes_defaults_to_the_modules_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _svc(),
        "get_settings",
        lambda: Settings(llm_timeout_s=900.0, llm_max_retries=2),
    )
    assert _svc().effective_stall_minutes() == 52


@pytest.mark.asyncio
async def test_claim_binds_the_effective_window_not_the_raw_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _svc(),
        "get_settings",
        lambda: Settings(llm_timeout_s=900.0, llm_max_retries=2),
    )
    conn = _conn(fetchval=uuid4())
    await _svc().claim(conn, uuid4(), uuid4(), "alice")
    bound = conn.fetchval.await_args.args[1:]
    assert 52 in bound
    assert 20 not in bound


def _patch_slow_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.api.routes import shortlist as routes

    for module in (routes, _svc()):
        monkeypatch.setattr(
            module,
            "get_settings",
            lambda: Settings(llm_timeout_s=900.0, llm_max_retries=2),
            raising=False,
        )


@pytest.mark.parametrize("minutes_ago", [25, 30, 51])
@pytest.mark.asyncio
async def test_a_slow_model_row_inside_the_effective_window_is_not_stalled(
    minutes_ago: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    _patch_slow_llm(monkeypatch)
    body = await _get(_build_app(_route_conn(fetchrow=_pending(minutes_ago))), entry_id)
    assert (body["status"], body["reason"]) == ("pending", None)


@pytest.mark.parametrize("minutes_ago", [53, 90])
@pytest.mark.asyncio
async def test_a_slow_model_row_past_the_effective_window_is_stalled(
    minutes_ago: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    _patch_slow_llm(monkeypatch)
    body = await _get(_build_app(_route_conn(fetchrow=_pending(minutes_ago))), entry_id)
    assert (body["status"], body["reason"]) == ("failed", "stalled")


# -- worker: claim id, heartbeat, no-op settle -----------------------------


@pytest.mark.asyncio
async def test_the_worker_heartbeats_before_the_entry_lookup_and_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    llm = _FakeLLM(_ingest(_q(2, "Describe a budget.")))
    real_chat = llm.chat_json

    async def _chat(*a: Any, **k: Any) -> Any:
        order.append("llm")
        return await real_chat(*a, **k)

    llm.chat_json = _chat  # type: ignore[method-assign]
    h = _Harness(monkeypatch, llm=llm)

    async def _beat(*_a: Any, **_k: Any) -> bool:
        order.append("heartbeat")
        return True

    async def _get_one(*_a: Any, **_k: Any) -> Any:
        order.append("get_one")
        return h.entry

    h.heartbeat.side_effect = _beat
    h.get_one.side_effect = _get_one

    await h.run()

    assert order == ["heartbeat", "get_one", "llm"]
    call = h.heartbeat.await_args
    assert call is not None
    assert tuple(call.args[1:3]) == (h.job_id, h.resume_id)
    assert call.kwargs["claim_id"] == h.claim_id


@pytest.mark.asyncio
async def test_the_worker_heartbeats_again_on_a_retry_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arq import Retry

    from src.pipeline.llm import LLMUnavailableError

    h = _Harness(monkeypatch, llm=_FakeLLM(error=LLMUnavailableError("down")))
    h.ctx["job_try"] = 1
    with pytest.raises(Retry):
        await h.run()
    h.ctx["job_try"] = 2
    with pytest.raises(Retry):
        await h.run()
    assert h.heartbeat.await_count == 2
    assert all(c.kwargs["claim_id"] == h.claim_id for c in h.heartbeat.await_args_list)


@pytest.mark.asyncio
async def test_the_worker_passes_its_claim_id_to_record_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, llm=_FakeLLM(_ingest(_q(2, "Describe a budget."))))
    assert await h.run() == "ready"
    call = h.ready.await_args
    assert call is not None and call.kwargs["claim_id"] == h.claim_id


@pytest.mark.asyncio
async def test_the_worker_passes_its_claim_id_to_record_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.pipeline.llm import LLMOutputInvalidError

    h = _Harness(monkeypatch, llm=_FakeLLM(error=LLMOutputInvalidError("bad")))
    assert await h.run() == "failed"
    call = h.failed.await_args
    assert call is not None and call.kwargs["claim_id"] == h.claim_id


@pytest.mark.asyncio
async def test_the_worker_passes_its_claim_id_when_the_entry_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, llm=_FakeLLM(), lookup_hit=False)
    assert await h.run() == "failed"
    call = h.failed.await_args
    assert call is not None and call.kwargs["claim_id"] == h.claim_id


@pytest.mark.asyncio
async def test_the_worker_passes_its_claim_id_to_record_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, llm=_FakeLLM(), evaluated=False)
    assert await h.run() == "refused"
    call = h.refused.await_args
    assert call is not None and call.kwargs["claim_id"] == h.claim_id


@pytest.mark.asyncio
async def test_a_noop_ready_settle_is_logged_superseded_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    h = _Harness(monkeypatch, llm=_FakeLLM(_ingest(_q(2, "Describe a budget."))))
    h.ready.return_value = False
    with caplog.at_level(logging.WARNING):
        result = await h.run()
    assert result == "ready"
    assert any(
        "superseded" in r.getMessage().lower() and r.levelno >= logging.WARNING
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_noop_failed_settle_is_logged_superseded_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from src.pipeline.llm import LLMOutputInvalidError

    h = _Harness(monkeypatch, llm=_FakeLLM(error=LLMOutputInvalidError("bad")))
    h.failed.return_value = False
    with caplog.at_level(logging.WARNING):
        result = await h.run()
    assert result == "failed"
    assert any("superseded" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_successful_settle_logs_no_superseded_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    h = _Harness(monkeypatch, llm=_FakeLLM(_ingest(_q(2, "Describe a budget."))))
    with caplog.at_level(logging.WARNING):
        await h.run()
    assert not any("superseded" in r.getMessage().lower() for r in caplog.records)
