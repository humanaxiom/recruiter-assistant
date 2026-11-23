"""RED -- interview-question generator, review round 1: the GET route.

Contract chosen here (the coder follows these names):

* ``interview_questions.blind_review BOOLEAN NOT NULL DEFAULT false`` records
  the job's blind state when the set was GENERATED (``record_ready(...,
  blind_review=)``); ``InterviewQuestionsRecord`` gains ``blind_review`` and
  ``requested_at``.
* **F1 -- blind flip.** On GET, when ``entry.blinded`` is true NOW and the
  stored set was generated non-blind, the response is ``status="ready"``,
  ``reason="blind_changed"``, ``questions=[]``, ``stale=True``: no question
  text and no ``probes_quote`` leave the server. The marker is the existing
  ``reason`` field (no new response key). Every other combination serves the
  stored set as before.
* **F3 -- stall.** A ``pending`` row whose ``requested_at`` is older than
  ``settings.interview_questions_stall_minutes`` is reported as
  ``status="failed"``, ``reason="stalled"``, ``questions=[]``. Nothing is
  written. A ``None`` ``requested_at`` is never treated as stalled.
* **F7.** The GET-side hash is computed over the SCRUBBED gaps, with the
  candidate identifiers loaded through
  ``interview_question_service.load_identifiers(conn, resume_id) ->
  (name, email, phone)`` (module attribute; the worker uses the same helper).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.services.interview_questions import gaps_hash, redact_gaps, select_gaps
from tests.unit.test_route_interview_questions import (
    QUESTION_TEXT,
    QUOTE,
    REQ_MISSING,
    REQ_PARTIAL,
    _build_app,
    _client,
    _conn,
    _entry,
    _iq,
    _ready_row,
    _req,
    _Row,
    _Wiring,
)


@pytest.fixture(autouse=True)
def _no_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _iq(),
        "load_identifiers",
        AsyncMock(return_value=(None, None, None)),
        raising=False,
    )


def _blinded(entry_id: Any, *, blinded: bool) -> Any:
    return _entry(entry_id=entry_id).model_copy(update={"blinded": blinded})


async def _get(app: Any, entry_id: Any) -> dict[str, Any]:
    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}/interview-questions")
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    return body


def _row(*, gaps_sha: str | None, blind: bool) -> _Row:
    row = _ready_row(gaps_sha=gaps_sha)
    row["blind_review"] = blind
    return row


# -- F1 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_set_generated_non_blind_is_withheld_once_the_job_is_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    entry = _blinded(entry_id, blinded=True)
    _Wiring(monkeypatch, entry=entry)
    sha = gaps_hash(select_gaps(entry))
    app = _build_app(_conn(fetchrow=_row(gaps_sha=sha, blind=False)))

    body = await _get(app, entry_id)

    assert body["status"] == "ready"
    assert body["reason"] == "blind_changed"
    assert body["questions"] == []
    assert body["stale"] is True


@pytest.mark.asyncio
async def test_a_withheld_set_leaks_no_question_or_quote_text_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    entry = _blinded(entry_id, blinded=True)
    _Wiring(monkeypatch, entry=entry)
    app = _build_app(
        _conn(fetchrow=_row(gaps_sha=gaps_hash(select_gaps(entry)), blind=False))
    )

    raw = json.dumps(await _get(app, entry_id))

    for forbidden in (QUESTION_TEXT, QUOTE, "lab deployment"):
        assert forbidden not in raw


@pytest.mark.parametrize(
    ("blind_now", "generated_blind"),
    [(True, True), (False, False), (False, True)],
)
@pytest.mark.asyncio
async def test_every_other_blind_combination_serves_the_stored_set(
    blind_now: bool, generated_blind: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    entry = _blinded(entry_id, blinded=blind_now)
    _Wiring(monkeypatch, entry=entry)
    sha = gaps_hash(select_gaps(entry))
    app = _build_app(_conn(fetchrow=_row(gaps_sha=sha, blind=generated_blind)))

    body = await _get(app, entry_id)

    assert body["reason"] is None
    assert body["stale"] is False
    assert [q["question"] for q in body["questions"]][0] == QUESTION_TEXT
    assert len(body["questions"]) == 2


@pytest.mark.asyncio
async def test_a_legacy_row_without_a_blind_stamp_is_treated_as_non_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The column defaults false, so a pre-existing set on a job that is blind
    now is withheld -- the conservative direction."""
    entry_id = uuid4()
    entry = _blinded(entry_id, blinded=True)
    _Wiring(monkeypatch, entry=entry)
    row = _ready_row(gaps_sha=gaps_hash(select_gaps(entry)))
    row["blind_review"] = None
    body = await _get(_build_app(_conn(fetchrow=row)), entry_id)
    assert body["reason"] == "blind_changed"
    assert body["questions"] == []


