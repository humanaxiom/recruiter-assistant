"""RED pin — pilot defect 2026-09-10: a job whose JD parse extracted ZERO
``required_skills`` AND ZERO ``nice_to_have_skills`` gave the recruiter no
signal at all -- the Generate button worked, no banner appeared, and the
resulting shortlist (once ranked) carried no explanation for why it was
empty/meaningless. Mirrors ADR-017 decision 1's "refuse rather than silently
degrade" shape and the ADR-040/041 disclosure line: the UI must disclose the
SAME state the API now refuses to rank.

Fixtures are built as real ``JobOut``/``JDExtracted``/``Skill`` DTOs,
``model_dump(mode="json")``'d -- per HANDOFF lesson 6, never a hand-written
dict (see ``test_templates_render_api_shaped_rows.py``'s docstring). Follows
``test_frontend_shortlist_degraded_hint.py``'s client/autouse-mock/regex-on-
body harness for the shortlist page, and that file's
``_enable_cas_session``/role-loop pattern for the "every role" assertion.

**Stable markers a coder must produce (stated explicitly per the task):**
* the phrase ``"no requirements"`` (case-insensitive) appears somewhere in
  the disclosure text, on both the shortlist page and the job detail page;
* the shortlist-page banner carries the HTML id ``jd-no-requirements``;
* the job-detail parse-status warning carries the HTML id
  ``jd-no-requirements-warning``.

None of this exists yet: ``shortlist_list.html`` has no such banner,
``parse_status.html`` renders only a muted span, and ``app.py`` has no
``_jd_yielded_no_requirements`` helper and does not pass ``jd_has_no_requirements``
into either template. Every test below fails. RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client, csrf
from frontend import app as frontend_app_module
from frontend.app import app
from src.schemas import JDExtracted, Skill
from src.schemas.jobs import JobOut
from src.settings import Settings

_TS = dt.datetime(2026, 9, 10, tzinfo=dt.UTC)

_MARKER = "no requirements"


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture
def csrf_client() -> Any:
    """Mirrors core/tests/unit/conftest.py's csrf_client fixture -- this file
    defines its own plain client fixture (not the shared one), so a page
    token has to be supplied locally for the two write-route tests below."""
    app.config.update(TESTING=True)
    c = app.test_client()
    with c.session_transaction() as session:
        session[csrf.PAGE_SESSION_KEY] = "test-only-page-token"
    c.environ_base["HTTP_X_CSRF_TOKEN"] = "test-only-page-token"
    return c


@pytest.fixture(autouse=True)
def _no_ranking_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirrors ``test_frontend_shortlist_degraded_hint.py``'s identical
    fixture: no in-flight ranking/awaiting_llm state, so the poll banners
    never interfere with these assertions."""
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={"job_id": None, "state": None, "reason": None, "at": None}
        ),
    )
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))


def _job(**over: Any) -> dict[str, Any]:
    """A real ``JobOut`` DTO, ``model_dump(mode='json')``'d -- never a
    hand-written dict (HANDOFF lesson 6 /
    ``test_templates_render_api_shaped_rows.py``)."""
    base: dict[str, Any] = {
        "id": uuid4(),
        "title": "Multimedia Specialist",
        "department": "School of Medicine",
        "location": "Burnaby",
        "employment_type": None,
        "seniority": None,
        "min_years": None,
        "description_raw": "A detailed job description. " * 3,
        "description_parsed": None,
        "status": "open",
        "retention_days": 180,
        "shortlist_top_percent": 100,
        "blind_review": False,
        "failure_reason": None,
        "created_by": "asalah",
        "created_at": _TS,
        "updated_at": _TS,
        "parsed_at": _TS,
        "closed_at": None,
        "source": "manual",
        "external_id": None,
        "external_url": None,
        "external_last_seen_at": None,
        "title_provisional": False,
    }
    base.update(over)
    return JobOut(**base).model_dump(mode="json")


def _jd(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"title": "Multimedia Specialist"}
    base.update(over)
    return JDExtracted(**base).model_dump(mode="json")


