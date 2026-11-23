"""RED pin -- interview questions, slice S3: the ``api_client`` methods.

Contract: ``get_interview_questions(entry_id, *, client=None)`` is
``GET /shortlist/{id}/interview-questions`` and returns the parsed JSON body;
``request_interview_questions(entry_id, *, client=None)`` is the matching POST
(202 ``{"status": "pending"}``). Both take no ``reveal`` parameter (shortlist
reads are blind) and both propagate errors exactly like their siblings:
404 -> NotFound, 409 -> Conflict (a BadRequest subclass), 403 -> BadRequest,
5xx / connect failure -> BackendUnavailable.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from frontend import api_client


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


_CALLS: list[Callable[[httpx.Client], Any]] = [
    lambda c: api_client.get_interview_questions(uuid4(), client=c),
    lambda c: api_client.request_interview_questions(uuid4(), client=c),
]


def test_get_interview_questions_issues_get_to_the_resource() -> None:
    entry_id = uuid4()
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"status": "none"})

    body = api_client.get_interview_questions(entry_id, client=_client_with(handler))
    assert seen == {
        "method": "GET",
        "path": f"/shortlist/{entry_id}/interview-questions",
    }
    assert body == {"status": "none"}


def test_request_interview_questions_issues_post_and_returns_body() -> None:
    entry_id = uuid4()
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(202, json={"status": "pending"})

    body = api_client.request_interview_questions(
        entry_id, client=_client_with(handler)
    )
    assert seen == {
        "method": "POST",
        "path": f"/shortlist/{entry_id}/interview-questions",
    }
    assert body == {"status": "pending"}


def test_get_interview_questions_returns_the_ready_payload_unchanged() -> None:
    payload = {
        "status": "ready",
        "reason": None,
        "questions": [
            {
                "requirement_index": 0,
                "requirement": "Kubernetes",
                "question": "Tell me about a cluster you ran.",
                "probes_quote": None,
            }
        ],
        "stale": False,
        "generated_at": "2026-07-15T00:00:00Z",
    }
    out = api_client.get_interview_questions(
        uuid4(), client=_client_with(_json_handler(200, payload))
    )
    assert out == payload


@pytest.mark.parametrize("call", _CALLS)
def test_404_raises_not_found(call: Callable[[httpx.Client], Any]) -> None:
    with pytest.raises(api_client.NotFound):
        call(_client_with(_json_handler(404, {"detail": "nope"})))


@pytest.mark.parametrize("call", _CALLS)
def test_5xx_raises_backend_unavailable(call: Callable[[httpx.Client], Any]) -> None:
    with pytest.raises(api_client.BackendUnavailable):
        call(_client_with(_json_handler(503, {"detail": "down"})))


@pytest.mark.parametrize("call", _CALLS)
def test_connect_error_raises_backend_unavailable(
    call: Callable[[httpx.Client], Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(api_client.BackendUnavailable):
        call(_client_with(handler))


def test_post_409_raises_conflict() -> None:
    client = _client_with(_json_handler(409, {"detail": "already pending"}))
    with pytest.raises(api_client.Conflict) as info:
        api_client.request_interview_questions(uuid4(), client=client)
    assert info.value.status_code == 409


def test_post_403_raises_bad_request_with_status_code() -> None:
    client = _client_with(_json_handler(403, {"detail": "forbidden"}))
    with pytest.raises(api_client.BadRequest) as info:
        api_client.request_interview_questions(uuid4(), client=client)
    assert info.value.status_code == 403
    assert not isinstance(info.value, api_client.Conflict)


@pytest.mark.parametrize(
    "name", ["get_interview_questions", "request_interview_questions"]
)
def test_signatures_take_no_reveal_parameter(name: str) -> None:
    fn = getattr(api_client, name)
    assert "reveal" not in inspect.signature(fn).parameters


def test_new_methods_are_exported_in_all() -> None:
    assert "get_interview_questions" in api_client.__all__
    assert "request_interview_questions" in api_client.__all__
