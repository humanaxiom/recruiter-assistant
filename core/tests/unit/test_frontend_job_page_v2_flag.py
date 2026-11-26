"""S1 (job-page redesign, Option A) -- the v2 flag, its allowlist and ``?tab=``.

RED: nothing below exists yet. ``Settings`` has no ``job_page_v2`` /
``job_page_v2_users`` fields, ``/jobs/<id>`` ignores ``?tab=``, and no page
carries the ``data-job-layout="v2"`` marker.

The layout is v2 when ``Settings.job_page_v2`` is on OR the signed-in CAS
username is in the comma-separated ``Settings.job_page_v2_users`` (trimmed,
case-insensitive). A CAS-off dev session has no identity, so only the global
flag can turn v2 on there. (The third source, the ``users.job_page_v2`` column,
is slice S1a and is not tested here.)

Driven through the Flask test client with a stubbed ``api_client``, as the rest
of the frontend suite does.
"""

from __future__ import annotations

import datetime as dt
from html.parser import HTMLParser
from typing import Any
from uuid import UUID

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from src.schemas.jobs import JDExtracted, JobOut, Skill
from src.settings import Settings

_JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
_TS = dt.datetime(2026, 9, 3, 8, 30, tzinfo=dt.UTC)
_MARKER = 'data-job-layout="v2"'
_TABS = ("overview", "candidates", "requirements", "shortlist", "settings")


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _job(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": _JOB_ID,
        "title": "Multimedia Specialist",
        "department": "School of Medicine",
        "location": "Burnaby",
        "employment_type": None,
        "seniority": None,
        "min_years": 3,
        "description_raw": "A detailed job description of the role. " * 3,
        "description_parsed": JDExtracted(
            title="x", required_skills=[Skill(name="Video editing")]
        ),
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
    }
    base.update(over)
    return JobOut(**base).model_dump(mode="json")


def _stub_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: job)
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_resume_splits", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "get_latest_resume_export", lambda jid, **kw: None)
    monkeypatch.setattr(api_client, "list_job_assignees", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_users", lambda **kw: [])
    monkeypatch.setattr(api_client, "list_shortlist", lambda jid, **kw: [])


def _session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    username: str | None = "jordan",
    role: str = "recruiter",
    **settings_kwargs: Any,
) -> None:
    """Install settings and (when ``username`` is given) a CAS session."""
    cas = username is not None
    settings = Settings(cas_enabled=cas, **settings_kwargs)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    if cas:
        status = {
            "authenticated": True,
            "username": username,
            "cas_enabled": True,
            "role": role,
        }
        monkeypatch.setattr(api_client, "get_cas_user", lambda **kw: status)
    _stub_backend(monkeypatch)