def _parsed_resume() -> dict[str, Any]:
    from src.schemas.resumes import ResumeListItem

    return ResumeListItem(
        id=uuid4(),
        original_filename="resume.pdf",
        status="parsed",  # type: ignore[arg-type]
        uploaded_at=_TS,
        parsed_at=_TS,
        candidate_name="Jane Smith",
        degraded=False,
    ).model_dump(mode="json")


def _get_shortlist(client: Any, job_id: Any) -> Any:
    return client.get(f"/jobs/{job_id}/shortlist")


def _get_detail(client: Any, job_id: Any) -> Any:
    return client.get(f"/jobs/{job_id}")


def _authenticated(role: str) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": "jordan",
        "cas_enabled": True,
        "role": role,
    }


def _enable_cas_session(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(role))
    )


# ── shortlist page: Generate disabled + reason ──────────────────────────────


def test_generate_disabled_when_parsed_and_both_lists_empty(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    job = _job(description_parsed=_jd(required_skills=[], nice_to_have_skills=[]))
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_parsed_resume()])
    )

    resp = _get_shortlist(client, job_id)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "disabled" in body
    assert _MARKER in body.lower()


def test_generate_enabled_when_only_nice_to_have_present(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    job = _job(
        description_parsed=_jd(
            required_skills=[],
            nice_to_have_skills=[Skill(name="Terraform").model_dump()],
        )
    )
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_parsed_resume()])
    )

    resp = _get_shortlist(client, job_id)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'id="jd-no-requirements"' not in body


def test_generate_enabled_when_required_present(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    job = _job(
        description_parsed=_jd(
            required_skills=[Skill(name="Python").model_dump()],
            nice_to_have_skills=[],
        )
    )
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_parsed_resume()])
    )

    resp = _get_shortlist(client, job_id)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'id="jd-no-requirements"' not in body


# ── shortlist page: banner over existing entries ────────────────────────────


def test_banner_renders_over_existing_shortlist_entries(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    """The banner must render even when the job ALREADY carries ranked
    entries from before the guard existed -- a stale shortlist off an
    all-zero JD is exactly the meaningless-output case the disclosure
    exists for."""
    job_id = uuid4()
    job = _job(description_parsed=_jd(required_skills=[], nice_to_have_skills=[]))
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_parsed_resume()])
    )
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(
            return_value=[
                {
                    "id": str(uuid4()),
                    "job_id": str(job_id),
                    "resume_id": str(uuid4()),
                    "rank": 1,
                    "score_final": 0.5,
                    "score_breakdown": {},
                    "evidence": None,
                    "generated_at": _TS.isoformat(),
                    "blinded": False,
                    "display_label": "Jane Smith",
                }
            ]
        ),
    )

    resp = _get_shortlist(client, job_id)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'id="jd-no-requirements"' in body


def test_no_banner_when_requirements_exist(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    job = _job(
        description_parsed=_jd(
            required_skills=[Skill(name="Python").model_dump()], nice_to_have_skills=[]
        )
    )
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_parsed_resume()])
    )

    body = _get_shortlist(client, job_id).get_data(as_text=True)

    assert 'id="jd-no-requirements"' not in body


def test_no_banner_when_parsed_at_none(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    job = _job(parsed_at=None, description_parsed=None)
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_parsed_resume()])
    )

    body = _get_shortlist(client, job_id).get_data(as_text=True)

    assert 'id="jd-no-requirements"' not in body


def test_no_banner_when_description_parsed_none_but_parsed_at_set(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    """A degenerate wire shape -- parsed_at is set but description_parsed is
    still null. Must not crash and must not falsely claim "no requirements"
    when there is nothing to evaluate at all (this is a parse-shape
    oddity, not the zero-requirements case)."""
    job_id = uuid4()
    job = _job(description_parsed=None)
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_parsed_resume()])
    )

    resp = _get_shortlist(client, job_id)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'id="jd-no-requirements"' not in body


# ── job detail page: parse_status.html warning ──────────────────────────────


def test_job_detail_warns_when_both_lists_empty(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    job = _job(description_parsed=_jd(required_skills=[], nice_to_have_skills=[]))
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))

    resp = _get_detail(client, job_id)
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'id="jd-no-requirements-warning"' in body
    assert _MARKER in body.lower()