# -- F3 ---------------------------------------------------------------------


def _pending(minutes_ago: float | None) -> _Row:
    requested = (
        dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)
        if minutes_ago is not None
        else None
    )
    return _Row(
        status="pending",
        reason=None,
        questions="[]",
        gaps_sha256=None,
        generated_at=None,
        requested_at=requested,
        blind_review=False,
    )


@pytest.mark.parametrize("minutes_ago", [21, 45, 600])
@pytest.mark.asyncio
async def test_an_old_pending_row_is_reported_failed_stalled(
    minutes_ago: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    conn = _conn(fetchrow=_pending(minutes_ago))

    body = await _get(_build_app(conn), entry_id)

    assert body["status"] == "failed"
    assert body["reason"] == "stalled"
    assert body["questions"] == []
    conn.execute.assert_not_awaited()


@pytest.mark.parametrize("minutes_ago", [0, 1, 19])
@pytest.mark.asyncio
async def test_a_fresh_pending_row_is_still_pending(
    minutes_ago: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry_id = uuid4()
    _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))

    body = await _get(_build_app(_conn(fetchrow=_pending(minutes_ago))), entry_id)

    assert (body["status"], body["reason"]) == ("pending", None)


@pytest.mark.asyncio
async def test_a_pending_row_with_no_timestamp_is_not_called_stalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = uuid4()
    _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    body = await _get(_build_app(_conn(fetchrow=_pending(None))), entry_id)
    assert body["status"] == "pending"


@pytest.mark.asyncio
async def test_the_stall_window_comes_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hard-coded 20 would pass the cases above; a 16-minute-old row with a
    15-minute window must be stalled (15 is above the honest-window floor of
    13 minutes at the default LLM timeout)."""
    from src.api.routes import shortlist as routes
    from src.settings import Settings

    entry_id = uuid4()
    _Wiring(monkeypatch, entry=_entry(entry_id=entry_id))
    for module in (routes, _iq()):
        monkeypatch.setattr(
            module,
            "get_settings",
            lambda: Settings(interview_questions_stall_minutes=15),
            raising=False,
        )

    body = await _get(_build_app(_conn(fetchrow=_pending(16))), entry_id)

    assert (body["status"], body["reason"]) == ("failed", "stalled")


# -- F7 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_get_side_hash_is_over_the_scrubbed_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker stores ``gaps_hash(redact_gaps(...))``; if GET hashed the raw
    requirement a set whose requirement names the candidate would read stale
    forever."""
    entry_id = uuid4()
    name = "Jane Smith"
    entry = _entry(
        entry_id=entry_id,
        requirements=[
            _req(REQ_PARTIAL, "partial", QUOTE),
            _req(f"{REQ_MISSING} for {name}'s team", "missing"),
        ],
    )
    _Wiring(monkeypatch, entry=entry)
    monkeypatch.setattr(
        _iq(),
        "load_identifiers",
        AsyncMock(return_value=(name, "jane@example.test", "604-555-0192")),
        raising=False,
    )
    stored = gaps_hash(
        redact_gaps(
            select_gaps(entry),
            name=name,
            email="jane@example.test",
            phone="604-555-0192",
        )
    )
    assert stored != gaps_hash(select_gaps(entry))
    app = _build_app(_conn(fetchrow=_row(gaps_sha=stored, blind=False)))

    body = await _get(app, entry_id)

    assert body["current_gaps_sha256"] == stored
    assert body["stale"] is False
