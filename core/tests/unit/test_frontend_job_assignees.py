"""RED — Item 2, the hiring-manager assignment screen (ADR-020 §2).

Pins the frontend half of the contract: the "Assigned hiring managers"
section on ``job_detail.html`` (writer-only), and its two write routes
(``POST /jobs/<job_id>/assignees`` and ``POST
/jobs/<job_id>/assignees/<user_id>/remove``). None of this exists yet —
``frontend.app`` has no such routes, and ``job_detail.html`` renders nothing
about assignees. Every test below fails. RED half of the TDD cycle.

Every ``User`` fixture is a real ``src.schemas.auth.User``,
``model_dump(mode="json")``'d — never a hand-written dict (see
``test_templates_render_api_shaped_rows.py``'s docstring, and this task's own
instruction to build fixtures via ``User(...).model_dump(mode="json")``).

Mirrors ``test_frontend_job_details_edit.py``'s ``csrf_client`` fixture for
the happy-path POSTs, and ``test_frontend_work_authorization_on_shortlist.py``
's ``test_declaring_without_a_token_is_rejected_before_the_backend`` for the
bad-CSRF-token case (a route guarded by the ordinary, opt-OUT ``_csrf_gate``
hook — no new one-shot slot).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.schemas.auth import User
from src.settings import Settings

_TS = dt.datetime(2026, 9, 17, tzinfo=dt.UTC)


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


@pytest.fixture
def plain_client() -> Any:
    """A bare test client with no page token — proves the CSRF guard rejects
    a request before the route body (and the backend) ever runs."""
    app.config.update(TESTING=True)
    return app.test_client()


def _user(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid4(),
        "cas_username": "dana",
        "display_name": "Dana Lee",
        "email": "dana@example.org",
        "role": "hiring_manager",
        "active": True,
        "created_at": _TS,
        "last_seen_at": _TS,
    }
    base.update(over)
    return User(**base).model_dump(mode="json")


def _job(job_id: Any, **over: Any) -> dict[str, Any]:
    """A minimal job-detail body — only the fields ``job_detail.html``
    actually reads (mirrors ``test_frontend_job_details_edit.py``'s own thin
    ``_job`` helper for this exact template)."""
    base: dict[str, Any] = {
        "id": str(job_id),
        "title": "Multimedia Specialist",
        "department": "School of Medicine",
        "location": "Burnaby",
        "min_years": None,
        "status": "open",
        "blind_review": False,
        "parsed_at": "2026-09-01T00:00:00Z",
        "description_parsed": None,
        "failure_reason": None,
    }
    base.update(over)
    return base


def _authenticated(role: str) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": "jordan",
        "cas_enabled": True,
        "role": role,
    }


def _enable_cas_session(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    """Mirrors ``test_frontend_zero_requirements_disclosure.py``'s helper of
    the same name — a live CAS session with a NON-writer role, so
    ``is_writer`` reads ``False`` and the section must not render at all."""
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(role))
    )


# ── the section renders for a writer ────────────────────────────────────────


def test_assignees_section_renders_with_current_assignees_and_a_select(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    assignee = _user(cas_username="dana", display_name="Dana Lee")
    candidate = _user(cas_username="priya", display_name="Priya Singh")
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(jid))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    monkeypatch.setattr(
        api_client, "list_job_assignees", lambda jid, **kw: [assignee]
    )
    monkeypatch.setattr(
        api_client, "list_users", lambda **kw: [assignee, candidate]
    )

    html = client.get(f"/jobs/{job_id}").get_data(as_text=True)

    assert "Assigned hiring managers" in html
    assert "Dana Lee" in html or "dana" in html
    assert '<select name="user_id">' in html
    assert "priya" in html.lower()


def test_no_hiring_manager_assigned_shows_the_invisibility_sentence(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(jid))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_job_assignees", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_users", lambda **kw: [])

    html = client.get(f"/jobs/{job_id}").get_data(as_text=True)

    assert (
        "No hiring manager is assigned — this requisition is invisible to "
        "hiring managers" in html
    )


def test_list_users_is_called_scoped_to_hiring_manager_role(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    seen: dict[str, Any] = {}

    def fake_list_users(**kw: Any) -> list[dict[str, Any]]:
        seen.update(kw)
        return []

    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(jid))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_job_assignees", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_users", fake_list_users)

    client.get(f"/jobs/{job_id}")

    assert seen.get("role") == "hiring_manager"


# ── hidden for a non-writer session ─────────────────────────────────────────


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_section_is_hidden_for_a_non_writer_session(
    monkeypatch: pytest.MonkeyPatch, client: Any, role: str
) -> None:
    _enable_cas_session(monkeypatch, role)
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(jid))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])

    called: dict[str, bool] = {}

    def fail_if_called(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        called["yes"] = True
        return []

    monkeypatch.setattr(api_client, "list_job_assignees", fail_if_called)
    monkeypatch.setattr(api_client, "list_users", fail_if_called)

    resp = client.get(
        f"/jobs/{job_id}", headers={"Cookie": "ra_session=tok-live"}
    )
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Assigned hiring managers" not in html
    assert not called, (
        f"role {role!r} is not a writer — the section's own fetches must be "
        "skipped entirely, never fetched-and-hidden"
    )


# ── POST /jobs/<id>/assignees — add ─────────────────────────────────────────


def test_add_assignee_posts_to_the_right_api_call_and_redirects(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    seen: dict[str, Any] = {}

    def fake_add(
        jid: UUID, user_id: UUID, *, note: str | None = None, **kw: Any
    ) -> Any:
        seen["job_id"] = jid
        seen["user_id"] = user_id
        seen["note"] = note
        return None

    monkeypatch.setattr(api_client, "add_job_assignee", fake_add)
    resp = client.post(
        f"/jobs/{job_id}/assignees", data={"user_id": str(target_user_id)}
    )

    assert resp.status_code == 302
    assert str(job_id) in resp.headers["Location"]
    assert seen["job_id"] == job_id
    assert seen["user_id"] == target_user_id


def test_add_assignee_not_found_job_404s(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_add(*_a: Any, **_kw: Any) -> Any:
        raise api_client.NotFound("no such job")

    monkeypatch.setattr(api_client, "add_job_assignee", fake_add)
    resp = client.post(f"/jobs/{uuid4()}/assignees", data={"user_id": str(uuid4())})
    assert resp.status_code == 404


def test_add_assignee_backend_unavailable_maps_to_503(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_add(*_a: Any, **_kw: Any) -> Any:
        raise api_client.BackendUnavailable("down")

    monkeypatch.setattr(api_client, "add_job_assignee", fake_add)
    resp = client.post(f"/jobs/{uuid4()}/assignees", data={"user_id": str(uuid4())})
    assert resp.status_code == 503


def test_add_assignee_backend_403_becomes_a_403_page_never_a_500(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hiring_manager/auditor session posting through a crafted form still
    gets 403'd by the BACKEND — the route must surface that plainly rather
    than raising an unhandled 500 (the same ADR-033 lesson as
    ``transition_status``/``reparse_job``/``edit_job_details``)."""

    def fake_add(*_a: Any, **_kw: Any) -> Any:
        raise api_client.BadRequest("forbidden", status_code=403, detail="forbidden")

    monkeypatch.setattr(api_client, "add_job_assignee", fake_add)
    resp = client.post(f"/jobs/{uuid4()}/assignees", data={"user_id": str(uuid4())})
    assert resp.status_code == 403