class _Anchors(HTMLParser):
    """Collects ``(text, href, aria-current)`` for every anchor."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str, str | None]] = []
        self._open: list[str | None] | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            d = dict(attrs)
            self._open = [d.get("href"), d.get("aria-current")]
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._open is not None:
            text = " ".join("".join(self._text).split())
            self.anchors.append((text, self._open[0] or "", self._open[1]))
            self._open = None


def _active_tabs(html: str) -> list[str]:
    parser = _Anchors()
    parser.feed(html)
    return [t.lower() for t, _h, cur in parser.anchors if cur == "page"]


def _tab_labels(html: str) -> set[str]:
    parser = _Anchors()
    parser.feed(html)
    wanted = {"overview", "candidates", "requirements", "shortlist", "settings"}
    return {t.lower() for t, _h, _c in parser.anchors if t.lower() in wanted}


def _get(client: Any, url: str, expect: int = 200) -> str:
    resp = client.get(url, headers={"Cookie": "ra_session=tok-live"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == expect, body[:400]
    return body


# ── Settings ────────────────────────────────────────────────────────────────


def test_the_flag_defaults_off_and_the_allowlist_defaults_empty() -> None:
    settings = Settings()
    assert settings.job_page_v2 is False
    assert settings.job_page_v2_users == ""


def test_the_flag_and_allowlist_bind_to_their_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_PAGE_V2", "true")
    monkeypatch.setenv("JOB_PAGE_V2_USERS", "alice,bob")
    settings = Settings()
    assert settings.job_page_v2 is True
    assert settings.job_page_v2_users == "alice,bob"


# ── which layout renders ────────────────────────────────────────────────────


def test_the_global_flag_on_renders_v2_for_a_cas_off_dev_session(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, username=None, job_page_v2=True)
    assert _MARKER in _get(client, f"/jobs/{_JOB_ID}")


@pytest.mark.parametrize("role", ["admin", "recruiter", "hiring_manager", "auditor"])
def test_the_global_flag_on_renders_v2_for_every_role(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    _session(monkeypatch, role=role, job_page_v2=True)
    assert _MARKER in _get(client, f"/jobs/{_JOB_ID}")


def test_the_flag_off_renders_v1_with_no_marker(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, job_page_v2=False)
    body = _get(client, f"/jobs/{_JOB_ID}")
    assert "data-job-layout" not in body


def test_cas_off_with_the_flag_off_renders_v1(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, username=None, job_page_v2=False)
    assert "data-job-layout" not in _get(client, f"/jobs/{_JOB_ID}")


def test_an_allowlisted_user_sees_v2_with_the_flag_off(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, username="jordan", job_page_v2_users="alice,jordan")
    assert _MARKER in _get(client, f"/jobs/{_JOB_ID}")


@pytest.mark.parametrize("listing", ["  Jordan ", "alice,  JORDAN", "JORDAN,bob"])
def test_the_allowlist_is_trimmed_and_case_insensitive(
    client: Any, monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    _session(monkeypatch, username="jordan", job_page_v2_users=listing)
    assert _MARKER in _get(client, f"/jobs/{_JOB_ID}")


def test_an_unlisted_user_sees_v1(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _session(monkeypatch, username="jordan", job_page_v2_users="alice,bob")
    assert "data-job-layout" not in _get(client, f"/jobs/{_JOB_ID}")


def test_an_allowlist_does_not_match_on_a_substring(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, username="jordan", job_page_v2_users="jordana,xjordan")
    assert "data-job-layout" not in _get(client, f"/jobs/{_JOB_ID}")


def test_a_cas_off_session_never_matches_an_allowlist(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No identity means only the global flag applies, whatever the list says."""
    _session(
        monkeypatch,
        username=None,
        job_page_v2=False,
        job_page_v2_users="anonymous,dev,jordan,",
    )
    assert "data-job-layout" not in _get(client, f"/jobs/{_JOB_ID}")


def test_an_empty_allowlist_entry_matches_no_one(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, username="jordan", job_page_v2_users=" , ,")
    assert "data-job-layout" not in _get(client, f"/jobs/{_JOB_ID}")


# ── ?tab= routing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("tab", _TABS)
def test_each_valid_tab_is_the_active_one_for_a_writer(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    _session(monkeypatch, job_page_v2=True)
    body = _get(client, f"/jobs/{_JOB_ID}?tab={tab}")
    assert _MARKER in body
    assert _active_tabs(body) == [tab]


@pytest.mark.parametrize("query", ["", "?tab=", "?tab=nonsense", "?tab=OVERVIEWX"])
def test_a_missing_or_unknown_tab_renders_overview_with_200(
    client: Any, monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    _session(monkeypatch, job_page_v2=True)
    body = _get(client, f"/jobs/{_JOB_ID}{query}")
    assert _MARKER in body
    assert _active_tabs(body) == ["overview"]


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_settings_is_not_offered_to_a_non_writer_and_falls_back_to_overview(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    _session(monkeypatch, role=role, job_page_v2=True)
    body = _get(client, f"/jobs/{_JOB_ID}?tab=settings")
    assert _active_tabs(body) == ["overview"]
    assert "settings" not in _tab_labels(body)
    assert _tab_labels(body) == {"overview", "candidates", "requirements", "shortlist"}


@pytest.mark.parametrize("role", ["admin", "recruiter"])
def test_a_writer_is_offered_all_five_tabs(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    _session(monkeypatch, role=role, job_page_v2=True)
    body = _get(client, f"/jobs/{_JOB_ID}")
    assert _tab_labels(body) == set(_TABS)


@pytest.mark.parametrize(
    "hostile",
    [
        "<script>alert(1)</script>",
        '"><img src=x onerror=alert(1)>',
        "javascript:alert(1)",
    ],
)
def test_a_hostile_tab_value_is_never_reflected(
    client: Any, monkeypatch: pytest.MonkeyPatch, hostile: str
) -> None:
    _session(monkeypatch, job_page_v2=True)
    resp = client.get(f"/jobs/{_JOB_ID}", query_string={"tab": hostile})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert _MARKER in body
    assert "alert(1)" not in body
    assert "onerror" not in body
    assert _active_tabs(body) == ["overview"]


def test_a_repeated_tab_parameter_does_not_break_the_page(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(monkeypatch, job_page_v2=True)
    body = _get(client, f"/jobs/{_JOB_ID}?tab=settings&tab=candidates")
    assert len(_active_tabs(body)) == 1
