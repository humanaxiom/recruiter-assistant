"""RED -- interview-question generator, slice S2: the persistence service
(``src.services.interview_question_service``), its settings knob and its DDL.

Neither the module nor the table nor the setting exists yet. Imports of the
new module are LAZY (``importlib``) so that a missing module fails the one test
that needs it rather than aborting collection of the whole unit suite.

Contract pinned here (every name below is a decision of this RED commit):

* ``Settings.interview_questions_stall_minutes`` -- int, default 20.
* ``FailureReason`` (StrEnum): ``entry_gone`` / ``invalid_output`` /
  ``no_questions``. ``record_refused`` takes a ``RefusalReason`` value from
  ``src.services.interview_questions``. The two sets are disjoint and BOTH
  functions raise ``ValueError`` for anything else, BEFORE touching the
  connection -- "reasons contain no free text" is enforced, not hoped for.
* ``record_ready(conn, job_id, resume_id, *, questions, gaps, model)`` stores
  the questions with their requirement text (so a stored row stays readable
  after the shortlist's gaps move), ``gaps_hash(gaps)`` and ``PROMPT_VERSION``.
* ``get(conn, job_id, resume_id)`` returns ``None`` or a record with the
  attributes ``status``, ``reason``, ``questions`` (decoded ``list[dict]``),
  ``gaps_sha256``, ``prompt_version``, ``model``, ``generated_at``.

Whether the DB behaves is NOT proven here (a string match cannot); that is
``tests/integration/test_interview_questions_*_pg.py``.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.models.ddl import _STATEMENTS
from src.services.interview_questions import (
    PROMPT_VERSION,
    Gap,
    InterviewQuestionItem,
    RefusalReason,
    gaps_hash,
)
from src.settings import Settings

_NOW = dt.datetime(2026, 11, 21, tzinfo=dt.UTC)

_STALL_FIELD = "interview_questions_stall_minutes"
_FAILURE_REASONS = {"entry_gone", "invalid_output", "no_questions"}
_REFUSAL_REASONS = {r.value for r in RefusalReason}


def _svc() -> Any:
    return importlib.import_module("src.services.interview_question_service")


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _conn(*, fetchrow: Any = None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetchval = AsyncMock(return_value=True)
    conn.transaction = MagicMock()
    return conn


def _flat_args(conn: MagicMock) -> list[Any]:
    out: list[Any] = []
    for name in ("execute", "fetch", "fetchrow", "fetchval"):
        for call in getattr(conn, name).await_args_list:
            out.extend(call.args[1:])
            out.extend(call.kwargs.values())
    return out


def _db_calls(conn: MagicMock) -> int:
    return sum(
        getattr(conn, n).await_count
        for n in ("execute", "fetch", "fetchrow", "fetchval")
    )


# ── settings ─────────────────────────────────────────────────────────────


def test_stall_minutes_default_is_twenty() -> None:
    assert getattr(Settings(), _STALL_FIELD) == 20


def test_stall_minutes_is_a_positive_plain_int() -> None:
    value = getattr(Settings(), _STALL_FIELD)
    assert isinstance(value, int) and not isinstance(value, bool)
    assert value > 0


def test_stall_minutes_is_overridable() -> None:
    settings = Settings(interview_questions_stall_minutes=7)
    assert getattr(settings, _STALL_FIELD) == 7


# ── DDL (string-level; the integration file proves it against Postgres) ──


def _table_sql() -> str:
    for stmt in _STATEMENTS:
        if re.search(
            r"create\s+table\s+if\s+not\s+exists\s+interview_questions\b", stmt, re.I
        ):
            return re.sub(r"\s+", "", stmt).lower()
    raise AssertionError("no CREATE TABLE IF NOT EXISTS interview_questions statement")


def test_ddl_declares_the_table_idempotently() -> None:
    assert _table_sql()


@pytest.mark.parametrize(
    "fragment",
    [
        "jsonbnotnulldefault'[]'",
        "requested_attimestamptznotnulldefaultnow()",
        "generated_attimestamptz",
        "unique(job_id,resume_id)",
        "'pending','ready','failed','refused'",
    ],
)
def test_ddl_carries_the_contract_fragment(fragment: str) -> None:
    assert fragment in _table_sql()


def test_ddl_cascades_from_both_parents() -> None:
    sql = _table_sql()
    assert "job_iduuidnotnullreferencesjobs(id)ondeletecascade" in sql
    assert "resume_iduuidnotnullreferencesresumes(id)ondeletecascade" in sql


# ── FailureReason / reason validation ────────────────────────────────────


def test_failure_reason_is_exactly_the_fixed_set() -> None:
    assert {m.value for m in _svc().FailureReason} == _FAILURE_REASONS


@pytest.mark.parametrize("reason", sorted(_FAILURE_REASONS))
@pytest.mark.asyncio
async def test_record_failed_accepts_each_fixed_reason(reason: str) -> None:
    conn = _conn()
    await _svc().record_failed(conn, uuid4(), uuid4(), reason)
    assert reason in _flat_args(conn)


@pytest.mark.parametrize(
    "bad",
    [
        "Jane Smith could not be parsed",
        "",
        "ENTRY_GONE",
        "not_evaluated",  # a REFUSAL reason is not a failure reason
        "a" * 500,
    ],
)
@pytest.mark.asyncio
async def test_record_failed_rejects_free_text_before_touching_the_db(bad: str) -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        await _svc().record_failed(conn, uuid4(), uuid4(), bad)
    assert _db_calls(conn) == 0


@pytest.mark.parametrize("reason", sorted(_REFUSAL_REASONS))
@pytest.mark.asyncio
async def test_record_refused_accepts_each_refusal_reason(reason: str) -> None:
    conn = _conn()
    await _svc().record_refused(conn, uuid4(), uuid4(), RefusalReason(reason))
    assert reason in _flat_args(conn)


@pytest.mark.parametrize(
    "bad", ["every requirement is evidenced in the resume", "", "entry_gone", "x@y.z"]
)
@pytest.mark.asyncio
async def test_record_refused_rejects_anything_outside_the_refusal_enum(
    bad: str,
) -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        await _svc().record_refused(conn, uuid4(), uuid4(), bad)
    assert _db_calls(conn) == 0


# ── record_ready ─────────────────────────────────────────────────────────


def _gaps() -> list[Gap]:
    return [
        Gap(
            index=1,
            requirement="Kubernetes operations",
            status="partial",
            quote="Deployed a pod once in a lab",
        ),
        Gap(index=2, requirement="Budget ownership", status="missing"),
    ]


def _items() -> list[InterviewQuestionItem]:
    return [
        InterviewQuestionItem(
            requirement_index=1,
            question="Walk me through the lab deployment.",
            probes_quote="Deployed a pod once in a lab",
        ),
        InterviewQuestionItem(
            requirement_index=2, question="Describe a budget you were accountable for."
        ),
    ]


def _stored_questions(conn: MagicMock) -> list[dict[str, Any]]:
    for arg in _flat_args(conn):
        if isinstance(arg, str):
            try:
                decoded = json.loads(arg)
            except ValueError:
                continue
            if isinstance(decoded, list) and decoded and isinstance(decoded[0], dict):
                return decoded
    raise AssertionError("no JSON question list was bound to any statement")


@pytest.mark.asyncio
async def test_record_ready_stores_questions_with_their_requirement_text() -> None:
    conn = _conn()
    await _svc().record_ready(
        conn, uuid4(), uuid4(), questions=_items(), gaps=_gaps(), model="m-test"
    )
    stored = _stored_questions(conn)
    assert [q["requirement_index"] for q in stored] == [1, 2]
    assert [q["requirement"] for q in stored] == [
        "Kubernetes operations",
        "Budget ownership",
    ]
    assert stored[0]["question"] == "Walk me through the lab deployment."
    assert stored[0]["probes_quote"] == "Deployed a pod once in a lab"
    assert stored[1]["probes_quote"] is None


@pytest.mark.asyncio
async def test_record_ready_stamps_gap_hash_prompt_version_and_model() -> None:
    conn = _conn()
    gaps = _gaps()
    await _svc().record_ready(
        conn, uuid4(), uuid4(), questions=_items(), gaps=gaps, model="m-test"
    )
    args = _flat_args(conn)
    assert gaps_hash(gaps) in args
    assert PROMPT_VERSION in args
    assert "m-test" in args


@pytest.mark.asyncio
async def test_record_ready_binds_the_row_identity() -> None:
    conn = _conn()
    job_id, resume_id = uuid4(), uuid4()
    await _svc().record_ready(
        conn, job_id, resume_id, questions=_items(), gaps=_gaps(), model="m"
    )
    args = _flat_args(conn)
    assert job_id in args and resume_id in args


# ── get ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_none_when_there_is_no_row() -> None:
    assert await _svc().get(_conn(fetchrow=None), uuid4(), uuid4()) is None


@pytest.mark.asyncio
async def test_get_decodes_the_jsonb_question_list() -> None:
    stored = [
        {
            "requirement_index": 2,
            "requirement": "Budget ownership",
            "question": "Describe a budget you owned.",
            "probes_quote": None,
        }
    ]
    row = _Row(
        status="ready",
        reason=None,
        questions=json.dumps(stored),  # asyncpg hands jsonb back as str
        gaps_sha256="abc123",
        prompt_version=PROMPT_VERSION,
        model="m-test",
        generated_at=_NOW,
        requested_at=_NOW,
        created_by="alice",
    )
    rec = await _svc().get(_conn(fetchrow=row), uuid4(), uuid4())
    assert rec.status == "ready"
    assert rec.reason is None
    assert rec.questions == stored
    assert rec.gaps_sha256 == "abc123"
    assert rec.prompt_version == PROMPT_VERSION
    assert rec.model == "m-test"
    assert rec.generated_at == _NOW


@pytest.mark.asyncio
async def test_get_of_a_pending_row_has_an_empty_question_list() -> None:
    row = _Row(status="pending", reason=None, questions="[]", gaps_sha256=None)
    rec = await _svc().get(_conn(fetchrow=row), uuid4(), uuid4())
    assert rec.status == "pending"
    assert rec.questions == []
    assert rec.gaps_sha256 is None


def test_the_public_surface_exists() -> None:
    # The names the route and worker call through.
    svc = _svc()
    for name in ("claim", "record_ready", "record_failed", "record_refused", "get"):
        assert callable(getattr(svc, name)), name