def test_add_assignee_without_a_page_token_is_rejected_before_the_backend(
    plain_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy_called: dict[str, bool] = {}

    def fake_add(*_a: Any, **_kw: Any) -> Any:
        spy_called["yes"] = True
        return None

    monkeypatch.setattr(api_client, "add_job_assignee", fake_add)
    resp = plain_client.post(
        f"/jobs/{uuid4()}/assignees", data={"user_id": str(uuid4())}
    )
    assert resp.status_code == 403
    assert not spy_called


# ── POST /jobs/<id>/assignees/<user_id>/remove — remove ────────────────────


def test_remove_assignee_issues_the_right_api_call_and_redirects(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    target_user_id = uuid4()
    seen: dict[str, Any] = {}

    def fake_remove(jid: UUID, user_id: UUID, **kw: Any) -> Any:
        seen["job_id"] = jid
        seen["user_id"] = user_id
        return None

    monkeypatch.setattr(api_client, "remove_job_assignee", fake_remove)
    resp = client.post(f"/jobs/{job_id}/assignees/{target_user_id}/remove")

    assert resp.status_code == 302
    assert str(job_id) in resp.headers["Location"]
    assert seen["job_id"] == job_id
    assert seen["user_id"] == target_user_id


def test_remove_assignee_not_found_404s(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_remove(*_a: Any, **_kw: Any) -> Any:
        raise api_client.NotFound("no such assignment")

    monkeypatch.setattr(api_client, "remove_job_assignee", fake_remove)
    resp = client.post(f"/jobs/{uuid4()}/assignees/{uuid4()}/remove")
    assert resp.status_code == 404


def test_remove_assignee_backend_403_becomes_a_403_page_never_a_500(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_remove(*_a: Any, **_kw: Any) -> Any:
        raise api_client.BadRequest("forbidden", status_code=403, detail="forbidden")

    monkeypatch.setattr(api_client, "remove_job_assignee", fake_remove)
    resp = client.post(f"/jobs/{uuid4()}/assignees/{uuid4()}/remove")
    assert resp.status_code == 403


def test_remove_assignee_without_a_page_token_is_rejected_before_the_backend(
    plain_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy_called: dict[str, bool] = {}

    def fake_remove(*_a: Any, **_kw: Any) -> Any:
        spy_called["yes"] = True
        return None

    monkeypatch.setattr(api_client, "remove_job_assignee", fake_remove)
    resp = plain_client.post(f"/jobs/{uuid4()}/assignees/{uuid4()}/remove")
    assert resp.status_code == 403
    assert not spy_called


__all__: list[str] = []
