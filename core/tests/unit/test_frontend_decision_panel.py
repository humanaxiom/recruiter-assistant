"""RED pin -- human decision record, slice S4: Flask role helpers, the POST
route, the entry-page panel and the job shortlist badge.

Contract (from the plan's Frontend section; assumptions flagged inline):

* ``_DECIDER_ROLES = ("admin", "recruiter", "hiring_manager")`` beside the
  UNCHANGED ``_WRITER_ROLES``; the ``is_decider`` template variable is true for
  those roles and (fail-open, like ``is_writer``) when CAS is off.
* ``POST /shortlist/<uuid:entry_id>/decision-record`` takes the form fields
  ``decision`` and ``rationale`` and the ORDINARY page CSRF token; it calls
  ``api_client.record_human_decision(entry_id, decision, rationale)`` and PRGs
  to the entry page. ``BadRequest`` -> flash + redirect; ``NotFound`` -> 404;
  ``BackendUnavailable`` -> 503.
* The entry page renders the decision panel from
  ``api_client.get_human_decisions`` (tolerant: any ``BackendError`` -> an
  "unavailable" panel with NO form, page still 200).
* The job shortlist page renders a read-only badge per card from
  ``api_client.get_job_latest_decisions`` keyed by ``resume_id`` (tolerant),
  minting no per-card token.

Every payload is a real DTO passed through ``model_dump(mode="json")`` -- the
BFF sees JSON strings, never datetimes.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from flask import g

from frontend import api_client, csrf
from frontend import app as frontend_app_module
from frontend.app import app
from src.schemas.decisions import (
    DecisionBadge,
    DecisionHistory,
    DecisionOut,
    DecisionValue,
)
from src.schemas.matching import ScoreBreakdown, ShortlistEntry
from src.settings import Settings
from tests.unit.conftest import TEST_PAGE_TOKEN
from tests.unit.test_frontend_work_authorization_on_shortlist import _session_mapping

_NOW = dt.datetime(2026, 7, 15, 12, 0, tzinfo=dt.UTC)
_NOTE = "Recording a decision does not change the candidate's rank or score."
_RATIONALE = "Verified two references; strong match on the must-haves."


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


# -- builders (real DTOs, JSON-mode) -------------------------------------------


def _entry(
    entry_id: UUID,
    *,
    job_id: UUID | None = None,
    resume_id: UUID | None = None,
    rank: int = 1,
    label: str = "Candidate A",
    blinded: bool = True,
) -> dict[str, Any]:
    return ShortlistEntry(
        id=entry_id,
        job_id=job_id or uuid4(),
        resume_id=resume_id or uuid4(),
        rank=rank,
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
        blinded=blinded,
        display_label=label,
    ).model_dump(mode="json")


def _decision(
    *,
    decision: DecisionValue = "accept",
    rationale: str = _RATIONALE,
    decided_by: str = "reviewer-alpha",
    role: str = "hiring_manager",
    at: dt.datetime = _NOW,
    snap_rank: int | None = 7,
    snap_score: float | None = 0.8125,
    blind: bool = True,
) -> DecisionOut:
    return DecisionOut(
        id=uuid4(),
        job_id=uuid4(),
        resume_id=uuid4(),
        decision=decision,
        rationale=rationale,
        decided_by=decided_by,
        decided_by_role=role,
        decided_at=at,
        snap_rank=snap_rank,
        snap_score_final=snap_score,
        snap_blind_review=blind,
    )


def _history(*items: DecisionOut) -> dict[str, Any]:
    """``items`` newest first, as the backend returns them."""
    return DecisionHistory(
        latest=items[0] if items else None, history=list(items)
    ).model_dump(mode="json")


def _badge(resume_id: UUID, decision: DecisionValue, decided_by: str) -> dict[str, Any]:
    return DecisionBadge(
        resume_id=resume_id, decision=decision, decided_by=decided_by, decided_at=_NOW
    ).model_dump(mode="json")


def _stub_entry_page(monkeypatch: Any, entry_id: UUID, history: Any) -> None:
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=_entry(entry_id))
    )
    # Interview panel in the benign "none" state: it must not be the source of
    # any "unavailable" text these tests look for.
    monkeypatch.setattr(
        api_client,
        "get_interview_questions",
        MagicMock(return_value={"status": "none"}),
    )
    if isinstance(history, Exception):
        mock = MagicMock(side_effect=history)
    else:
        mock = MagicMock(return_value=history)
    monkeypatch.setattr(api_client, "get_human_decisions", mock, raising=False)


def _page(client: Any, entry_id: UUID, **kw: Any) -> str:
    resp = client.get(f"/shortlist/{entry_id}", **kw)
    assert resp.status_code == 200
    return html.unescape(resp.get_data(as_text=True))


def _cas(monkeypatch: Any, role: str | None) -> dict[str, str]:
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


def _decision_form(body: str, entry_id: UUID) -> str | None:
    m = re.search(
        rf"<form[^>]*/shortlist/{entry_id}/decision-record[^>]*>.*?</form>",
        body,
        re.DOTALL,
    )
    return m.group(0) if m else None


# -- role sets and the is_decider context variable -----------------------------


def test_decider_roles_are_admin_recruiter_hiring_manager() -> None:
    assert tuple(frontend_app_module._DECIDER_ROLES) == (
        "admin",
        "recruiter",
        "hiring_manager",
    )


def test_writer_roles_are_unchanged_hiring_manager_is_not_a_writer() -> None:
    assert frontend_app_module._WRITER_ROLES == ("admin", "recruiter")
    assert "hiring_manager" not in frontend_app_module._WRITER_ROLES


def _context_for(cas_user: dict[str, Any] | None) -> dict[str, Any]:
    with app.test_request_context("/"):
        g.cas_user = cas_user
        ctx: dict[str, Any] = frontend_app_module.inject_current_user()
        return ctx


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("admin", True),
        ("recruiter", True),
        ("hiring_manager", True),
        ("auditor", False),
        (None, False),
        ("mystery", False),
    ],
)
def test_is_decider_by_role(role: str | None, expected: bool) -> None:
    ctx = _context_for({"authenticated": True, "username": "j", "role": role})
    assert ctx["is_decider"] is expected


def test_is_decider_true_when_cas_is_off() -> None:
    assert _context_for(None)["is_decider"] is True


def test_hiring_manager_is_decider_but_not_writer() -> None:
    user = {"authenticated": True, "username": "j", "role": "hiring_manager"}
    ctx = _context_for(user)
    assert ctx["is_decider"] is True
    assert ctx["is_writer"] is False


# -- POST /shortlist/<id>/decision-record --------------------------------------


class _Recorder:
    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self.calls: list[tuple[UUID, str, str]] = []
        self._result = (
            result if result is not None else _decision().model_dump(mode="json")
        )
        self._exc = exc

    def __call__(
        self,
        entry_id: UUID,
        decision: str,
        rationale: str,
        *,
        client: Any = None,
    ) -> Any:
        self.calls.append((entry_id, decision, rationale))
        if self._exc is not None:
            raise self._exc
        return self._result


def _install(monkeypatch: Any, rec: _Recorder) -> None:
    monkeypatch.setattr(api_client, "record_human_decision", rec, raising=False)


def _post(client: Any, entry_id: UUID, **form: str) -> Any:
    data = {"decision": "accept", "rationale": _RATIONALE}
    data.update(form)
    return client.post(f"/shortlist/{entry_id}/decision-record", data=data)


def test_post_records_with_the_forms_decision_and_rationale_then_redirects(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    rec = _Recorder()
    _install(monkeypatch, rec)
    text = "Not enough Kubernetes depth."
    resp = _post(client, eid, decision="reject", rationale=text)
    assert resp.status_code in (302, 303)
    assert resp.headers["Location"].endswith(f"/shortlist/{eid}")
    assert rec.calls == [(eid, "reject", text)]


@pytest.mark.parametrize("decision", ["accept", "reject", "hold"])
def test_post_passes_each_decision_value_through(
    monkeypatch: Any, client: Any, decision: str
) -> None:
    rec = _Recorder()
    _install(monkeypatch, rec)
    resp = _post(client, uuid4(), decision=decision)
    assert resp.status_code in (302, 303)
    assert [c[1] for c in rec.calls] == [decision]


def test_post_without_csrf_token_is_rejected_and_never_calls_backend(
    monkeypatch: Any,
) -> None:
    app.config.update(TESTING=True)
    bare = app.test_client()
    rec = _Recorder()
    _install(monkeypatch, rec)
    resp = _post(bare, uuid4())
    assert resp.status_code in (400, 403)
    assert rec.calls == []


def test_post_with_a_wrong_csrf_token_is_rejected(monkeypatch: Any) -> None:
    app.config.update(TESTING=True)
    c = app.test_client()
    with c.session_transaction() as session:
        session[csrf.PAGE_SESSION_KEY] = TEST_PAGE_TOKEN
    rec = _Recorder()
    _install(monkeypatch, rec)
    resp = c.post(
        f"/shortlist/{uuid4()}/decision-record",
        data={
            "decision": "accept",
            "rationale": _RATIONALE,
            csrf.FORM_FIELD: "not-the-token",
        },
    )
    assert resp.status_code in (400, 403)
    assert rec.calls == []


def test_post_accepts_the_page_token_as_a_form_field(monkeypatch: Any) -> None:
    app.config.update(TESTING=True)
    c = app.test_client()
    with c.session_transaction() as session:
        session[csrf.PAGE_SESSION_KEY] = TEST_PAGE_TOKEN
    rec = _Recorder()
    _install(monkeypatch, rec)
    resp = c.post(
        f"/shortlist/{uuid4()}/decision-record",
        data={
            "decision": "hold",
            "rationale": _RATIONALE,
            csrf.FORM_FIELD: TEST_PAGE_TOKEN,
        },
    )
    assert resp.status_code in (302, 303)
    assert len(rec.calls) == 1


def test_post_route_is_registered_and_not_csrf_exempt() -> None:
    rules = [
        r
        for r in app.url_map.iter_rules()
        if r.rule == "/shortlist/<uuid:entry_id>/decision-record"
    ]
    assert len(rules) == 1
    assert "POST" in rules[0].methods
    assert rules[0].endpoint not in frontend_app_module._CSRF_HOOK_EXEMPT_ENDPOINTS


def test_post_bad_request_flashes_its_message_and_redirects(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _install(
        monkeypatch,
        _Recorder(
            exc=api_client.BadRequest(
                "422",
                status_code=422,
                detail={"detail": "rationale must be at least 10 characters"},
            )
        ),
    )
    resp = _post(client, eid, rationale="short")
    assert resp.status_code in (302, 303)
    assert resp.headers["Location"].endswith(f"/shortlist/{eid}")
    with client.session_transaction() as session:
        flashes = [m for _, m in session.get("_flashes", [])]
    assert any("at least 10 characters" in m for m in flashes)


def test_post_backend_403_is_flashed_not_a_500(monkeypatch: Any, client: Any) -> None:
    _install(
        monkeypatch,
        _Recorder(
            exc=api_client.BadRequest(
                "403", status_code=403, detail={"detail": "forbidden"}
            )
        ),
    )
    resp = _post(client, uuid4())
    assert resp.status_code in (302, 303)
    with client.session_transaction() as session:
        assert session.get("_flashes")


def test_post_not_found_is_404(monkeypatch: Any, client: Any) -> None:
    rec = _Recorder(exc=api_client.NotFound("x"))
    _install(monkeypatch, rec)
    assert _post(client, uuid4()).status_code == 404
    assert len(rec.calls) == 1  # the route exists and asked the backend


def test_post_backend_unavailable_renders_the_unavailable_page(
    monkeypatch: Any, client: Any
) -> None:
    _install(monkeypatch, _Recorder(exc=api_client.BackendUnavailable("down")))
    assert _post(client, uuid4()).status_code == 503


# -- the entry-page panel ------------------------------------------------------


def test_panel_renders_latest_decision_with_all_its_fields(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    hist = _history(_decision())
    assert isinstance(hist["latest"]["decided_at"], str)
    _stub_entry_page(monkeypatch, eid, hist)
    body = _page(client, eid)
    assert re.search(r"accept", body, re.IGNORECASE)
    assert "reviewer-alpha" in body
    assert re.search(r"hiring[ _]manager", body, re.IGNORECASE)
    assert "2026-07-15" in body
    assert "T12:00:00" not in body
    assert _RATIONALE in body


def test_panel_shows_rank_and_score_at_the_time_not_the_current_ones(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history(_decision()))
    body = _page(client, eid)
    # snapshot rank 7 / score 0.8125 (the entry itself is rank 1 / 0.75).
    assert re.search(r"rank\D{0,30}7\b|#\s*7\b", body, re.IGNORECASE)
    assert re.search(r"0\.81|81", body)


def test_panel_without_snapshot_values_still_renders(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub_entry_page(
        monkeypatch, eid, _history(_decision(snap_rank=None, snap_score=None))
    )
    body = _page(client, eid)
    assert _RATIONALE in body
    assert "None" not in body


def test_panel_states_that_a_decision_does_not_change_rank_or_score(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history(_decision()))
    assert _NOTE in _page(client, eid)


def test_previous_decisions_are_listed_newest_first(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    newest = _decision(rationale="Newest rationale text here.", decided_by="rev-new")
    middle = _decision(
        decision="hold",
        rationale="Middle rationale text here.",
        decided_by="rev-mid",
        at=_NOW - dt.timedelta(days=2),
    )
    oldest = _decision(
        decision="reject",
        rationale="Oldest rationale text here.",
        decided_by="rev-old",
        at=_NOW - dt.timedelta(days=5),
    )
    _stub_entry_page(monkeypatch, eid, _history(newest, middle, oldest))
    body = _page(client, eid)
    heading = body.index("Previous decisions")
    assert body.index("Newest rationale text here.") < heading
    assert heading < body.index("Middle rationale text here.")
    assert body.index("Middle rationale text here.") < body.index(
        "Oldest rationale text here."
    )
    assert "2026-07-10" in body  # the oldest, rendered via `| day` from a string
    assert "T12:00:00" not in body


def test_rationale_markup_is_escaped(monkeypatch: Any, client: Any) -> None:
    eid = uuid4()
    _stub_entry_page(
        monkeypatch,
        eid,
        _history(_decision(rationale="<script>alert(1)</script> but fine overall")),
    )
    raw = client.get(f"/shortlist/{eid}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in raw


def test_decider_sees_the_form_with_the_required_controls(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history(_decision()))
    form = _decision_form(_page(client, eid), eid)
    assert form is not None
    assert re.search(r'method="post"', form, re.IGNORECASE)
    for value in ("accept", "reject", "hold"):
        assert re.search(
            rf'<input[^>]*type="radio"[^>]*value="{value}"|'
            rf'<input[^>]*value="{value}"[^>]*type="radio"',
            form,
        ), value
    assert re.search(r'name="decision"', form)
    textarea = re.search(r"<textarea[^>]*>", form)
    assert textarea is not None
    assert 'name="rationale"' in textarea.group(0)
    assert 'minlength="10"' in textarea.group(0)
    assert 'maxlength="1000"' in textarea.group(0)


def test_form_carries_the_shared_page_token_and_mints_no_slot(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history(_decision()))
    form = _decision_form(_page(client, eid), eid)
    assert form is not None
    assert re.search(
        rf'<input[^>]*type="hidden"[^>]*name="csrf_token"[^>]*value="{TEST_PAGE_TOKEN}"|'
        rf'<input[^>]*name="csrf_token"[^>]*value="{TEST_PAGE_TOKEN}"',
        form,
    )
    assert _session_mapping(client) == {}


@pytest.mark.parametrize("role", ["admin", "recruiter", "hiring_manager"])
def test_every_decider_role_sees_the_form(
    monkeypatch: Any, client: Any, role: str
) -> None:
    headers = _cas(monkeypatch, role)
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history(_decision()))
    assert _decision_form(_page(client, eid, headers=headers), eid) is not None


def test_cas_off_dev_session_sees_the_form(monkeypatch: Any, client: Any) -> None:
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history())
    assert _decision_form(_page(client, eid), eid) is not None


@pytest.mark.parametrize("role", ["auditor", None])
def test_non_decider_sees_history_but_no_form(
    monkeypatch: Any, client: Any, role: str | None
) -> None:
    headers = _cas(monkeypatch, role)
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history(_decision()))
    body = _page(client, eid, headers=headers)
    assert _RATIONALE in body
    assert "reviewer-alpha" in body
    assert f"/shortlist/{eid}/decision-record" not in body
    assert 'name="rationale"' not in body


def test_hiring_manager_gets_the_form_but_no_writer_controls(
    monkeypatch: Any, client: Any
) -> None:
    headers = _cas(monkeypatch, "hiring_manager")
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history(_decision()))
    body = _page(client, eid, headers=headers)
    assert _decision_form(body, eid) is not None
    assert "Generate interview questions" not in body
    assert f"/shortlist/{eid}/interview-questions" not in body


def test_empty_history_shows_no_decision_state_and_the_form(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history())
    body = _page(client, eid)
    assert re.search(r"no decision\w*\b.{0,30}recorded", body, re.IGNORECASE)
    assert "Previous decisions" not in body
    assert _decision_form(body, eid) is not None
    assert "unavailable" not in body.lower()


def test_empty_history_for_an_auditor_shows_the_state_without_a_form(
    monkeypatch: Any, client: Any
) -> None:
    headers = _cas(monkeypatch, "auditor")
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history())
    body = _page(client, eid, headers=headers)
    assert re.search(r"no decision\w*\b.{0,30}recorded", body, re.IGNORECASE)
    assert _decision_form(body, eid) is None


@pytest.mark.parametrize(
    "exc",
    [
        api_client.BackendUnavailable("down"),
        api_client.NotFound("gone"),
        api_client.BadRequest("403", status_code=403, detail={"detail": "no"}),
    ],
)
def test_history_fetch_failure_degrades_to_an_unavailable_panel_with_no_form(
    monkeypatch: Any, client: Any, exc: Exception
) -> None:
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, exc)
    resp = client.get(f"/shortlist/{eid}")
    assert resp.status_code == 200
    body = html.unescape(resp.get_data(as_text=True))
    vars(api_client)["get_human_decisions"].assert_called_once()
    assert re.search(
        r"decision.{0,120}unavailable|unavailable.{0,120}decision",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    assert _decision_form(body, eid) is None
    assert f"/shortlist/{eid}/decision-record" not in body
    assert "Candidate A" in body


def test_non_dict_history_payload_never_offers_a_form_or_500s(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, ["not", "a", "dict"])
    resp = client.get(f"/shortlist/{eid}")
    assert resp.status_code == 200
    vars(api_client)["get_human_decisions"].assert_called_once()
    assert f"/shortlist/{eid}/decision-record" not in resp.get_data(as_text=True)


def test_entry_page_fetches_history_for_its_own_entry(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    _stub_entry_page(monkeypatch, eid, _history())
    _page(client, eid)
    mock: MagicMock = vars(api_client)["get_human_decisions"]
    mock.assert_called_once()
    assert mock.call_args.args[0] == eid


def test_blind_entry_panel_adds_no_candidate_identity(
    monkeypatch: Any, client: Any
) -> None:
    """The panel may name the reviewer but must not surface any identifier
    the blind entry page did not already carry: the resume and job ids appear
    exactly as often with a decision history as without one."""
    eid = uuid4()
    resume_id, job_id = uuid4(), uuid4()
    monkeypatch.setattr(
        api_client,
        "get_shortlist_entry",
        MagicMock(
            return_value=_entry(eid, job_id=job_id, resume_id=resume_id, blinded=True)
        ),
    )
    monkeypatch.setattr(
        api_client,
        "get_interview_questions",
        MagicMock(return_value={"status": "none"}),
    )

    def render(history: dict[str, Any]) -> str:
        monkeypatch.setattr(
            api_client,
            "get_human_decisions",
            MagicMock(return_value=history),
            raising=False,
        )
        return _page(client, eid)

    without = render(_history())
    with_decisions = render(_history(_decision(), _decision(decision="hold")))
    assert "reviewer-alpha" in with_decisions  # the panel really rendered
    for ident in (str(resume_id), str(job_id)):
        assert with_decisions.count(ident) == without.count(ident)


# -- the job shortlist badge ---------------------------------------------------


def _job_page_stubs(monkeypatch: Any, job_id: UUID, entries: list[Any]) -> None:
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={"job_id": None, "state": None, "reason": None, "at": None}
        ),
    )


def _stub_latest(monkeypatch: Any, value: Any) -> MagicMock:
    mock = (
        MagicMock(side_effect=value)
        if isinstance(value, Exception)
        else MagicMock(return_value=value)
    )
    monkeypatch.setattr(api_client, "get_job_latest_decisions", mock, raising=False)
    return mock


def _cards(body: str) -> list[str]:
    parts = body.split('class="card candidate-card')
    return parts[1:]


def test_each_card_shows_the_badge_for_its_own_resume_only(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    entries = [
        _entry(uuid4(), job_id=job_id, resume_id=r1, rank=1, label="Candidate One"),
        _entry(uuid4(), job_id=job_id, resume_id=r2, rank=2, label="Candidate Two"),
        _entry(uuid4(), job_id=job_id, resume_id=r3, rank=3, label="Candidate Three"),
    ]
    _job_page_stubs(monkeypatch, job_id, entries)
    mock = _stub_latest(
        monkeypatch,
        [_badge(r2, "reject", "badge-reviewer-two"), _badge(r3, "hold", "badge-rev-3")],
    )
    resp = client.get(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 200
    mock.assert_called_once()
    assert mock.call_args.args[0] == job_id
    cards = _cards(resp.get_data(as_text=True))
    assert len(cards) == 3
    assert "badge-reviewer-two" not in cards[0]
    assert "badge-reviewer-two" in cards[1]
    assert "badge-rev-3" in cards[2]
    assert "badge-rev-3" not in cards[1]
    assert re.search(r"reject", cards[1], re.IGNORECASE)
    assert re.search(r"hold", cards[2], re.IGNORECASE)
    assert not re.search(r"badge-rev", cards[0])


def test_rejected_candidate_stays_visible_and_ranking_order_is_unchanged(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    r1, r2 = uuid4(), uuid4()
    entries = [
        _entry(uuid4(), job_id=job_id, resume_id=r1, rank=1, label="Candidate First"),
        _entry(uuid4(), job_id=job_id, resume_id=r2, rank=2, label="Candidate Second"),
    ]
    _job_page_stubs(monkeypatch, job_id, entries)
    badges = [_badge(r1, "reject", "rev-x"), _badge(r2, "accept", "rev-y")]
    _stub_latest(monkeypatch, badges)
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    assert "rev-x" in body and "rev-y" in body  # badges rendered
    assert "Candidate First" in body
    assert "Candidate Second" in body
    assert body.index("Candidate First") < body.index("Candidate Second")


@pytest.mark.parametrize(
    "exc",
    [
        api_client.BackendUnavailable("down"),
        api_client.NotFound("gone"),
        api_client.BadRequest("403", status_code=403, detail={"detail": "no"}),
    ],
)
def test_badge_fetch_failure_degrades_to_no_badges_page_still_200(
    monkeypatch: Any, client: Any, exc: Exception
) -> None:
    job_id = uuid4()
    entries = [_entry(uuid4(), job_id=job_id, label="Candidate Survivor")]
    _job_page_stubs(monkeypatch, job_id, entries)
    mock = _stub_latest(monkeypatch, exc)
    resp = client.get(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 200
    mock.assert_called_once()
    assert "Candidate Survivor" in resp.get_data(as_text=True)


def test_no_decisions_means_no_badge_markup(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    entries = [_entry(uuid4(), job_id=job_id)]
    _job_page_stubs(monkeypatch, job_id, entries)
    mock = _stub_latest(monkeypatch, [])
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    mock.assert_called_once()
    assert re.search(r"\bCandidate A\b", body)
    assert not re.search(r"accepted|rejected|on hold", body, re.IGNORECASE)


@pytest.mark.parametrize("n", [32, 50])
def test_badges_mint_no_token_and_leave_the_card_token_map_unchanged(
    monkeypatch: Any, client: Any, n: int
) -> None:
    job_id = uuid4()
    entries = [
        _entry(
            uuid4(),
            job_id=job_id,
            resume_id=uuid4(),
            rank=i + 1,
            label=f"Candidate {i}",
        )
        for i in range(n)
    ]
    _job_page_stubs(monkeypatch, job_id, entries)

    _stub_latest(monkeypatch, [])
    plain_resp = client.get(f"/jobs/{job_id}/shortlist")
    plain_body = plain_resp.get_data(as_text=True)
    plain_count = len(_session_mapping(client))

    badges = [
        _badge(UUID(e["resume_id"]), "accept" if i % 2 else "reject", f"rev-{i}")
        for i, e in enumerate(entries)
    ]
    _stub_latest(monkeypatch, badges)
    badge_resp = client.get(f"/jobs/{job_id}/shortlist")
    badge_body = badge_resp.get_data(as_text=True)
    badge_count = len(_session_mapping(client))

    assert badge_resp.status_code == 200
    assert "rev-0" in badge_body  # badges really rendered
    assert badge_count == plain_count
    if n == 32:
        assert badge_count == 64
    # Read-only: the badge adds no form and no decision-record target.
    assert badge_body.count("<form") == plain_body.count("<form")
    assert "/decision-record" not in badge_body


def test_blind_job_badge_adds_no_candidate_identity(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    r1 = uuid4()
    entries = [_entry(uuid4(), job_id=job_id, resume_id=r1, blinded=True)]
    _job_page_stubs(monkeypatch, job_id, entries)

    _stub_latest(monkeypatch, [])
    without = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    _stub_latest(monkeypatch, [_badge(r1, "accept", "rev-z")])
    with_badge = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    assert "rev-z" in with_badge
    assert with_badge.count(str(r1)) == without.count(str(r1))
    assert with_badge.count(str(job_id)) == without.count(str(job_id))
