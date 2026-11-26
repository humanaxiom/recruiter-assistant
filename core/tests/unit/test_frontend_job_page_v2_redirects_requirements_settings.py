"""S3 (job-page redesign, Option A) -- redirect and re-render targets.

RED: the Requirements and Settings handlers still redirect to the bare
``/jobs/<id>``; three 409 re-renders still draw the Overview; and the Settings
tab (a placeholder) carries none of the forms that would send a ``tab``.

Spec: ``S-contract.md`` section 3.1 rows 9 to 16:

====  ============================  ================  =====================
row   handler                       success redirect  409 re-render
====  ============================  ================  =====================
9     ``transition_status``         Settings form:    the tab the form named
                                    ``settings``;
                                    header Open
                                    button: its
                                    hidden tab
10    ``edit_job_details``          ``settings``      n/a
11    ``edit_job_requirements``     ``requirements``  n/a
12    ``reparse_job``               ``requirements``  ``requirements``
13    ``add_job_assignee_route``    ``settings``      n/a
14    ``remove_job_assignee_route`` ``settings``      n/a
15    ``edit_job_description``      ``requirements``  ``requirements``
16    ``blind_review``              ``settings``      n/a
====  ============================  ================  =====================

With the flag ON the ``Location`` is ``/jobs/<id>?tab=<tab>`` (path and query
asserted separately, never by substring). With the flag OFF it is the exact v1
bare ``/jobs/<id>`` (a parity twin of the existing ``Location.endswith``
assertions, which pass today and must keep passing). A 409 re-render keeps its
status and shows the v2 page with the matching tab active when v2 is on, and
the v1 page when it is off.

Where a row has TWO success redirects in the handler (details, requirements,
description each redirect early when the form field is missing) both are driven.
The Settings status forms and the header Open form are taken FROM the rendered
page, so the hidden ``tab`` they carry is part of what is pinned.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from frontend import api_client
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import JOB_ID, Node

_FLAGS = pytest.mark.parametrize("flag", [True, False], ids=["v2", "v1"])
_Scenario = Callable[[Any, pytest.MonkeyPatch], Any]
_USER_ID = dom.make_user(1)["id"]


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _on(resp: Any, tab: str) -> None:
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    where = urlsplit(resp.headers["Location"])
    assert where.path == f"/jobs/{JOB_ID}"
    assert parse_qs(where.query) == {"tab": [tab]}


def _bare(resp: Any) -> None:
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    location = resp.headers["Location"]
    assert location.endswith(f"/jobs/{JOB_ID}")
    assert "?" not in location


def _conflict(message: str) -> api_client.Conflict:
    detail = {"message": message}
    return api_client.Conflict("backend 409", status_code=409, detail=detail)


def _form_from_page(
    client: Any, tab: str, suffix: str, *, to: str | None = None
) -> Node:
    body = client.get(f"/jobs/{JOB_ID}?tab={tab}").get_data(as_text=True)
    root = dom.parse(body)
    found = dom.forms_posting_to(root, f"/jobs/{JOB_ID}{suffix}")
    if to is not None:
        found = [f for f in found if dom.hidden_inputs(f).get("to") == to]
    assert len(found) == 1, f"expected one {suffix} form (to={to}) on {tab}"
    return found[0]


# ── scenarios: each performs one POST that succeeds and lands on a tab ──────


def _details_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(return_value={})
    mp.setattr(api_client, "patch_job", spy)
    data = {"department": "School of Medicine", "location": "Burnaby"}
    return client.post(f"/jobs/{JOB_ID}/details", data=data)


def _details_nothing_submitted(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(return_value={})
    mp.setattr(api_client, "patch_job", spy)
    resp = client.post(f"/jobs/{JOB_ID}/details", data={})
    spy.assert_not_called()
    return resp


def _requirements_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(api_client, "patch_job", MagicMock(return_value={}))
    data = {"additional_requirements": "Must have MEG analysis."}
    return client.post(f"/jobs/{JOB_ID}/requirements", data=data)


def _requirements_nothing_submitted(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(return_value={})
    mp.setattr(api_client, "patch_job", spy)
    resp = client.post(f"/jobs/{JOB_ID}/requirements", data={})
    spy.assert_not_called()
    return resp


def _reparse_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(api_client, "reparse_job", MagicMock(return_value={}))
    return client.post(f"/jobs/{JOB_ID}/reparse")


def _assignee_add_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(api_client, "add_job_assignee", MagicMock(return_value={}))
    return client.post(f"/jobs/{JOB_ID}/assignees", data={"user_id": _USER_ID})


def _assignee_remove_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(api_client, "remove_job_assignee", MagicMock(return_value=None))
    return client.post(f"/jobs/{JOB_ID}/assignees/{_USER_ID}/remove")


def _description_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(api_client, "patch_job", MagicMock(return_value={}))
    data = {"description_raw": "A corrected job description."}
    return client.post(f"/jobs/{JOB_ID}/description", data=data)


def _description_nothing_submitted(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(return_value={})
    mp.setattr(api_client, "patch_job", spy)
    resp = client.post(f"/jobs/{JOB_ID}/description", data={})
    spy.assert_not_called()
    return resp


def _blind_review_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(return_value={})
    mp.setattr(api_client, "patch_job", spy)
    resp = client.post(f"/jobs/{JOB_ID}/blind-review", data={"blind_review": "true"})
    spy.assert_called_once()
    return resp


def _status_from_settings(to: str) -> _Scenario:
    def scenario(client: Any, mp: pytest.MonkeyPatch) -> Any:
        spy = MagicMock(return_value={})
        mp.setattr(api_client, "transition_status", spy)
        form = _form_from_page(client, "settings", "/status", to=to)
        resp = client.post(form.get("action") or "", data=dom.hidden_inputs(form))
        if resp.status_code == 302:
            spy.assert_called_once()
        return resp

    return scenario


_SUCCESS: dict[str, tuple[_Scenario, str]] = {
    "9-status-closed-from-settings": (_status_from_settings("closed"), "settings"),
    "9-status-archived-from-settings": (_status_from_settings("archived"), "settings"),
    "10-details": (_details_ok, "settings"),
    "10-details-nothing-submitted": (_details_nothing_submitted, "settings"),
    "11-requirements": (_requirements_ok, "requirements"),
    "11-requirements-nothing-submitted": (
        _requirements_nothing_submitted,
        "requirements",
    ),
    "12-reparse": (_reparse_ok, "requirements"),
    "13-assignee-add": (_assignee_add_ok, "settings"),
    "14-assignee-remove": (_assignee_remove_ok, "settings"),
    "15-description": (_description_ok, "requirements"),
    "15-description-nothing-submitted": (
        _description_nothing_submitted,
        "requirements",
    ),
    "16-blind-review": (_blind_review_ok, "settings"),
}


@_FLAGS
@pytest.mark.parametrize("name", list(_SUCCESS))
def test_the_success_redirect_lands_on_its_tab_only_when_v2_is_on(
    client: Any, monkeypatch: pytest.MonkeyPatch, flag: bool, name: str
) -> None:
    dom.install(monkeypatch, flag=flag)
    scenario, tab = _SUCCESS[name]

    resp = scenario(client, monkeypatch)

    if flag:
        _on(resp, tab)
    else:
        _bare(resp)


# ── row 9: the header Open button keeps the tab it was clicked on ───────────


def _header_open_form(client: Any, tab: str) -> Node:
    body = client.get(f"/jobs/{JOB_ID}?tab={tab}").get_data(as_text=True)
    root = dom.parse(body)
    wrapper = dom.tab_root(root, tab)
    found = [
        f
        for f in dom.forms_posting_to(root, f"/jobs/{JOB_ID}/status")
        if dom.hidden_inputs(f).get("to") == "open"
        and (wrapper is None or not f.within(wrapper))
    ]
    assert len(found) == 1, f"the header has no Open form on {tab}"
    return found[0]


@pytest.mark.parametrize("tab", ["candidates", "requirements", "settings"])
def test_the_header_open_button_carries_and_keeps_its_validated_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    dom.install(monkeypatch, state="parsed")
    monkeypatch.setattr(api_client, "transition_status", MagicMock(return_value={}))
    form = _header_open_form(client, tab)
    assert dom.hidden_inputs(form)["tab"] == tab

    resp = client.post(form.get("action") or "", data=dom.hidden_inputs(form))

    _on(resp, tab)


@pytest.mark.parametrize("tab", ["requirements", "settings"])
def test_the_header_open_button_lands_back_on_the_v1_page_when_v2_is_off(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    dom.install(monkeypatch, state="parsed", flag=False)
    monkeypatch.setattr(api_client, "transition_status", MagicMock(return_value={}))

    resp = client.post(f"/jobs/{JOB_ID}/status", data={"to": "open", "tab": tab})

    _bare(resp)


@pytest.mark.parametrize("junk", ["<script>", "../../x", "Settings"])
def test_a_forged_status_tab_falls_back_and_is_never_reflected(
    client: Any, monkeypatch: pytest.MonkeyPatch, junk: str
) -> None:
    dom.install(monkeypatch, state="parsed")
    monkeypatch.setattr(api_client, "transition_status", MagicMock(return_value={}))

    resp = client.post(f"/jobs/{JOB_ID}/status", data={"to": "open", "tab": junk})

    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert urlsplit(location).path == f"/jobs/{JOB_ID}"
    assert junk not in location
    assert "script" not in location


# ── the Settings status forms stay on Settings ──────────────────────────────


def test_every_settings_status_form_names_the_settings_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, state="parsed")
    for to in ("open", "archived"):
        form = _form_from_page(client, "settings", "/status", to=to)
        assert dom.hidden_inputs(form)["tab"] == "settings", to


# ── 409 re-renders: the matching tab, status kept; v1 page when v2 is off ───


def _reparse_conflict(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(side_effect=_conflict("A parse is already running."))
    mp.setattr(api_client, "reparse_job", spy)
    return client.post(f"/jobs/{JOB_ID}/reparse")


def _description_conflict(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(side_effect=_conflict("Only a draft job can be edited."))
    mp.setattr(api_client, "patch_job", spy)
    data = {"description_raw": "New text."}
    return client.post(f"/jobs/{JOB_ID}/description", data=data)


def _status_conflict_from(tab: str) -> _Scenario:
    def scenario(client: Any, mp: pytest.MonkeyPatch) -> Any:
        spy = MagicMock(side_effect=_conflict("That transition is not allowed."))
        mp.setattr(api_client, "transition_status", spy)
        return client.post(f"/jobs/{JOB_ID}/status", data={"to": "closed", "tab": tab})

    return scenario


def _status_conflict_from_settings_form(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(side_effect=_conflict("That transition is not allowed."))
    mp.setattr(api_client, "transition_status", spy)
    form = _form_from_page(client, "settings", "/status", to="closed")
    return client.post(form.get("action") or "", data=dom.hidden_inputs(form))


_CONFLICTS: dict[str, tuple[_Scenario, str, str, str]] = {
    # name: (scenario, job state, tab on screen, message)
    "12-reparse": (
        _reparse_conflict,
        "failed",
        "requirements",
        "A parse is already running.",
    ),
    "15-description": (
        _description_conflict,
        "open",
        "requirements",
        "Only a draft job can be edited.",
    ),
    "9-status-from-requirements": (
        _status_conflict_from("requirements"),
        "open",
        "requirements",
        "That transition is not allowed.",
    ),
    "9-status-from-settings": (
        _status_conflict_from("settings"),
        "open",
        "settings",
        "That transition is not allowed.",
    ),
    "9-status-from-the-settings-form": (
        _status_conflict_from_settings_form,
        "open",
        "settings",
        "That transition is not allowed.",
    ),
}


@pytest.mark.parametrize("name", list(_CONFLICTS))
def test_a_409_rerender_shows_the_matching_tab_with_the_error_when_v2_is_on(
    client: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    scenario, state, tab, message = _CONFLICTS[name]
    dom.install(monkeypatch, state=state)

    resp = scenario(client, monkeypatch)

    body = resp.get_data(as_text=True)
    assert resp.status_code == 409, body[:300]
    root = dom.parse(body)
    assert dom.tab_root(root, tab) is not None, f"the re-render is not {tab}"
    current = [a.text() for a in root.find_all("a", {"aria-current": "page"})]
    assert current == [tab.capitalize()]
    errors = root.find_all("p", {"class": "error"})
    assert any(message in e.text() for e in errors)


@pytest.mark.parametrize("name", list(_CONFLICTS))
def test_a_409_rerender_is_the_v1_page_when_v2_is_off(
    client: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    scenario, state, _tab, message = _CONFLICTS[name]
    dom.install(monkeypatch, state=state, flag=False)

    resp = scenario(client, monkeypatch)

    body = resp.get_data(as_text=True)
    assert resp.status_code == 409, body[:300]
    assert "data-job-layout" not in body
    assert "data-job-tab" not in body
    assert message in dom.parse(body).text()
