"""S1 (job-page redesign, Option A) -- every v2 tab renders what the API sends.

The frontend is a BFF: every timestamp reaches a template as a **string**, and
a hand-written fixture dict can hold types the API never produces. In
production a ``{{ job.updated_at.strftime(...) }}`` 500'd the whole jobs list
behind a green suite (``test_templates_render_api_shaped_rows.py``). So every
fixture here is a real DTO serialised with ``model_dump(mode="json")``, and all
five tabs are rendered for each.

RED: ``?tab=`` and the v2 shell do not exist, so the marker assertions fail.
Once they exist, ``.strftime`` on a string, or a field a template reads that
the DTO does not carry, fails here instead of in front of a user.

The export and pending-split read models have no DTO class in ``src/`` (they
are plain dicts built in the API layer), so those two fixtures are dicts shaped
like the ones in ``test_frontend_resume_export_job.py`` and
``test_frontend_split_review.py``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from src.schemas.jobs import (
    JDExtracted,
    JobOut,
    ManagerRequirements,
    Skill,
)
from src.schemas.resumes import ResumeListItem
from src.settings import Settings

_JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
_JOB_TS = dt.datetime(2026, 9, 3, 8, 30, 45, tzinfo=dt.UTC)
_RESUME_TS = dt.datetime(2026, 9, 4, 13, 15, 22, tzinfo=dt.UTC)
_MARKER = 'data-job-layout="v2"'
_TABS = ("overview", "candidates", "requirements", "shortlist", "settings")
#: The job timestamps as a browser-bound string would carry them. A v2 page
#: must never dump these raw (``| day`` renders a date).
_RAW_JOB_TS = ("2026-09-03T08:30:45", "08:30:45")


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _job(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": _JOB_ID,
        "title": "Multimedia Specialist",
        "department": "School of Medicine",
        "location": "Burnaby",
        "employment_type": "full_time",
        "seniority": "mid",
        "min_years": 3,
        "description_raw": "A detailed job description of the role. " * 3,
        "description_parsed": JDExtracted(
            title="Multimedia Specialist",
            required_skills=[Skill(name="Video editing"), Skill(name="Photography")],
            nice_to_have_skills=[Skill(name="Motion graphics")],
        ),
        "status": "open",
        "retention_days": 180,
        "shortlist_top_percent": 100,
        "blind_review": True,
        "failure_reason": None,
        "created_by": "asalah",
        "created_at": _JOB_TS,
        "updated_at": _JOB_TS,
        "parsed_at": _JOB_TS,
        "closed_at": None,
        "additional_requirements": "Must have Python.",
        "additional_requirements_parsed": ManagerRequirements(
            must_have_skills=[Skill(name="Python")]
        ),
        "source": "taleo",
        "external_id": "7124",
        "external_url": "https://tre.tbe.taleo.net/req?rid=7124",
        "external_last_seen_at": _JOB_TS,
        "title_provisional": True,
    }
    base.update(over)
    return JobOut(**base).model_dump(mode="json")


def _bare_job() -> dict[str, Any]:
    """A freshly created manual requisition: every optional field null."""
    return _job(
        department=None,
        location=None,
        employment_type=None,
        seniority=None,
        min_years=None,
        description_parsed=None,
        status="draft",
        parsed_at=None,
        additional_requirements=None,
        additional_requirements_parsed=None,
        source="manual",
        external_id=None,
        external_url=None,
        external_last_seen_at=None,
        title_provisional=False,
        blind_review=False,
    )


def _resumes() -> list[dict[str, Any]]:
    rows = [
        ResumeListItem(
            id=UUID("22222222-2222-4222-8222-222222222222"),
            original_filename="ada.pdf",
            status="parsed",
            uploaded_at=_RESUME_TS,
            parsed_at=_RESUME_TS,
            candidate_name="Ada Lovelace",
            has_cover_letter=True,
        ),
        ResumeListItem(
            id=UUID("33333333-3333-4333-8333-333333333333"),
            original_filename="grace.pdf",
            status="parsing",
            uploaded_at=_RESUME_TS,
            parsed_at=None,
        ),
        ResumeListItem(
            id=UUID("44444444-4444-4444-8444-444444444444"),
            original_filename="alan.pdf",
            status="parsed",
            uploaded_at=_RESUME_TS,
            parsed_at=_RESUME_TS,
            candidate_name="Alan Turing",
            withdrawn_at=_RESUME_TS,
            withdrawal_reason="accepted another offer",
        ),
    ]
    return [r.model_dump(mode="json") for r in rows]


def _assignee() -> dict[str, Any]:
    from src.schemas.auth import User

    return User(
        id=UUID("55555555-5555-4555-8555-555555555555"),
        cas_username="dana",
        display_name="Dana Lee",
        email="dana@example.org",
        role="hiring_manager",
        active=True,
        created_at=_JOB_TS,
        last_seen_at=_JOB_TS,
    ).model_dump(mode="json")


def _arrange(
    monkeypatch: pytest.MonkeyPatch,
    job: dict[str, Any],
    *,
    role: str | None = None,
    resumes: list[dict[str, Any]] | None = None,
) -> None:
    settings = Settings(cas_enabled=role is not None, job_page_v2=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    if role is not None:
        status = {
            "authenticated": True,
            "username": "jordan",
            "cas_enabled": True,
            "role": role,
        }
        monkeypatch.setattr(api_client, "get_cas_user", lambda **kw: status)
    rows = _resumes() if resumes is None else resumes
    export = {
        "id": "66666666-6666-4666-8666-666666666666",
        "status": "ready",
        "counts": {"applicants": 3, "anonymized": 2, "not_anonymized": 1},
        "created_at": _JOB_TS.isoformat(),
        "expires_at": _JOB_TS.isoformat(),
    }
    split = {
        "id": "77777777-7777-4777-8777-777777777777",
        "status": "proposed",
        "created_at": _JOB_TS.isoformat(),
        "page_count": 8,
        "method": "llm",
        "source_filename": "export.pdf",
    }
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: job)
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: rows)
    monkeypatch.setattr(api_client, "list_resume_splits", lambda jid, **kw: [split])
    monkeypatch.setattr(
        api_client, "get_latest_resume_export", lambda jid, **kw: export
    )
    monkeypatch.setattr(
        api_client, "list_job_assignees", lambda jid, **kw: [_assignee()]
    )
    monkeypatch.setattr(api_client, "list_users", lambda **kw: [_assignee()])
    monkeypatch.setattr(api_client, "list_shortlist", lambda jid, **kw: [])


def _render(client: Any, tab: str) -> str:
    resp = client.get(
        f"/jobs/{_JOB_ID}?tab={tab}", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200, body[:400]
    return body


@pytest.mark.parametrize("tab", _TABS)
def test_a_full_taleo_job_renders_every_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    _arrange(monkeypatch, _job())
    body = _render(client, tab)
    assert _MARKER in body
    assert "Multimedia Specialist" in body


@pytest.mark.parametrize("tab", _TABS)
def test_a_bare_manual_job_renders_every_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    _arrange(monkeypatch, _bare_job(), resumes=[])
    body = _render(client, tab)
    assert _MARKER in body


@pytest.mark.parametrize("tab", _TABS)
@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_a_non_writer_renders_every_requested_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str, role: str
) -> None:
    """``settings`` is not offered, so it falls back to Overview, still 200."""
    _arrange(monkeypatch, _job(), role=role)
    body = _render(client, tab)
    assert _MARKER in body


@pytest.mark.parametrize("tab", _TABS)
@pytest.mark.parametrize(
    "over",
    [
        {"status": "draft", "parsed_at": None, "description_parsed": None},
        {
            "status": "draft",
            "parsed_at": None,
            "description_parsed": None,
            "failure_reason": "The model returned no usable JSON.",
        },
        {"status": "draft"},
        {"status": "closed", "closed_at": _JOB_TS},
        {"status": "archived", "closed_at": _JOB_TS},
        {"description_parsed": JDExtracted(title="x")},
    ],
    ids=["parsing", "failed", "parsed", "closed", "archived", "zero_requirements"],
)
def test_every_lifecycle_state_renders_every_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str, over: dict[str, Any]
) -> None:
    _arrange(monkeypatch, _job(**over))
    assert _MARKER in _render(client, tab)


@pytest.mark.parametrize("tab", _TABS)
def test_a_job_timestamp_is_never_dumped_raw_on_a_v2_page(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    """``| day`` renders a date; the raw string is noise (HANDOFF lesson 6)."""
    _arrange(monkeypatch, _job())
    body = _render(client, tab)
    assert _MARKER in body
    for raw in _RAW_JOB_TS:
        assert raw not in body, f"raw job timestamp {raw!r} leaked into {tab}"


def test_the_fixtures_are_json_mode_dumps_with_string_timestamps() -> None:
    """Guard the guard: a fixture that smuggles a datetime in proves nothing."""
    job = _job()
    for key in ("created_at", "updated_at", "parsed_at", "external_last_seen_at"):
        assert isinstance(job[key], str), key
    assert isinstance(job["id"], str)
    assert all(isinstance(r["uploaded_at"], str) for r in _resumes())
