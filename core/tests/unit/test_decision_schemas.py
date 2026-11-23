"""RED -- human decision record, slice S1: the wire schemas in
``src.schemas.decisions`` (``DecisionIn`` / ``DecisionOut`` / ``DecisionBadge`` /
``DecisionHistory``).

Contract (plan: human decision record):

* ``DecisionIn`` -- ``extra="forbid"`` (so a client-supplied ``decided_by`` is a
  422, identity comes from the session only); ``decision`` is exactly
  accept / reject / hold; ``rationale`` is stripped and must be 10..1000 chars
  AFTER the strip; control characters other than newline and tab are rejected.
* ``DecisionBadge`` carries NO rationale.
* ``DecisionHistory.latest`` is ``DecisionOut | None``; ``history`` is a list.

The module is imported lazily so a missing module fails each test on its own
instead of aborting collection of the unit suite.
"""

from __future__ import annotations

import datetime as dt
import importlib
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

_NOW = dt.datetime(2026, 11, 23, 12, 0, tzinfo=dt.UTC)
_GOOD = "Strong systems background, fits the team."


def _mod() -> Any:
    return importlib.import_module("src.schemas.decisions")


def _out_kwargs() -> dict[str, Any]:
    return {
        "id": uuid4(),
        "job_id": uuid4(),
        "resume_id": uuid4(),
        "decision": "accept",
        "rationale": _GOOD,
        "decided_by": "alice",
        "decided_by_role": "recruiter",
        "decided_at": _NOW,
        "snap_rank": 3,
        "snap_score_final": 0.71,
        "snap_blind_review": False,
    }


# -- DecisionIn: decision ---------------------------------------------------


@pytest.mark.parametrize("value", ["accept", "reject", "hold"])
def test_decision_in_accepts_each_of_the_three_values(value: str) -> None:
    body = _mod().DecisionIn(decision=value, rationale=_GOOD)
    assert body.decision == value


@pytest.mark.parametrize(
    "value", ["", "Accept", "ACCEPT", "approve", "maybe", "advance", " accept", None]
)
def test_decision_in_rejects_any_other_value(value: Any) -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionIn(decision=value, rationale=_GOOD)


def test_decision_in_requires_both_fields() -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionIn(decision="accept")
    with pytest.raises(ValidationError):
        _mod().DecisionIn(rationale=_GOOD)


# -- DecisionIn: rationale length ------------------------------------------


def test_rationale_of_exactly_ten_chars_is_accepted() -> None:
    assert _mod().DecisionIn(decision="hold", rationale="a" * 10).rationale == "a" * 10


def test_rationale_of_nine_chars_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionIn(decision="hold", rationale="a" * 9)


def test_rationale_of_exactly_1000_chars_is_accepted() -> None:
    body = _mod().DecisionIn(decision="hold", rationale="a" * 1000)
    assert len(body.rationale) == 1000


def test_rationale_of_1001_chars_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionIn(decision="hold", rationale="a" * 1001)


def test_rationale_is_stripped_before_the_length_check() -> None:
    body = _mod().DecisionIn(decision="reject", rationale=f"   {_GOOD}\n\t ")
    assert body.rationale == _GOOD


def test_whitespace_padding_cannot_satisfy_the_minimum() -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionIn(decision="reject", rationale="   short   " + " " * 20)


@pytest.mark.parametrize("blank", ["", "          ", "\n" * 15, "\t \n" * 8])
def test_blank_rationale_is_rejected(blank: str) -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionIn(decision="accept", rationale=blank)


def test_thousand_chars_plus_padding_is_still_valid_after_strip() -> None:
    padded = " " * 5 + "a" * 1000 + " " * 5
    body = _mod().DecisionIn(decision="accept", rationale=padded)
    assert body.rationale == "a" * 1000


# -- DecisionIn: control characters ----------------------------------------


def test_newline_and_tab_inside_the_rationale_are_allowed() -> None:
    text = "First line of reasoning.\n\tSecond line, indented."
    assert _mod().DecisionIn(decision="accept", rationale=text).rationale == text


@pytest.mark.parametrize(
    "code", [0, 1, 7, 8, 11, 12, 13, 27, 127], ids=lambda c: f"chr{c}"
)
def test_control_characters_are_rejected(code: int) -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionIn(
            decision="accept", rationale=f"Good fit {chr(code)} for the team"
        )


# -- DecisionIn: no client-supplied identity -------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"decided_by": "mallory"},
        {"decided_by_role": "admin"},
        {"decided_by_user_id": str(uuid4())},
        {"decided_at": "2020-01-01T00:00:00Z"},
        {"snap_rank": 1},
        {"anything_else": 1},
    ],
)
def test_decision_in_forbids_extra_fields(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionIn(decision="accept", rationale=_GOOD, **extra)


def test_decision_in_from_json_rejects_decided_by() -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionIn.model_validate_json(
            '{"decision":"accept","rationale":"Good fit for the role","decided_by":"x"}'
        )


# -- DecisionOut / DecisionBadge / DecisionHistory -------------------------


def test_decision_out_round_trips_json() -> None:
    out = _mod().DecisionOut(**_out_kwargs())
    dumped = out.model_dump(mode="json")
    assert isinstance(dumped["decided_at"], str)
    assert dumped["rationale"] == _GOOD
    assert _mod().DecisionOut.model_validate(dumped) == out


def test_decision_out_field_set_is_exactly_the_contract() -> None:
    assert set(_mod().DecisionOut.model_fields) == {
        "id",
        "job_id",
        "resume_id",
        "decision",
        "rationale",
        "decided_by",
        "decided_by_role",
        "decided_at",
        "snap_rank",
        "snap_score_final",
        "snap_blind_review",
    }


def test_decision_out_snapshot_fields_may_be_none() -> None:
    kw = _out_kwargs() | {"snap_rank": None, "snap_score_final": None}
    out = _mod().DecisionOut(**kw)
    assert out.snap_rank is None and out.snap_score_final is None


def test_decision_out_does_not_expose_the_internal_ids() -> None:
    fields = set(_mod().DecisionOut.model_fields)
    assert "seq" not in fields
    assert "decided_by_user_id" not in fields
    assert "snap_entry_id" not in fields


def test_decision_badge_field_set_has_no_rationale() -> None:
    assert set(_mod().DecisionBadge.model_fields) == {
        "resume_id",
        "decision",
        "decided_by",
        "decided_at",
    }


def test_decision_badge_dump_never_contains_rationale() -> None:
    badge = _mod().DecisionBadge(
        resume_id=uuid4(), decision="hold", decided_by="bob", decided_at=_NOW
    )
    assert "rationale" not in badge.model_dump(mode="json")


def test_decision_history_with_no_decisions() -> None:
    h = _mod().DecisionHistory(latest=None, history=[])
    assert h.latest is None and h.history == []


def test_decision_history_holds_latest_and_newest_first_list() -> None:
    newer = _mod().DecisionOut(**(_out_kwargs() | {"decision": "reject"}))
    older = _mod().DecisionOut(**_out_kwargs())
    h = _mod().DecisionHistory(latest=newer, history=[newer, older])
    dumped = h.model_dump(mode="json")
    assert dumped["latest"]["decision"] == "reject"
    assert [d["decision"] for d in dumped["history"]] == ["reject", "accept"]


def test_decision_history_requires_history() -> None:
    with pytest.raises(ValidationError):
        _mod().DecisionHistory(latest=None)
