"""S1a (job-page redesign) -- the third source of ``_use_job_page_v2()``: the
signed-in user's ``job_page_v2`` field from ``g.cas_user`` (the ``/auth/cas/user``
status payload).

RED: ``_use_job_page_v2`` reads only the global flag and the allowlist today, so
a user whose payload says ``job_page_v2: true`` still gets v1.

The three sources combine by OR: ``Settings.job_page_v2``, the CAS username in
``Settings.job_page_v2_users``, or ``g.cas_user["job_page_v2"]`` being true. A
missing or false field is false. Driven over HTTP on the job page with the
``data-job-layout="v2"`` marker, as ``test_frontend_job_page_v2_flag.py`` does.
Also pins the audit disclosure allowlist for the new action, because the audit
viewer withholds every key it has not been told is safe, and ``True`` would
otherwise render as withheld.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from src.schemas.jobs import JDExtracted, JobOut, Skill
from src.services.audit_service import WITHHELD, redact_audit_details
from src.settings import Settings

_JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
_TS = dt.datetime(2026, 9, 3, 8, 30, tzinfo=dt.UTC)
_MARKER = 'data-job-layout="v2"'
_MISSING: Any = object()


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _job() -> dict[str, Any]:
    return JobOut(
        id=_JOB_ID,
        title="Multimedia Specialist",
        department="School of Medicine",
        location="Burnaby",
        employment_type=None,
        seniority=None,
        min_years=3,
        description_raw="A detailed job description of the role. " * 3,
        description_parsed=JDExtracted(
            title="x", required_skills=[Skill(name="Video editing")]
        ),
        status="open",
        retention_days=180,
        shortlist_top_percent=100,
        blind_review=False,
        failure_reason=None,
        created_by="asalah",
        created_at=_TS,
        updated_at=_TS,
        parsed_at=_TS,
        closed_at=None,
        source="manual",
    ).model_dump(mode="json")


def _session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    column: Any,
    username: str | None = "jordan",
    role: str = "recruiter",
    **settings_kwargs: Any,
) -> None:
    cas = username is not None
    settings = Settings(cas_enabled=cas, **settings_kwargs)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    if cas:
        status: dict[str, Any] = {
            "authenticated": True,
            "username": username,
            "cas_enabled": True,
            "role": role,
        }
        if column is not _MISSING:
            status["job_page_v2"] = column
        monkeypatch.setattr(api_client, "get_cas_user", lambda **kw: status)
    job = _job()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: job)
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_resume_splits", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "get_latest_resume_export", lambda jid, **kw: None)
    monkeypatch.setattr(api_client, "list_job_assignees", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_users", lambda **kw: [])
    monkeypatch.setattr(api_client, "list_shortlist", lambda jid, **kw: [])


def _body(client: Any) -> str:
    resp = client.get(f"/jobs/{_JOB_ID}", headers={"Cookie": "ra_session=tok-live"})
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_a_user_with_the_column_true_sees_v2_with_flag_and_allowlist_off(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, column=True, job_page_v2=False, job_page_v2_users="")
    assert _MARKER in _body(client)


@pytest.mark.parametrize("role", ["admin", "recruiter", "hiring_manager", "auditor"])
def test_the_column_works_for_every_role(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    _session(monkeypatch, column=True, role=role)
    assert _MARKER in _body(client)


@pytest.mark.parametrize("column", [False, None, _MISSING, "", 0])
def test_a_false_or_missing_field_is_v1(
    client: Any, monkeypatch: pytest.MonkeyPatch, column: Any
) -> None:
    _session(monkeypatch, column=column)
    assert "data-job-layout" not in _body(client)


def test_the_column_is_not_matched_by_the_string_false(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BFF payload bug must not turn "false" truthy."""
    _session(monkeypatch, column="false")
    assert "data-job-layout" not in _body(client)


def test_the_global_flag_still_wins_when_the_column_is_false(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, column=False, job_page_v2=True)
    assert _MARKER in _body(client)


def test_the_allowlist_still_wins_when_the_column_is_false(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, column=False, job_page_v2_users="jordan")
    assert _MARKER in _body(client)


def test_all_three_sources_on_together_is_still_v2(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, column=True, job_page_v2=True, job_page_v2_users="jordan")
    assert _MARKER in _body(client)


def test_an_unlisted_user_with_the_column_false_and_flag_off_is_v1(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, column=False, job_page_v2_users="alice,bob")
    assert "data-job-layout" not in _body(client)


def test_the_column_does_not_leak_to_another_request_in_the_same_process(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, column=True)
    assert _MARKER in _body(client)
    _session(monkeypatch, column=False)
    assert "data-job-layout" not in _body(client)


# ── audit disclosure allowlist for the new action ───────────────────────────


def test_the_new_audit_action_discloses_its_two_boolean_keys() -> None:
    details = {"old_value": False, "new_value": True}
    assert redact_audit_details("job_page_preview_changed", details) == details


def test_an_unlisted_key_on_the_new_action_is_still_withheld() -> None:
    out = redact_audit_details(
        "job_page_preview_changed",
        {"old_value": False, "new_value": True, "note": "free text"},
    )
    assert out["note"] == WITHHELD
    assert out["new_value"] is True
