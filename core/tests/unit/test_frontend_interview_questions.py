"""RED pin -- interview questions, slice S3: Flask routes and the entry panel.

Contract (the coder follows these names):

* ``POST /shortlist/<entry_id>/interview-questions`` -- endpoint
  ``request_interview_questions``. Guarded by the ORDINARY ``_csrf_gate``
  page token (NOT exempt, NOT a per-card one-shot slot: the session's
  ``MAX_TOKENS_PER_SESSION = 64`` is a shared budget). PRG: on success and on
  409 it redirects to ``/shortlist/<entry_id>`` (409 adds a flash); 404 -> 404;
  a backend 403 (``BadRequest``) is handled, never a 500.
* ``GET /shortlist/<entry_id>/interview-questions-panel`` -- endpoint
  ``interview_questions_panel``. Returns ONLY the panel fragment (no ``<html``).
  The panel root has ``id="interview-questions"``; while pending it carries
  ``hx-get`` of this URL with ``hx-trigger="every 3s"``, and no other state
  carries an ``every`` trigger (so polling stops by itself).
* ``shortlist_entry.html`` includes the same panel, fed by
  ``api_client.get_interview_questions``. A failure to fetch it must NOT break
  the entry page.

Payloads are built from ``InterviewQuestionsResponse.model_dump(mode="json")``
-- the frontend sees JSON strings, never datetimes.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from frontend import api_client, csrf
from frontend import app as frontend_app_module
from frontend.app import app
from src.api.routes.shortlist import (
    InterviewQuestionOut,
    InterviewQuestionsResponse,
)
from src.schemas.matching import ScoreBreakdown, ShortlistEntry
from src.settings import Settings
from tests.unit.conftest import TEST_PAGE_TOKEN

_NOW = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _entry(entry_id: UUID) -> dict[str, Any]:
    return ShortlistEntry(
        id=entry_id,
        job_id=uuid4(),
        resume_id=uuid4(),
        rank=1,
        score_final=0.75,
        score_breakdown=ScoreBreakdown(
            skill=0.7,
            experience=0.6,
            education=0.5,
            seniority=0.5,
            vector=0.4,
            structured=0.55,
        ),
        evidence=None,
        generated_at=_NOW,
        blinded=True,
        display_label="Candidate A",
    ).model_dump(mode="json")


def _q(idx: int, requirement: str, question: str) -> InterviewQuestionOut:
    return InterviewQuestionOut(
        requirement_index=idx, requirement=requirement, question=question
    )


def _payload(
    status: str = "none",
    *,
    reason: str | None = None,
    questions: list[InterviewQuestionOut] | None = None,
    stale: bool = False,
    generated_at: dt.datetime | None = None,
) -> dict[str, Any]:
    return InterviewQuestionsResponse(
        status=status,
        reason=reason,
        questions=questions or [],
        stale=stale,
        generated_at=generated_at,
    ).model_dump(mode="json")


def _ready(
    *,
    questions: list[InterviewQuestionOut] | None = None,
    stale: bool = False,
) -> dict[str, Any]:
    qs = (
        questions
        if questions is not None
        else [
            _q(0, "Kubernetes in production", "Walk me through a cluster you ran."),
            _q(2, "Terraform", "How did you structure your modules?"),
        ]
    )
    return _payload("ready", questions=qs, stale=stale, generated_at=_NOW)


def _stub(monkeypatch: Any, entry_id: UUID, iq: Any) -> MagicMock:
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=_entry(entry_id))
    )
    if isinstance(iq, Exception):
        mock = MagicMock(side_effect=iq)
    else:
        mock = MagicMock(return_value=iq)
    monkeypatch.setattr(api_client, "get_interview_questions", mock)
    return mock


def _page(client: Any, entry_id: UUID) -> str:
    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _panel(client: Any, entry_id: UUID) -> str:
    resp = client.get(f"/shortlist/{entry_id}/interview-questions-panel")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _cas(monkeypatch: Any, role: str) -> dict[str, str]:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client,
        "get_cas_user",
        MagicMock(
            return_value={
                "authenticated": True,
                "username": "jordan",
                "cas_enabled": True,
                "role": role,
            }
        ),
    )
    return {"Cookie": "ra_session=tok-live"}


# -- panel states, via the full page ------------------------------------------


def test_none_state_shows_generate_button_to_a_writer(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, {"status": "none"})
    body = _page(client, eid)
    assert 'id="interview-questions"' in body
    assert "Generate interview questions" in body
    assert f'action="/shortlist/{eid}/interview-questions"' in body


def test_post_form_carries_the_shared_page_token_not_a_one_shot_slot(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, {"status": "none"})
    body = _page(client, eid)
    form = re.search(
        r"<form[^>]*interview-questions\"[^>]*>.*?</form>", body, re.DOTALL
    )
    assert form is not None
    assert TEST_PAGE_TOKEN in form.group(0)
    assert 'name="csrf_token"' in form.group(0)
    with client.session_transaction() as session:
        assert not session.get(csrf.SESSION_KEY)


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_none_state_has_no_button_for_non_writers(
    monkeypatch: Any, client: Any, role: str
) -> None:
    headers = _cas(monkeypatch, role)
    eid = uuid4()
    _stub(monkeypatch, eid, {"status": "none"})
    body = client.get(f"/shortlist/{eid}", headers=headers).get_data(as_text=True)
    assert "Generate interview questions" not in body
    assert f"/shortlist/{eid}/interview-questions" not in body


def test_pending_state_shows_message_and_polls_the_panel_endpoint(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _payload("pending"))
    body = _page(client, eid)
    assert re.search(r"generating|in progress|working", body, re.IGNORECASE)
    assert f"/shortlist/{eid}/interview-questions-panel" in body
    assert re.search(r'hx-trigger="[^"]*every\s+\d+s', body)
    assert "Generate interview questions" not in body


@pytest.mark.parametrize(
    "iq",
    [
        {"status": "none"},
        _payload("failed", reason="invalid_output"),
        _payload("refused", reason="no_gaps"),
        _ready(),
    ],
)
def test_only_pending_polls(monkeypatch: Any, client: Any, iq: dict[str, Any]) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, iq)
    panel = _panel(client, eid)
    assert not re.search(r"every\s+\d+s", panel)


def test_ready_state_groups_questions_by_requirement(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    qs = [
        _q(0, "Kubernetes in production", "Question one about clusters?"),
        _q(0, "Kubernetes in production", "Question two about upgrades?"),
        _q(2, "Terraform", "Question three about modules?"),
    ]
    _stub(monkeypatch, eid, _ready(questions=qs))
    body = _page(client, eid)
    assert body.count("Kubernetes in production") == 1
    assert body.count("Terraform") == 1
    for text in ("one about clusters", "two about upgrades", "three about modules"):
        assert text in body
    assert body.index("Kubernetes in production") < body.index("Terraform")
    assert "Generate interview questions" not in body


def test_ready_panel_with_iso_string_generated_at_renders_200(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    iq = _ready()
    assert isinstance(iq["generated_at"], str)
    _stub(monkeypatch, eid, iq)
    body = _page(client, eid)
    panel = body.split('id="interview-questions"', 1)[1]
    assert "2026-07-15" in panel
    assert "T12:00:00" not in panel


def test_ready_copy_says_not_evidenced_never_lacks(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _ready())
    panel = _panel(client, eid)
    assert "not evidenced in the résumé" in panel
    assert not re.search(r"\black", panel, re.IGNORECASE)


def test_question_markup_is_escaped(monkeypatch: Any, client: Any) -> None:
    eid = uuid4()
    qs = [_q(0, "Req <b>bold</b>", "<script>alert(1)</script> why?")]
    _stub(monkeypatch, eid, _ready(questions=qs))
    for body in (_page(client, eid), _panel(client, eid)):
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
        assert "<b>bold</b>" not in body


@pytest.mark.parametrize(
    ("status", "reason", "needle"),
    [
        ("refused", "not_evaluated", "evaluated"),
        ("refused", "no_requirements", "requirement"),
        ("refused", "no_gaps", "evidenced"),
        ("failed", "invalid_output", "try again"),
        ("failed", "no_questions", "try again"),
        ("failed", "entry_gone", "no longer"),
    ],
)
def test_failed_and_refused_render_human_copy_not_the_raw_enum(
    monkeypatch: Any, client: Any, status: str, reason: str, needle: str
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _payload(status, reason=reason))
    panel = _panel(client, eid)
    assert reason not in panel
    assert needle in panel.lower()


def test_unknown_reason_renders_generic_copy_not_the_raw_value(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _payload("failed", reason="brand_new_reason"))
    panel = _panel(client, eid)
    assert "brand_new_reason" not in panel
    assert "interview questions" in panel.lower()


@pytest.mark.parametrize(
    ("status", "reason"),
    [("failed", "invalid_output"), ("refused", "no_gaps")],
)
def test_failed_or_refused_has_retry_button_for_writer(
    monkeypatch: Any, client: Any, status: str, reason: str
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _payload(status, reason=reason))
    panel = _panel(client, eid)
    assert re.search(r"retry|try again", panel, re.IGNORECASE)
    assert f'action="/shortlist/{eid}/interview-questions"' in panel


def test_failed_has_no_retry_for_hiring_manager(monkeypatch: Any, client: Any) -> None:
    headers = _cas(monkeypatch, "hiring_manager")
    eid = uuid4()
    _stub(monkeypatch, eid, _payload("failed", reason="invalid_output"))
    body = client.get(
        f"/shortlist/{eid}/interview-questions-panel", headers=headers
    ).get_data(as_text=True)
    assert "<form" not in body


def test_stale_banner_and_regenerate_for_writer(monkeypatch: Any, client: Any) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _ready(stale=True))
    panel = _panel(client, eid)
    assert "The evidence has changed since these questions were generated" in panel
    assert "Regenerate" in panel
    assert "Kubernetes in production" in panel


def test_fresh_ready_has_no_stale_banner(monkeypatch: Any, client: Any) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _ready(stale=False))
    assert "evidence has changed" not in _panel(client, eid)


def test_hiring_manager_sees_ready_questions_but_no_button(
    monkeypatch: Any, client: Any
) -> None:
    headers = _cas(monkeypatch, "hiring_manager")
    eid = uuid4()
    _stub(monkeypatch, eid, _ready(stale=True))
    body = client.get(f"/shortlist/{eid}", headers=headers).get_data(as_text=True)
    assert "Kubernetes in production" in body
    assert "evidence has changed" in body
    assert "Regenerate" not in body
    assert f"/shortlist/{eid}/interview-questions" not in body


def test_entry_page_survives_the_questions_fetch_failing(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, api_client.BackendUnavailable("down"))
    resp = client.get(f"/shortlist/{eid}")
    assert resp.status_code == 200
    assert "Candidate A" in resp.get_data(as_text=True)


# -- the panel partial -------------------------------------------------------


def test_panel_partial_is_a_fragment_not_a_page(monkeypatch: Any, client: Any) -> None:
    eid = uuid4()
    _stub(monkeypatch, eid, _ready())
    panel = _panel(client, eid)
    assert "<html" not in panel.lower()
    assert "Kubernetes in production" in panel


def test_panel_partial_unknown_entry_is_404(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "get_interview_questions",
        MagicMock(side_effect=api_client.NotFound("x")),
    )
    resp = client.get(f"/shortlist/{uuid4()}/interview-questions-panel")
    assert resp.status_code == 404


def test_panel_partial_backend_down_is_not_a_500(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "get_interview_questions",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    resp = client.get(f"/shortlist/{uuid4()}/interview-questions-panel")
    assert resp.status_code in (200, 502, 503)


# -- the POST route ----------------------------------------------------------


def test_post_triggers_generation_and_redirects_back(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    mock = MagicMock(return_value={"status": "pending"})
    monkeypatch.setattr(api_client, "request_interview_questions", mock)
    resp = client.post(f"/shortlist/{eid}/interview-questions")
    assert resp.status_code in (302, 303)
    assert resp.headers["Location"].endswith(f"/shortlist/{eid}")
    mock.assert_called_once()
    assert mock.call_args.args[0] == eid


def test_post_without_csrf_token_is_403_and_never_calls_backend(
    monkeypatch: Any,
) -> None:
    app.config.update(TESTING=True)
    bare = app.test_client()
    mock = MagicMock(return_value={"status": "pending"})
    monkeypatch.setattr(api_client, "request_interview_questions", mock)
    resp = bare.post(f"/shortlist/{uuid4()}/interview-questions")
    assert resp.status_code == 403
    mock.assert_not_called()


def test_post_accepts_the_page_token_as_a_form_field(monkeypatch: Any) -> None:
    app.config.update(TESTING=True)
    c = app.test_client()
    with c.session_transaction() as session:
        session[csrf.PAGE_SESSION_KEY] = TEST_PAGE_TOKEN
    mock = MagicMock(return_value={"status": "pending"})
    monkeypatch.setattr(api_client, "request_interview_questions", mock)
    resp = c.post(
        f"/shortlist/{uuid4()}/interview-questions",
        data={csrf.FORM_FIELD: TEST_PAGE_TOKEN},
    )
    assert resp.status_code in (302, 303)
    mock.assert_called_once()


def test_post_is_not_exempt_from_the_csrf_hook() -> None:
    exempt = frontend_app_module._CSRF_HOOK_EXEMPT_ENDPOINTS
    assert "request_interview_questions" not in exempt


def test_post_conflict_redirects_with_a_flash(monkeypatch: Any, client: Any) -> None:
    eid = uuid4()
    monkeypatch.setattr(
        api_client,
        "request_interview_questions",
        MagicMock(
            side_effect=api_client.Conflict(
                "409", status_code=409, detail={"detail": "pending"}
            )
        ),
    )
    resp = client.post(f"/shortlist/{eid}/interview-questions")
    assert resp.status_code in (302, 303)
    assert resp.headers["Location"].endswith(f"/shortlist/{eid}")
    with client.session_transaction() as session:
        flashes = [m for _, m in session.get("_flashes", [])]
    assert flashes
    assert any("already" in m.lower() for m in flashes)


def test_post_unknown_entry_is_404(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "request_interview_questions",
        MagicMock(side_effect=api_client.NotFound("x")),
    )
    assert client.post(f"/shortlist/{uuid4()}/interview-questions").status_code == 404


def test_post_backend_403_is_handled_not_a_500(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "request_interview_questions",
        MagicMock(
            side_effect=api_client.BadRequest(
                "403", status_code=403, detail={"detail": "forbidden"}
            )
        ),
    )
    try:
        resp = client.post(f"/shortlist/{uuid4()}/interview-questions")
    except api_client.BadRequest:
        pytest.fail("BadRequest (backend 403) escaped the route")
    assert resp.status_code < 500


def test_post_backend_down_is_not_an_unhandled_error(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "request_interview_questions",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    try:
        resp = client.post(f"/shortlist/{uuid4()}/interview-questions")
    except api_client.BackendUnavailable:
        pytest.fail("BackendUnavailable escaped the route")
    assert resp.status_code in (302, 303, 502, 503)


def test_routes_are_registered_with_expected_methods() -> None:
    rules = {
        r.endpoint: r
        for r in app.url_map.iter_rules()
        if r.endpoint in ("request_interview_questions", "interview_questions_panel")
    }
    assert set(rules) == {"request_interview_questions", "interview_questions_panel"}
    assert "POST" in rules["request_interview_questions"].methods
    assert "GET" in rules["interview_questions_panel"].methods