def test_job_detail_silent_when_required_present(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    job = _job(
        description_parsed=_jd(
            required_skills=[Skill(name="Python").model_dump()], nice_to_have_skills=[]
        )
    )
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))

    body = _get_detail(client, job_id).get_data(as_text=True)

    assert 'id="jd-no-requirements-warning"' not in body


def test_job_detail_silent_when_parsed_at_none(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    job = _job(parsed_at=None, description_parsed=None)
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))

    body = _get_detail(client, job_id).get_data(as_text=True)

    assert 'id="jd-no-requirements-warning"' not in body


# ── visible for every session role -- parse state, not PII ─────────────────


@pytest.mark.parametrize("role", ["recruiter", "admin", "hiring_manager", "auditor"])
def test_shortlist_warning_renders_for_every_session_role(
    monkeypatch: pytest.MonkeyPatch, client: Any, role: str
) -> None:
    _enable_cas_session(monkeypatch, role)
    job_id = uuid4()
    job = _job(description_parsed=_jd(required_skills=[], nice_to_have_skills=[]))
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[_parsed_resume()])
    )

    resp = client.get(
        f"/jobs/{job_id}/shortlist", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'id="jd-no-requirements"' in body, (
        f"role {role!r} did not see the zero-requirements banner -- it is "
        "parse state, not PII, and must be visible regardless of role"
    )


@pytest.mark.parametrize("role", ["recruiter", "admin", "hiring_manager", "auditor"])
def test_job_detail_warning_renders_for_every_session_role(
    monkeypatch: pytest.MonkeyPatch, client: Any, role: str
) -> None:
    _enable_cas_session(monkeypatch, role)
    job_id = uuid4()
    job = _job(description_parsed=_jd(required_skills=[], nice_to_have_skills=[]))
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=job))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))

    resp = client.get(f"/jobs/{job_id}", headers={"Cookie": "ra_session=tok-live"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'id="jd-no-requirements-warning"' in body, (
        f"role {role!r} did not see the zero-requirements parse-status "
        "warning -- it is parse state, not PII, and must be visible "
        "regardless of role"
    )


# ── POST generate: a 409 Conflict from the API re-renders, never aborts ────


def test_post_generate_conflict_does_not_return_a_raw_409(
    monkeypatch: pytest.MonkeyPatch, csrf_client: Any
) -> None:
    """``api_client.Conflict`` subclasses ``BadRequest`` (per its own
    docstring), so ``generate_shortlist``'s existing ``except BadRequest:
    abort(exc.status_code)`` would already 409 here -- the fix must catch
    ``Conflict`` FIRST and re-render the cards fragment with the reason
    instead. Uses ``csrf_client`` (a page token supplied) so the request
    reaches the route body at all -- a bare client 403s at the CSRF guard
    before ``api_client.generate_shortlist`` is ever called, which would make
    this test pass for the wrong reason."""
    job_id = uuid4()

    def _raise_conflict(*_a: Any, **_kw: Any) -> Any:
        raise api_client.Conflict(
            "job has no required or nice-to-have skills",
            status_code=409,
            detail={"detail": "job has no required or nice-to-have skills"},
        )

    monkeypatch.setattr(api_client, "generate_shortlist", _raise_conflict)

    resp = csrf_client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code != 403, "request never reached the route (CSRF-blocked)"
    assert resp.status_code != 409


def test_post_generate_conflict_body_contains_the_reason(
    monkeypatch: pytest.MonkeyPatch, csrf_client: Any
) -> None:
    job_id = uuid4()

    def _raise_conflict(*_a: Any, **_kw: Any) -> Any:
        raise api_client.Conflict(
            "job has no required or nice-to-have skills",
            status_code=409,
            detail={"detail": "job has no required or nice-to-have skills"},
        )

    monkeypatch.setattr(api_client, "generate_shortlist", _raise_conflict)

    resp = csrf_client.post(f"/jobs/{job_id}/shortlist")
    assert resp.status_code != 403, "request never reached the route (CSRF-blocked)"
    body = resp.get_data(as_text=True)

    assert _MARKER in body.lower() or "skill" in body.lower()
