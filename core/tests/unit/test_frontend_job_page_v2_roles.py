"""S4 (job-page redesign, Option A) -- the role matrix, section 9.

Pins, for admin, recruiter, hiring_manager and auditor (and the CAS-off dev
session, which is a writer): which tabs each role is offered, what the header
offers, and which controls each tab carries. Then, for every control a role
does NOT see, that the route it would post to still answers 403 when the backend
refuses (ADR-033).

**Hiding is a compensating UX control only.** The backend is the boundary
(``require_session_role``). A hidden control whose route turned the backend's 403
into a 500, a redirect or a re-rendered 400 would make the hiding the only
defence the user meets, so each hidden form route gets one representative 403
here, following ``test_frontend_write_route_403_handling.py``.

Much of the render matrix is already true after S1-S3 and is pinned again here as
ONE table so a later slice cannot quietly loosen a role's view. What is new in S4
is the Shortlist row (teaser and link for every role) and the 403 on the four
intake routes. The latter is a defect found while writing this file:
``upload_resumes``, ``upload_candidate_roster``, ``split_upload`` and
``export_start`` catch ``BadRequest`` and turn the backend's 403 into a 400
re-render or a flash plus a redirect, while ``transition_status``, ``edit_job_*``,
``reparse_job``, the assignee routes and ``blind_review`` abort with the real
status. See the report that accompanies this file.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from frontend import api_client
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import JOB_ID, Node

_WRITER_ROLES: tuple[str | None, ...] = ("admin", "recruiter", None)
_READER_ROLES: tuple[str, ...] = ("hiring_manager", "auditor")
_ALL_ROLES: tuple[str | None, ...] = (*_WRITER_ROLES, *_READER_ROLES)
_WRITER = pytest.mark.parametrize("role", _WRITER_ROLES, ids=lambda r: r or "cas-off")
_READER = pytest.mark.parametrize("role", _READER_ROLES)
_ANY = pytest.mark.parametrize("role", _ALL_ROLES, ids=lambda r: r or "cas-off")

_ALL_TABS = ["Overview", "Candidates", "Requirements", "Shortlist", "Settings"]
_READER_TABS = ["Overview", "Candidates", "Requirements", "Shortlist"]
_TAB_KEYS = ("overview", "candidates", "requirements", "shortlist", "settings")
_CARDS = {"single", "zip", "taleo-pdf", "roster"}


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _get(client: Any, tab: str | None = None, *, status: int = 200) -> Node:
    url = f"/jobs/{JOB_ID}" + (f"?tab={tab}" if tab else "")
    resp = client.get(url, headers={"Cookie": "ra_session=tok-live"})
    text = resp.get_data(as_text=True)
    assert resp.status_code == status, text[:400]
    return dom.parse(text)


def _wrapper(root: Node, tab: str) -> Node:
    node = dom.tab_root(root, tab)
    assert node is not None, f'the page has no data-job-tab="{tab}" element'
    return node


def _nav(root: Node) -> list[str]:
    nav = root.find("nav", {"aria-label": "Job sections"})
    assert nav is not None, "no tab nav"
    return [a.text() for a in nav.find_all("a")]


def _current(root: Node) -> list[str]:
    return [a.text() for a in root.find_all("a", {"aria-current": "page"})]


def _actions(root: Node) -> Node:
    node = root.find("div", {"class": "actions"})
    assert node is not None, "the header has no actions row"
    return node


# ── the tab nav ─────────────────────────────────────────────────────────────


@_WRITER
@pytest.mark.parametrize("tab", [None, *_TAB_KEYS])
def test_a_writer_is_offered_all_five_tabs(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None, tab: str | None
) -> None:
    dom.install(monkeypatch, role=role)
    assert _nav(_get(client, tab)) == _ALL_TABS


@_READER
@pytest.mark.parametrize("tab", [None, *_TAB_KEYS])
def test_a_non_writer_is_offered_four_tabs_and_never_settings(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str, tab: str | None
) -> None:
    dom.install(monkeypatch, role=role)
    assert _nav(_get(client, tab)) == _READER_TABS


@_READER
def test_settings_for_a_non_writer_falls_back_to_overview_with_a_200(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role)
    reads = dom.stub_settings_reads(monkeypatch)
    root = _get(client, "settings")
    assert _current(root) == ["Overview"]
    assert dom.tab_root(root, "settings") is None
    assert reads.list_job_assignees.call_count == 0
    assert reads.list_users.call_count == 0


# ── the header's next action ────────────────────────────────────────────────


@_READER
@pytest.mark.parametrize("state", ["parsing", "parsed", "open", "closed"])
def test_a_non_writer_header_is_view_shortlist_and_nothing_else(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str, state: str
) -> None:
    dom.install(monkeypatch, role=role, state=state)
    actions = _actions(_get(client))
    assert [a.text() for a in actions.find_all("a")] == ["View shortlist"]
    (link,) = actions.find_all("a")
    assert link.get("href") == f"/jobs/{JOB_ID}/shortlist"
    assert actions.find_all("form") == []
    assert actions.find_all("button") == []


@_WRITER
def test_a_writer_header_on_a_parsed_draft_offers_the_open_form(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, state="parsed")
    actions = _actions(_get(client))
    forms = dom.forms_posting_to(actions, f"/jobs/{JOB_ID}/status")
    assert len(forms) == 1
    assert dom.hidden_inputs(forms[0])["to"] == "open"
    assert "Open for applicants" in [b.text() for b in forms[0].find_all("button")]


@_WRITER
def test_a_writer_header_on_an_open_job_offers_add_candidates_and_the_shortlist(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, state="open")
    texts = [a.text() for a in _actions(_get(client)).find_all("a")]
    assert texts == ["Add candidates", "View shortlist"]


# ── Overview ────────────────────────────────────────────────────────────────


@_READER
def test_a_non_writer_overview_carries_no_job_write_form(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role, state="parsed")
    root = _get(client)
    posts = [
        f
        for f in root.find_all("form")
        if (f.get("method") or "").lower() == "post"
        and (f.get("action") or "").startswith(("/jobs/", "/resume-exports/"))
    ]
    assert posts == []


# ── Candidates ──────────────────────────────────────────────────────────────


@_WRITER
def test_a_writer_sees_the_table_the_four_cards_and_the_export_menu(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, export=dom.make_export("ready"))
    tab = _wrapper(_get(client, "candidates"), "candidates")
    assert tab.find(attrs={"id": "resumes-table"}) is not None
    cards = tab.find_all(attrs={"data-intake-card": None})
    keys = {str(n.get("data-intake-card")) for n in cards}
    assert keys == _CARDS
    assert tab.find(attrs={"data-export-menu": None}) is not None
    assert "Read-only for your role" not in tab.text()


@_READER
def test_a_non_writer_sees_the_table_and_a_read_only_note_only(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role, export=dom.make_export("ready"))
    tab = _wrapper(_get(client, "candidates"), "candidates")
    assert tab.find(attrs={"id": "resumes-table"}) is not None
    assert "Read-only for your role" in tab.text()
    assert tab.find_all(attrs={"data-intake-card": None}) == []
    assert tab.find(attrs={"data-export-menu": None}) is None
    assert tab.find_all("form") == []


@_READER
def test_a_non_writer_candidates_tab_reads_no_splits_and_no_export(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    spies = dom.install(monkeypatch, role=role, export=dom.make_export("ready"))
    _get(client, "candidates")
    assert spies.list_resumes.call_count == 1
    assert spies.list_resume_splits.call_count == 0
    assert spies.get_latest_resume_export.call_count == 0


# ── Requirements ────────────────────────────────────────────────────────────


@_WRITER
def test_a_writer_sees_the_requirements_forms(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, state="parsed")
    tab = _wrapper(_get(client, "requirements"), "requirements")
    assert len(dom.forms_posting_to(tab, f"/jobs/{JOB_ID}/requirements")) == 1
    assert len(dom.forms_posting_to(tab, f"/jobs/{JOB_ID}/description")) == 1


@_READER
def test_a_non_writer_sees_the_requirements_read_only(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(
        monkeypatch,
        role=role,
        state="parsed",
        job_over={"additional_requirements": "Must have MEG analysis."},
    )
    tab = _wrapper(_get(client, "requirements"), "requirements")
    assert "Video editing" in tab.text()
    note = tab.find(attrs={"class": "manager-note"})
    assert note is not None and "Must have MEG analysis." in note.text()
    assert tab.find_all("form") == []
    assert tab.find_all("textarea") == []
    assert tab.find_all("button") == []


# ── Shortlist: teaser and link for every role ───────────────────────────────


@_ANY
def test_every_role_sees_the_shortlist_teaser_and_the_link(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role)
    dom.stub_shortlist(monkeypatch, [dom.make_shortlist_entry(i) for i in (1, 2, 3)])
    tab = _wrapper(_get(client, "shortlist"), "shortlist")
    assert len(tab.find_all(attrs={"data-shortlist-row": None})) == 3
    links = [a for a in tab.find_all("a") if "open full shortlist" in a.text().lower()]
    assert [a.get("href") for a in links] == [f"/jobs/{JOB_ID}/shortlist"]


@_READER
def test_a_non_writer_teaser_carries_no_decision_control(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role)
    dom.stub_shortlist(monkeypatch, [dom.make_shortlist_entry(1)])
    tab = _wrapper(_get(client, "shortlist"), "shortlist")
    assert tab.find_all("form") == []
    assert tab.find_all("button") == []


# ── Settings ────────────────────────────────────────────────────────────────


@_WRITER
def test_a_writer_sees_the_settings_forms(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, state="open")
    dom.stub_settings_reads(monkeypatch, users=[dom.make_user(1)])
    tab = _wrapper(_get(client, "settings"), "settings")
    for suffix in ("/details", "/assignees", "/blind-review", "/status"):
        assert dom.forms_posting_to(tab, f"/jobs/{JOB_ID}{suffix}"), suffix


# ── the hidden controls' routes still 403 when the backend refuses ──────────


def _forbidden() -> api_client.BadRequest:
    return api_client.BadRequest(
        "backend 403: session role not permitted for this route",
        status_code=403,
        detail={"detail": "session role not permitted for this route"},
    )


_USER = UUID(int=0x77777777000000000000000000000001)


def _pdf() -> dict[str, Any]:
    return {
        "consent_acknowledged": "true",
        "files": (io.BytesIO(b"%PDF-1.4 x"), "a.pdf"),
    }


# (what the hidden control is, route, api_client function it reaches, form data)
_HIDDEN: list[tuple[str, str, str, Callable[[], dict[str, Any]]]] = [
    (
        "open-form",
        f"/jobs/{JOB_ID}/status",
        "transition_status",
        lambda: {"to": "open"},
    ),
    (
        "details",
        f"/jobs/{JOB_ID}/details",
        "patch_job",
        lambda: {"department": "Library"},
    ),
    (
        "requirements",
        f"/jobs/{JOB_ID}/requirements",
        "patch_job",
        lambda: {"additional_requirements": "Must know MEG."},
    ),
    (
        "description",
        f"/jobs/{JOB_ID}/description",
        "patch_job",
        lambda: {"description_raw": "A new description. " * 5},
    ),
    ("reparse", f"/jobs/{JOB_ID}/reparse", "reparse_job", lambda: {}),
    (
        "assignee-add",
        f"/jobs/{JOB_ID}/assignees",
        "add_job_assignee",
        lambda: {"user_id": str(_USER)},
    ),
    (
        "assignee-remove",
        f"/jobs/{JOB_ID}/assignees/{_USER}/remove",
        "remove_job_assignee",
        lambda: {},
    ),
    (
        "blind-review",
        f"/jobs/{JOB_ID}/blind-review",
        "patch_job",
        lambda: {"blind_review": "true"},
    ),
    ("upload-resumes", f"/jobs/{JOB_ID}/resumes", "upload_resumes", _pdf),
    (
        "split-upload",
        f"/jobs/{JOB_ID}/resumes/split-upload",
        "split_upload",
        _pdf,
    ),
    (
        "roster",
        f"/jobs/{JOB_ID}/candidate-roster",
        "upload_candidate_roster",
        lambda: {"file": (io.BytesIO(b"a,b\n1,2\n"), "roster.csv")},
    ),
    (
        "export-start",
        f"/jobs/{JOB_ID}/resumes/exports",
        "start_resume_export",
        lambda: {},
    ),
]


@pytest.mark.parametrize("role", _READER_ROLES)
@pytest.mark.parametrize(
    ("route", "func", "data"),
    [(path, func, data) for _name, path, func, data in _HIDDEN],
    ids=[name for name, *_rest in _HIDDEN],
)
def test_a_hidden_controls_route_still_403s_when_the_backend_refuses(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    route: str,
    func: str,
    data: Callable[[], dict[str, Any]],
) -> None:
    dom.install(monkeypatch, role=role)
    refused = MagicMock(side_effect=_forbidden())
    monkeypatch.setattr(api_client, func, refused)

    resp = client.post(route, data=data(), headers={"Cookie": "ra_session=tok-live"})

    # The request must have reached the backend: a 403 from the CSRF gate or
    # the consent check would prove nothing about the backend's refusal.
    assert refused.call_count == 1, f"{func} was never reached ({resp.status_code})"
    assert resp.status_code == 403, (
        f"{route}: expected 403, got {resp.status_code} "
        f"(Location={resp.headers.get('Location')!r})"
    )
