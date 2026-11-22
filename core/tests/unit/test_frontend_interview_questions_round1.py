"""RED -- interview-question generator, review round 1: the Flask panel.

* **F4.** A failed questions fetch (any ``BackendError`` -- 5xx, a 4xx such as
  the backend's 401/403) is its own ``unavailable`` state, NOT ``none``: the
  panel says the questions "could not be loaded" and offers NO form or button
  to anyone, writers included (a "Generate" button on a panel that failed to
  load would invite a duplicate POST for an entry that may already have a set).
  The panel route catches ``api_client.BadRequest`` and renders, never a 500.
* **F1.** The API's ``ready`` + ``reason="blind_changed"`` payload renders the
  "regenerate" copy, none of the question text, and a Regenerate form for a
  writer only.
* **F2 / F3.** ``llm_unavailable`` and ``stalled`` render "try again" copy and a
  Retry form for a writer.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import pytest

from frontend import api_client
from tests.unit.test_frontend_interview_questions import (
    _cas,
    _page,
    _panel,
    _payload,
    _q,
    _ready,
    _stub,
)


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


_ERRORS = [
    api_client.BackendUnavailable("backend down"),
    api_client.BadRequest("forbidden", status_code=403, detail="forbidden"),
    api_client.BadRequest("unauthorised", status_code=401, detail="no session"),
    api_client.BackendError("something else"),
]
_ERROR_IDS = ["unavailable_5xx", "forbidden_403", "unauthorised_401", "base_error"]


# -- F4 ---------------------------------------------------------------------


@pytest.mark.parametrize("error", _ERRORS, ids=_ERROR_IDS)
def test_a_failed_fetch_on_the_entry_page_says_could_not_be_loaded(
    monkeypatch: Any, client: Any, error: Exception
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, error)
    body = _page(client, eid)
    panel = body.split('id="interview-questions"', 1)[1]
    assert "could not be loaded" in panel.lower()


@pytest.mark.parametrize("error", _ERRORS, ids=_ERROR_IDS)
def test_a_failed_fetch_offers_no_form_or_button_to_a_writer(
    monkeypatch: Any, client: Any, error: Exception
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, error)
    body = _page(client, eid)
    panel = body.split('id="interview-questions"', 1)[1]
    assert "<form" not in panel.split("</div>", 1)[0]
    assert "Generate interview questions" not in body
    assert f'action="/shortlist/{eid}/interview-questions"' not in body
    assert not re.search(r"every\s+\d+s", panel)


@pytest.mark.parametrize("role", ["admin", "recruiter", "hiring_manager", "auditor"])
def test_a_failed_fetch_offers_no_form_to_any_role(
    monkeypatch: Any, client: Any, role: str
) -> None:
    headers = _cas(monkeypatch, role)
    eid = uuid4()
    _stub(monkeypatch, eid, api_client.BackendUnavailable("down"))
    body = client.get(f"/shortlist/{eid}", headers=headers).get_data(as_text=True)
    assert "could not be loaded" in body.lower()
    assert f"/shortlist/{eid}/interview-questions" not in body


@pytest.mark.parametrize(
    "error",
    [
        api_client.BadRequest("forbidden", status_code=403, detail="forbidden"),
        api_client.BadRequest("unauthorised", status_code=401, detail="x"),
        api_client.BackendError("something else"),
    ],
    ids=["403", "401", "base_error"],
)
def test_the_panel_route_renders_instead_of_500ing_on_a_backend_rejection(
    monkeypatch: Any, client: Any, error: Exception
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, error)
    resp = client.get(f"/shortlist/{eid}/interview-questions-panel")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'id="interview-questions"' in body
    assert "could not be loaded" in body.lower()
    assert "<form" not in body
    assert not re.search(r"every\s+\d+s", body)


def test_a_none_state_still_offers_generate_to_a_writer(
    monkeypatch: Any, client: Any
) -> None:
    """Guard against the F4 fix swallowing the genuine empty state."""
    eid = uuid4()
    _stub(monkeypatch, eid, {"status": "none"})
    body = _page(client, eid)
    assert "Generate interview questions" in body
    assert "could not be loaded" not in body.lower()


# -- F1: blind_changed --------------------------------------------------------


def _blind_changed(*, with_questions: bool = False) -> dict[str, Any]:
    qs = [_q(0, "Kubernetes in production", "Walk me through Acme Corp.")]
    return _payload(
        "ready",
        reason="blind_changed",
        questions=qs if with_questions else [],
        stale=True,
    )


def test_blind_changed_tells_the_writer_to_regenerate(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _blind_changed())
    panel = _panel(client, eid).lower()
    assert "blind" in panel
    assert "regenerate" in panel
    assert "blind_changed" not in panel
    assert f'action="/shortlist/{eid}/interview-questions"' in panel
    assert not re.search(r"every\s+\d+s", panel)


def test_blind_changed_renders_no_question_text_even_if_the_payload_has_some(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _blind_changed(with_questions=True))
    for body in (_panel(client, eid), _page(client, eid)):
        assert "Acme Corp" not in body
        assert "Kubernetes in production" not in body


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_blind_changed_has_no_form_for_non_writers(
    monkeypatch: Any, client: Any, role: str
) -> None:
    headers = _cas(monkeypatch, role)
    eid = uuid4()
    _stub(monkeypatch, eid, _blind_changed())
    body = client.get(
        f"/shortlist/{eid}/interview-questions-panel", headers=headers
    ).get_data(as_text=True)
    assert "regenerate" in body.lower()
    assert "<form" not in body


def test_an_ordinary_stale_ready_set_is_not_mistaken_for_blind_changed(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _ready(stale=True))
    panel = _panel(client, eid)
    assert "Walk me through a cluster you ran." in panel
    assert "blind" not in panel.lower()


# -- F2 / F3: copy -----------------------------------------------------------


@pytest.mark.parametrize("reason", ["llm_unavailable", "stalled"])
def test_llm_unavailable_and_stalled_say_try_again_with_a_retry_for_writers(
    monkeypatch: Any, client: Any, reason: str
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _payload("failed", reason=reason))
    panel = _panel(client, eid)
    assert "try again" in panel.lower()
    assert reason not in panel
    assert "Retry" in panel
    assert f'action="/shortlist/{eid}/interview-questions"' in panel
    assert not re.search(r"every\s+\d+s", panel)


@pytest.mark.parametrize("reason", ["llm_unavailable", "stalled"])
def test_llm_unavailable_and_stalled_have_no_retry_for_a_hiring_manager(
    monkeypatch: Any, client: Any, reason: str
) -> None:
    headers = _cas(monkeypatch, "hiring_manager")
    eid = uuid4()
    _stub(monkeypatch, eid, _payload("failed", reason=reason))
    body = client.get(
        f"/shortlist/{eid}/interview-questions-panel", headers=headers
    ).get_data(as_text=True)
    assert "try again" in body.lower()
    assert "<form" not in body
