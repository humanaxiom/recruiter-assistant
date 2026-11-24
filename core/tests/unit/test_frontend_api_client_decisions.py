"""RED pin -- human decision record, slice S4: the ``api_client`` methods.

Contract (mirrors the interview-questions client functions):

* ``get_human_decisions(entry_id, *, client=None)`` is
  ``GET /shortlist/{id}/human-decisions`` and returns the parsed JSON body
  (``DecisionHistory``).
* ``record_human_decision(entry_id, decision, rationale, *, client=None)`` is
  ``POST /shortlist/{id}/human-decisions`` with the JSON body
  ``{"decision": ..., "rationale": ...}`` and nothing else (who decided comes
  from the session, never the body). 201 and 200 (idempotent repeat) both
  return the parsed body.
* ``get_job_latest_decisions(job_id, *, client=None)`` is
  ``GET /jobs/{id}/decisions/latest`` and returns the parsed list.

Status mapping: 404 -> ``NotFound``; 403 / 422 / other 4xx -> ``BadRequest``
carrying ``status_code`` and the parsed body as ``detail``; 5xx and
connect/timeout failures -> ``BackendUnavailable``. None takes ``reveal``
(shortlist reads are blind).
"""

from __future__ import annotations

import datetime as dt
import inspect
import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from src.schemas.decisions import DecisionBadge, DecisionHistory, DecisionOut

_NOW = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.UTC)
_RATIONALE = "Strong systems background; verified references."


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _json_handler(
    status_code: int, body: Any
) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return _handler


def _out() -> DecisionOut:
    return DecisionOut(
        id=uuid4(),
        job_id=uuid4(),
        resume_id=uuid4(),
        decision="accept",
        rationale=_RATIONALE,
        decided_by="jordan",
        decided_by_role="recruiter",
        decided_at=_NOW,
        snap_rank=2,
        snap_score_final=0.8,
        snap_blind_review=True,
    )


_CALLS: list[Callable[[httpx.Client], Any]] = [
    lambda c: api_client.get_human_decisions(uuid4(), client=c),
    lambda c: api_client.record_human_decision(uuid4(), "accept", _RATIONALE, client=c),
    lambda c: api_client.get_job_latest_decisions(uuid4(), client=c),
]


def test_get_human_decisions_issues_get_and_returns_the_history() -> None:
    entry_id = uuid4()
    out = _out()
    payload = DecisionHistory(latest=out, history=[out]).model_dump(mode="json")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json=payload)

    body = api_client.get_human_decisions(entry_id, client=_client_with(handler))
    assert seen == {
        "method": "GET",
        "path": f"/shortlist/{entry_id}/human-decisions",
    }
    assert body == payload


def test_get_human_decisions_empty_history_roundtrips() -> None:
    payload = DecisionHistory(latest=None, history=[]).model_dump(mode="json")
    body = api_client.get_human_decisions(
        uuid4(), client=_client_with(_json_handler(200, payload))
    )
    assert body == {"latest": None, "history": []}


@pytest.mark.parametrize("status_code", [201, 200])
def test_record_human_decision_posts_json_body_and_returns_dto(
    status_code: int,
) -> None:
    entry_id = uuid4()
    out = _out().model_dump(mode="json")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(status_code, json=out)

    body = api_client.record_human_decision(
        entry_id, "hold", _RATIONALE, client=_client_with(handler)
    )
    assert seen["method"] == "POST"
    assert seen["path"] == f"/shortlist/{entry_id}/human-decisions"
    assert seen["body"] == {"decision": "hold", "rationale": _RATIONALE}
    assert body == out


def test_record_human_decision_body_has_no_identity_fields() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_out().model_dump(mode="json"))

    api_client.record_human_decision(
        uuid4(), "reject", _RATIONALE, client=_client_with(handler)
    )
    assert set(seen["body"]) == {"decision", "rationale"}


def test_get_job_latest_decisions_issues_get_and_returns_the_list() -> None:
    job_id = uuid4()
    badge = DecisionBadge(
        resume_id=uuid4(), decision="reject", decided_by="jordan", decided_at=_NOW
    ).model_dump(mode="json")
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json=[badge])

    body = api_client.get_job_latest_decisions(job_id, client=_client_with(handler))
    assert seen == {
        "method": "GET",
        "path": f"/jobs/{job_id}/decisions/latest",
    }
    assert body == [badge]


def test_get_job_latest_decisions_empty_list() -> None:
    body = api_client.get_job_latest_decisions(
        uuid4(), client=_client_with(_json_handler(200, []))
    )
    assert body == []


@pytest.mark.parametrize("call", _CALLS)
def test_404_raises_not_found(call: Callable[[httpx.Client], Any]) -> None:
    with pytest.raises(api_client.NotFound):
        call(_client_with(_json_handler(404, {"detail": "nope"})))


@pytest.mark.parametrize("status_code", [500, 502, 503])
@pytest.mark.parametrize("call", _CALLS)
def test_5xx_raises_backend_unavailable(
    call: Callable[[httpx.Client], Any], status_code: int
) -> None:
    with pytest.raises(api_client.BackendUnavailable):
        call(_client_with(_json_handler(status_code, {"detail": "down"})))


@pytest.mark.parametrize("call", _CALLS)
def test_connect_error_raises_backend_unavailable(
    call: Callable[[httpx.Client], Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(api_client.BackendUnavailable):
        call(_client_with(handler))


@pytest.mark.parametrize("call", _CALLS)
def test_timeout_raises_backend_unavailable(
    call: Callable[[httpx.Client], Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(api_client.BackendUnavailable):
        call(_client_with(handler))


@pytest.mark.parametrize("status_code", [403, 422, 400])
def test_record_4xx_raises_bad_request_carrying_status_and_detail(
    status_code: int,
) -> None:
    body = {"detail": "rationale must be at least 10 characters"}
    client = _client_with(_json_handler(status_code, body))
    with pytest.raises(api_client.BadRequest) as info:
        api_client.record_human_decision(uuid4(), "accept", "short", client=client)
    assert info.value.status_code == status_code
    assert info.value.detail == body
    assert not isinstance(info.value, api_client.Conflict)


def test_get_human_decisions_403_raises_bad_request() -> None:
    client = _client_with(_json_handler(403, {"detail": "forbidden"}))
    with pytest.raises(api_client.BadRequest) as info:
        api_client.get_human_decisions(uuid4(), client=client)
    assert info.value.status_code == 403


@pytest.mark.parametrize(
    "name",
    ["get_human_decisions", "record_human_decision", "get_job_latest_decisions"],
)
def test_signatures_take_no_reveal_parameter(name: str) -> None:
    fn = getattr(api_client, name)
    assert "reveal" not in inspect.signature(fn).parameters


def test_record_signature_is_entry_decision_rationale() -> None:
    params = list(inspect.signature(api_client.record_human_decision).parameters)
    assert params[:3] == ["entry_id", "decision", "rationale"]
    assert "client" in params


def test_new_methods_are_exported_in_all() -> None:
    for name in (
        "get_human_decisions",
        "record_human_decision",
        "get_job_latest_decisions",
    ):
        assert name in api_client.__all__
