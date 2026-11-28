"""S1 (job-page redesign, Option A) -- the v2 shell: header, stepper, tab nav.

RED: ``job_detail_v2.html`` does not exist yet. Every case turns the layout on
(global flag) and reads the rendered HTML with ``html.parser``, not a regex over
template source.

**Markup hooks this file pins** (the contract fixes the visible text and the
links; it does not fix attribute names, so the tester chose these and the
coder must follow them):

* layout marker: ``data-job-layout="v2"`` anywhere in the page;
* tab nav: one ``<a>`` per visible tab whose text is the tab's name
  (Overview, Candidates, Requirements, Shortlist, Settings), the non-overview
  links carrying ``tab=<name>`` in their ``href``; the ACTIVE tab link has
  ``aria-current="page"`` and no other element on the page does;
* stepper: the CURRENT lifecycle step carries ``aria-current="step"`` and its
  text names the step (Draft, Parsed, Open, Closed);
* next-action: texts and targets exactly as the contract's header table.

Hiring managers and auditors get "View shortlist" only; their header has no
form. The ``transition_status`` hidden ``tab`` field is validated against the
tab enum and never used as a URL.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from src.schemas.jobs import JDExtracted, JobOut, Skill
from src.schemas.resumes import ResumeListItem
from src.settings import Settings
from tests.e2e.driver import page_token

_JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
_TS = dt.datetime(2026, 9, 3, 8, 30, tzinfo=dt.UTC)
_MARKER = 'data-job-layout="v2"'
_TABS = ("overview", "candidates", "requirements", "shortlist", "settings")


# ── a small HTML reader ─────────────────────────────────────────────────────


@dataclass
class _Form:
    action: str
    method: str
    hidden: dict[str, str] = field(default_factory=dict)
    buttons: list[str] = field(default_factory=list)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str, str | None]] = []
        self.forms: list[_Form] = []
        self.steps: list[str] = []
        self.buttons: list[tuple[str, bool]] = []
        self._a: list[str | None] | None = None
        self._a_text: list[str] = []
        self._form: _Form | None = None
        self._btn: tuple[list[str], bool] | None = None
        self._step: tuple[str, int, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        if self._step is not None and tag == self._step[0]:
            self._step = (self._step[0], self._step[1] + 1, self._step[2])
        if d.get("aria-current") == "step" and self._step is None:
            self._step = (tag, 1, [])
        if tag == "a":
            self._a = [d.get("href"), d.get("aria-current")]
            self._a_text = []
        elif tag == "form":
            self._form = _Form(d.get("action") or "", (d.get("method") or "").lower())
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            if d.get("type") == "hidden" and d.get("name"):
                self._form.hidden[str(d["name"])] = d.get("value") or ""
        elif tag == "button":
            self._btn = ([], "disabled" in d)

    def handle_data(self, data: str) -> None:
        if self._a is not None:
            self._a_text.append(data)
        if self._btn is not None:
            self._btn[0].append(data)
        if self._step is not None:
            self._step[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._a is not None:
            text = " ".join("".join(self._a_text).split())
            self.anchors.append((text, self._a[0] or "", self._a[1]))
            self._a = None
        elif tag == "form":
            self._form = None
        elif tag == "button" and self._btn is not None:
            text = " ".join("".join(self._btn[0]).split())
            self.buttons.append((text, self._btn[1]))
            if self._form is not None:
                self._form.buttons.append(text)
            self._btn = None
        if self._step is not None and tag == self._step[0]:
            depth = self._step[1] - 1
            if depth == 0:
                self.steps.append(" ".join("".join(self._step[2]).split()))
                self._step = None
            else:
                self._step = (self._step[0], depth, self._step[2])


def _read(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    return page


def _links(page: _Page, text: str) -> list[str]:
    return [h for t, h, _c in page.anchors if t.lower() == text.lower()]


def _nav(page: _Page) -> dict[str, tuple[str, str | None]]:
    out: dict[str, tuple[str, str | None]] = {}
    for text, href, cur in page.anchors:
        if text.lower() in _TABS:
            out[text.lower()] = (href, cur)
    return out


# ── fixtures ────────────────────────────────────────────────────────────────


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


_STATES: dict[str, dict[str, Any]] = {
    "parsing": {"status": "draft", "parsed_at": None, "description_parsed": None},
    "failed": {
        "status": "draft",
        "parsed_at": None,
        "description_parsed": None,
        "failure_reason": "The model returned no usable JSON.",
    },
    "parsed": {"status": "draft"},
    "open": {"status": "open"},
    "closed": {"status": "closed", "closed_at": _TS},
    "archived": {"status": "archived", "closed_at": _TS},
}


def _arrange(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    *,
    role: str | None = None,
) -> None:
    """Flag on; ``role`` None means a CAS-off dev session (a writer)."""
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
    job = _job(**_STATES[state])
    resume = ResumeListItem(
        id=UUID("22222222-2222-4222-8222-222222222222"),
        original_filename="ada.pdf",
        status="parsed",
        uploaded_at=_TS,
        parsed_at=_TS,
        candidate_name="Ada Lovelace",
    ).model_dump(mode="json")
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: job)
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [resume])
    monkeypatch.setattr(api_client, "list_resume_splits", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "get_latest_resume_export", lambda jid, **kw: None)
    monkeypatch.setattr(api_client, "list_job_assignees", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_users", lambda **kw: [])
    monkeypatch.setattr(api_client, "list_shortlist", lambda jid, **kw: [])


def _get(client: Any, url: str) -> str:
    resp = client.get(url, headers={"Cookie": "ra_session=tok-live"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200, body[:400]
    return body


# ── the header renders on every tab ─────────────────────────────────────────


@pytest.mark.parametrize("tab", _TABS)
def test_the_header_and_marker_render_on_every_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    _arrange(monkeypatch, "open")
    body = _get(client, f"/jobs/{_JOB_ID}?tab={tab}")
    page = _read(body)
    assert _MARKER in body
    assert "Multimedia Specialist" in body
    assert page.steps, "the lifecycle stepper must mark its current step"
    assert _links(page, "Add candidates"), "the open-job next action is missing"


# ── the stepper ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "current"),
    [
        ("parsing", "Draft"),
        ("failed", "Draft"),
        ("parsed", "Parsed"),
        ("open", "Open"),
        ("closed", "Closed"),
        ("archived", "Closed"),
    ],
)
def test_the_stepper_marks_exactly_one_current_step(
    client: Any, monkeypatch: pytest.MonkeyPatch, state: str, current: str
) -> None:
    _arrange(monkeypatch, state)
    page = _read(_get(client, f"/jobs/{_JOB_ID}"))
    assert len(page.steps) == 1
    assert current.lower() in page.steps[0].lower()


def test_an_archived_job_shows_an_archived_pill(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(monkeypatch, "archived")
    assert "archived" in _get(client, f"/jobs/{_JOB_ID}").lower()


# ── the next action, per state, for a writer ────────────────────────────────


def test_a_parsing_draft_offers_a_disabled_wait_and_a_refresh_link(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(monkeypatch, "parsing")
    page = _read(_get(client, f"/jobs/{_JOB_ID}?tab=candidates"))
    waiting = [(t, dis) for t, dis in page.buttons if "waiting for parse" in t.lower()]
    assert waiting and all(dis for _t, dis in waiting)
    refresh = _links(page, "Refresh status")
    assert refresh, "a parsing draft must offer a Refresh status link"
    assert f"/jobs/{_JOB_ID}" in refresh[0]
    assert "tab=candidates" in refresh[0], "refresh stays on the same tab"
    assert not _links(page, "Add candidates")
    assert not [f for f in page.forms if f.action.endswith("/status")]


def test_a_parse_failure_links_to_the_reparse_control_on_requirements(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(monkeypatch, "failed")
    page = _read(_get(client, f"/jobs/{_JOB_ID}"))
    links = _links(page, "Re-parse the job description")
    assert links and "tab=requirements" in links[0]
    assert f"/jobs/{_JOB_ID}" in links[0]
    assert not [f for f in page.forms if f.action.endswith("/status")]


def test_a_parsed_draft_offers_the_open_form_with_a_token_and_a_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(monkeypatch, "parsed")
    body = _get(client, f"/jobs/{_JOB_ID}?tab=requirements")
    page = _read(body)
    forms = [f for f in page.forms if f.hidden.get("to") == "open"]
    assert len(forms) == 1
    form = forms[0]
    assert form.method == "post"
    assert form.action == f"/jobs/{_JOB_ID}/status"
    assert any("open for applicants" in b.lower() for b in form.buttons)
    assert form.hidden.get("csrf_token"), "the header form needs its page token"
    assert form.hidden["csrf_token"] == page_token(body)
    assert form.hidden.get("tab") in _TABS, "the form must carry a valid tab"


def test_an_open_job_offers_add_candidates_and_view_shortlist(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(monkeypatch, "open")
    page = _read(_get(client, f"/jobs/{_JOB_ID}"))
    add = _links(page, "Add candidates")
    assert add and "tab=candidates" in add[0]
    shortlist = _links(page, "View shortlist")
    assert shortlist and shortlist[0].endswith(f"/jobs/{_JOB_ID}/shortlist")


def test_a_closed_job_offers_view_shortlist_and_no_intake_action(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(monkeypatch, "closed")
    page = _read(_get(client, f"/jobs/{_JOB_ID}"))
    assert _links(page, "View shortlist")
    assert not _links(page, "Add candidates")
    assert not [f for f in page.forms if f.hidden.get("to") == "open"]


def test_an_archived_job_has_no_next_action(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(monkeypatch, "archived")
    page = _read(_get(client, f"/jobs/{_JOB_ID}"))
    assert not _links(page, "Add candidates")
    assert not _links(page, "Re-parse the job description")
    assert not [t for t, _d in page.buttons if "waiting for parse" in t.lower()]
    assert not [f for f in page.forms if f.hidden.get("to") == "open"]


# ── hiring manager and auditor: View shortlist only ─────────────────────────


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
@pytest.mark.parametrize("state", ["parsing", "failed", "parsed", "open", "closed"])
def test_a_non_writer_header_offers_view_shortlist_only(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str, state: str
) -> None:
    _arrange(monkeypatch, state, role=role)
    body = _get(client, f"/jobs/{_JOB_ID}")
    page = _read(body)
    assert _MARKER in body
    assert _links(page, "View shortlist")
    assert not _links(page, "Add candidates")
    assert not _links(page, "Re-parse the job description")
    assert not _links(page, "Refresh status")
    assert not [t for t, _d in page.buttons if "open for applicants" in t.lower()]
    assert not [f for f in page.forms if f.action.endswith("/status")]


# ── the tab nav ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tab", _TABS)
def test_the_nav_links_carry_a_tab_and_only_the_active_one_is_current(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    _arrange(monkeypatch, "open")
    page = _read(_get(client, f"/jobs/{_JOB_ID}?tab={tab}"))
    nav = _nav(page)
    assert set(nav) == set(_TABS)
    for name, (href, current) in nav.items():
        assert f"/jobs/{_JOB_ID}" in href
        if name != "overview":
            assert f"tab={name}" in href
        assert (current == "page") is (name == tab), name
    assert [c for _t, _h, c in page.anchors if c == "page"] == ["page"]


# ── the page token ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("tab", _TABS)
def test_every_tab_carries_the_page_token_for_htmx_and_the_e2e_driver(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    _arrange(monkeypatch, "open")
    body = _get(client, f"/jobs/{_JOB_ID}?tab={tab}")
    assert _MARKER in body
    assert page_token(body), "hx-headers must carry the page token on every tab"


# ── POST /jobs/<id>/status honours a validated tab ──────────────────────────


@pytest.mark.parametrize("tab", _TABS)
def test_a_valid_tab_field_is_honoured_in_the_redirect(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    _arrange(monkeypatch, "parsed")
    spy = MagicMock(return_value={"id": str(_JOB_ID), "status": "open"})
    monkeypatch.setattr(api_client, "transition_status", spy)
    resp = client.post(f"/jobs/{_JOB_ID}/status", data={"to": "open", "tab": tab})
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert f"/jobs/{_JOB_ID}" in location
    if tab != "overview":
        assert location.endswith(f"?tab={tab}")
    spy.assert_called_once()


@pytest.mark.parametrize(
    "junk",
    [
        "",
        "nonsense",
        "//evil.example/x",
        "https://evil.example/",
        "../../admin",
        "settings&next=//evil.example",
        "candidates\r\nSet-Cookie: x=1",
    ],
)
def test_a_junk_tab_field_falls_back_to_overview_and_is_never_a_url(
    client: Any, monkeypatch: pytest.MonkeyPatch, junk: str
) -> None:
    _arrange(monkeypatch, "parsed")
    monkeypatch.setattr(
        api_client,
        "transition_status",
        MagicMock(return_value={"id": str(_JOB_ID), "status": "open"}),
    )
    resp = client.post(f"/jobs/{_JOB_ID}/status", data={"to": "open", "tab": junk})
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "evil" not in location
    assert "Set-Cookie" not in location
    assert location.endswith((f"/jobs/{_JOB_ID}", f"/jobs/{_JOB_ID}?tab=overview"))


def test_a_missing_tab_field_redirects_to_overview(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(monkeypatch, "parsed")
    monkeypatch.setattr(
        api_client,
        "transition_status",
        MagicMock(return_value={"id": str(_JOB_ID), "status": "open"}),
    )
    resp = client.post(f"/jobs/{_JOB_ID}/status", data={"to": "open"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(
        (f"/jobs/{_JOB_ID}", f"/jobs/{_JOB_ID}?tab=overview")
    )


def test_the_tab_field_is_ignored_when_v2_is_off(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arrange(monkeypatch, "parsed")
    settings = Settings(cas_enabled=False, job_page_v2=False)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client,
        "transition_status",
        MagicMock(return_value={"id": str(_JOB_ID), "status": "open"}),
    )
    resp = client.post(
        f"/jobs/{_JOB_ID}/status", data={"to": "open", "tab": "settings"}
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{_JOB_ID}")
    assert "tab=" not in resp.headers["Location"]
