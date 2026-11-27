"""S6 (job-page redesign) -- editable job details.

RED: the Settings tab still carries a department-and-campus form only,
``edit_job_details`` accepts only ``department`` and ``location`` and does no
validation, the header has no "Edit details" link, and the Requirements tab
shows nothing at all for the description of a job that is no longer a draft.

Spec: ``docs/plans/2026-11-job-page-redesign/S-contract.md`` section 3 "S6".
Product-owner decisions: one Job details form (title, minimum years, seniority,
employment type, department, campus) that REPLACES the Settings department and
campus form and posts to the same route; the description stays editable on a
draft only, and is shown read-only (with a close-and-recreate note) otherwise;
v1 is untouched and its form still posts only department and location.

**Markup hooks this file pins** (the contract fixes ids and text, not the rest):

* the form is ``id="job-details"`` and posts to ``/jobs/<id>/details``;
* its fields are named ``title``, ``min_years``, ``seniority``,
  ``employment_type``, ``department``, ``location``;
* the header link reads "Edit details" and points at ``?tab=settings`` with the
  fragment ``#job-details``;
* a validation failure re-renders through ``_render_job_detail`` (the page's
  ``p.error``) on the Settings tab with status 400 and makes NO ``patch_job``.

Fixtures come from ``tests/unit/job_page_v2_dom.py`` (DTO-built, ISO-string
timestamps); pages are parsed with its DOM helper, never a regex.
"""

from __future__ import annotations

from typing import Any, get_args
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from frontend import api_client
from src.schemas.jobs import EmploymentType, Seniority
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import JOB_ID, Node

_WRITER_ROLES: tuple[str | None, ...] = ("admin", "recruiter", None)
_READER_ROLES: tuple[str, ...] = ("hiring_manager", "auditor")
_WRITER = pytest.mark.parametrize("role", _WRITER_ROLES, ids=lambda r: r or "cas-off")
_READER = pytest.mark.parametrize("role", _READER_ROLES)
_TABS = ("overview", "candidates", "requirements", "shortlist", "settings")
_ALL_TABS = pytest.mark.parametrize("tab", _TABS)

_DETAILS = f"/jobs/{JOB_ID}/details"


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _body(client: Any, tab: str) -> str:
    resp = client.get(
        f"/jobs/{JOB_ID}?tab={tab}", headers={"Cookie": "ra_session=tok-live"}
    )
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200, text[:400]
    return text


def _root(client: Any, tab: str) -> Node:
    return dom.parse(_body(client, tab))


def _details_forms(root: Node) -> list[Node]:
    return dom.forms_posting_to(root, "/details")


def _job_details_form(client: Any, tab: str = "settings") -> Node:
    root = _root(client, tab)
    found = [f for f in root.find_all("form") if f.get("id") == "job-details"]
    assert len(found) == 1, "expected exactly one form with id=job-details"
    return found[0]


def _edit_links(root: Node) -> list[Node]:
    return [a for a in root.find_all("a") if a.text() == "Edit details"]


def _select_values(form: Node, name: str) -> list[str]:
    select = form.find("select", {"name": name})
    assert select is not None, f"no <select name={name}>"
    return [o.get("value") or "" for o in select.find_all("option")]


def _post(client: Any, mp: pytest.MonkeyPatch, data: dict[str, str]) -> Any:
    spy = MagicMock(return_value={})
    mp.setattr(api_client, "patch_job", spy)
    resp = client.post(_DETAILS, data=data)
    resp.spy = spy
    return resp


def _on_settings(resp: Any) -> None:
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    where = urlsplit(resp.headers["Location"])
    assert where.path == f"/jobs/{JOB_ID}"
    assert parse_qs(where.query) == {"tab": ["settings"]}


# ── the form: writers ───────────────────────────────────────────────────────


@_WRITER
def test_a_writer_sees_one_job_details_form_posting_to_details(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role)
    form = _job_details_form(client)
    assert (form.get("action") or "").endswith(_DETAILS)
    assert (form.get("method") or "").lower() == "post"
    assert dom.hidden_inputs(form).get("csrf_token")


@_WRITER
def test_the_form_is_prefilled_from_the_job(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(
        monkeypatch,
        role=role,
        job_over={
            "title": "Research Analyst",
            "min_years": 7,
            "seniority": "senior",
            "employment_type": "contract",
            "department": "School of Medicine",
            "location": "Burnaby",
        },
    )
    form = _job_details_form(client)
    expected = {
        "title": "Research Analyst",
        "min_years": "7",
        "department": "School of Medicine",
        "location": "Burnaby",
    }
    for name, value in expected.items():
        field = form.find("input", {"name": name})
        assert field is not None, f"no input {name}"
        assert field.get("value") == value, name
    for name, value in (("seniority", "senior"), ("employment_type", "contract")):
        select = form.find("select", {"name": name})
        assert select is not None
        options = select.find_all("option")
        chosen = [o.get("value") for o in options if "selected" in o.attrs]
        assert chosen == [value], name


def test_unset_seniority_and_employment_type_select_the_blank_option(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(
        monkeypatch,
        job_over={"seniority": None, "employment_type": None, "min_years": None},
    )
    form = _job_details_form(client)
    for name in ("seniority", "employment_type"):
        select = form.find("select", {"name": name})
        assert select is not None
        for option in select.find_all("option"):
            if "selected" in option.attrs:
                assert (option.get("value") or "") == "", name
    years = form.find("input", {"name": "min_years"})
    assert years is not None and (years.get("value") or "") == ""


def test_the_selects_hold_a_blank_option_and_exactly_the_schema_values(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    form = _job_details_form(client)
    for name, literal in (
        ("seniority", Seniority),
        ("employment_type", EmploymentType),
    ):
        values = _select_values(form, name)
        assert values.count("") == 1, f"{name}: want exactly one blank option"
        assert sorted(v for v in values if v) == sorted(get_args(literal)), name


def test_the_select_options_are_labelled_readably_not_with_raw_codes(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    form = _job_details_form(client)
    select = form.find("select", {"name": "employment_type"})
    assert select is not None
    for option in select.find_all("option"):
        label = option.text()
        if option.get("value"):
            assert label and "_" not in label, f"raw code shown as a label: {label!r}"


def test_minimum_years_is_a_number_input_bounded_0_to_50(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    years = _job_details_form(client).find("input", {"name": "min_years"})
    assert years is not None
    assert years.get("type") == "number"
    assert years.get("min") == "0"
    assert years.get("max") == "50"


def test_the_form_carries_the_ranking_hint(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    text = _job_details_form(client).text().lower()
    assert "regenerate the shortlist" in text


def test_the_form_keeps_the_campus_options_from_the_canonicaliser(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.campus import CAMPUS_CODES

    dom.install(monkeypatch)
    form = _job_details_form(client)
    campus = form.find("input", {"name": "location"})
    assert campus is not None and campus.get("list")
    listing = form.find("datalist", {"id": campus.get("list")})
    assert listing is not None
    assert {o.get("value") for o in listing.find_all("option")} == set(CAMPUS_CODES)


def test_exactly_one_form_on_the_page_posts_to_details(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old separate department-and-campus form is gone, not kept beside."""
    dom.install(monkeypatch)
    root = _root(client, "settings")
    forms = _details_forms(root)
    assert len(forms) == 1
    assert forms[0].get("id") == "job-details"
    assert forms[0].find("input", {"name": "title"}) is not None


@pytest.mark.parametrize("tab", ["overview", "candidates", "requirements", "shortlist"])
def test_the_form_lives_on_settings_only(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    dom.install(monkeypatch)
    assert _details_forms(_root(client, tab)) == []


# ── non-writers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tab", _TABS)
@_READER
def test_a_non_writer_sees_no_details_form_and_no_edit_link(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str, tab: str
) -> None:
    dom.install(monkeypatch, role=role)
    root = _root(client, tab)
    assert _details_forms(root) == []
    assert [f for f in root.find_all("form") if f.get("id") == "job-details"] == []
    assert root.find_all("input", {"name": "min_years"}) == []
    assert _edit_links(root) == []


# ── the header link ─────────────────────────────────────────────────────────


@_ALL_TABS
@_WRITER
def test_a_writer_gets_an_edit_details_link_on_every_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None, tab: str
) -> None:
    dom.install(monkeypatch, role=role)
    root = _root(client, tab)
    (link,) = _edit_links(root)
    where = urlsplit(link.get("href") or "")
    assert where.path == f"/jobs/{JOB_ID}"
    assert parse_qs(where.query) == {"tab": ["settings"]}
    assert where.fragment == "job-details"
    wrapper = dom.tab_root(root, tab)
    if wrapper is not None:
        assert not link.within(wrapper), "the link belongs to the header"


# ── POST /details, v2 on, success ───────────────────────────────────────────

_FULL = {
    "title": "Senior Analyst",
    "min_years": "5",
    "seniority": "senior",
    "employment_type": "full_time",
    "department": "School of Medicine",
    "location": "Burnaby",
}


def test_a_full_submission_patches_every_field_and_returns_to_settings(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, dict(_FULL))
    _on_settings(resp)
    resp.spy.assert_called_once()
    assert resp.spy.call_args.args[0] == JOB_ID
    assert resp.spy.call_args.args[1] == {
        "title": "Senior Analyst",
        "min_years": 5,
        "seniority": "senior",
        "employment_type": "full_time",
        "department": "School of Medicine",
        "location": "Burnaby",
    }


@pytest.mark.parametrize(
    "only",
    [
        {"title": "Lead Editor"},
        {"min_years": "4"},
        {"seniority": "mid"},
        {"employment_type": "intern"},
        {"department": "Library"},
        {"location": "Surrey"},
        {"title": "Lead Editor", "location": "Surrey"},
    ],
    ids=lambda d: "+".join(d),
)
def test_the_patch_carries_only_the_submitted_fields(
    client: Any, monkeypatch: pytest.MonkeyPatch, only: dict[str, str]
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, dict(only))
    _on_settings(resp)
    payload = resp.spy.call_args.args[1]
    assert set(payload) == set(only)


def test_min_years_is_sent_as_an_int_not_a_string(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, {"min_years": " 12 "})
    payload = resp.spy.call_args.args[1]
    assert payload == {"min_years": 12}
    assert type(payload["min_years"]) is int


@pytest.mark.parametrize("years", ["0", "50"])
def test_the_min_years_bounds_are_inclusive(
    client: Any, monkeypatch: pytest.MonkeyPatch, years: str
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, {"min_years": years})
    _on_settings(resp)
    assert resp.spy.call_args.args[1] == {"min_years": int(years)}


@pytest.mark.parametrize("field", ["min_years", "seniority", "employment_type"])
def test_a_blank_optional_field_is_sent_as_none_meaning_unset(
    client: Any, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, {field: ""})
    _on_settings(resp)
    assert resp.spy.call_args.args[1] == {field: None}


def test_all_three_blank_optionals_clear_together(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    data = {"min_years": "", "seniority": "", "employment_type": ""}
    resp = _post(client, monkeypatch, data)
    assert resp.spy.call_args.args[1] == {
        "min_years": None,
        "seniority": None,
        "employment_type": None,
    }


def test_a_two_character_title_is_accepted_and_trimmed(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, {"title": "  QA  "})
    _on_settings(resp)
    assert resp.spy.call_args.args[1] == {"title": "QA"}


def test_a_campus_typed_as_a_code_goes_to_the_backend_unnormalised(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, {"location": "  bby "})
    assert resp.spy.call_args.args[1] == {"location": "bby"}


def test_a_nothing_submitted_post_does_not_reach_the_backend(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, {})
    _on_settings(resp)
    resp.spy.assert_not_called()


def test_a_crafted_extra_field_cannot_ride_along(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    data = {**_FULL, "blind_review": "false", "description_raw": "x" * 80}
    resp = _post(client, monkeypatch, data)
    payload = resp.spy.call_args.args[1]
    assert "blind_review" not in payload
    assert "description_raw" not in payload


# ── POST /details, v2 on, validation failures ───────────────────────────────

_BAD: list[tuple[str, str]] = [
    ("title", ""),
    ("title", "   "),
    ("title", "A"),
    ("min_years", "abc"),
    ("min_years", "-1"),
    ("min_years", "51"),
    ("min_years", "3.5"),
    ("seniority", "boss"),
    ("employment_type", "gig"),
]


@pytest.mark.parametrize(("field", "value"), _BAD)
def test_an_invalid_value_re_renders_settings_with_400_and_no_patch(
    client: Any, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, {field: value})
    resp.spy.assert_not_called()
    text = resp.get_data(as_text=True)
    assert resp.status_code == 400, text[:300]
    root = dom.parse(text)
    assert dom.tab_root(root, "settings") is not None, "must re-render Settings"
    current = [a.text() for a in root.find_all("a", {"aria-current": "page"})]
    assert current == ["Settings"]
    errors = [e.text() for e in root.find_all("p", {"class": "error"})]
    assert any(errors), "no message shown"


@pytest.mark.parametrize(("field", "value"), _BAD)
def test_one_invalid_field_blocks_the_valid_ones_too(
    client: Any, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    """All or nothing: a half-applied edit would change ranking inputs silently."""
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, {**_FULL, field: value})
    assert resp.status_code == 400
    resp.spy.assert_not_called()


def test_a_failed_validation_still_shows_the_form_to_try_again(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    resp = _post(client, monkeypatch, {"title": ""})
    root = dom.parse(resp.get_data(as_text=True))
    assert len(_details_forms(root)) == 1


# ── the description: draft only, read-only otherwise ────────────────────────

_CLOSE_NOTE = "close this job and create a new one"


@pytest.mark.parametrize("state", ["open", "closed", "archived"])
@_WRITER
def test_a_non_draft_shows_the_description_read_only_with_the_recreate_note(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None, state: str
) -> None:
    raw = "Operate the editing suite and train the department staff."
    dom.install(monkeypatch, role=role, state=state, job_over={"description_raw": raw})
    root = _root(client, "requirements")
    tab = dom.tab_root(root, "requirements")
    assert tab is not None
    assert dom.forms_posting_to(tab, "/description") == []
    assert root.find_all("textarea", {"name": "description_raw"}) == []
    assert raw in tab.text(), "the description text is not shown read-only"
    assert _CLOSE_NOTE in tab.text().lower()


@pytest.mark.parametrize("state", ["parsing", "failed", "parsed"])
@_WRITER
def test_a_draft_keeps_the_editor_and_has_no_recreate_note(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None, state: str
) -> None:
    dom.install(monkeypatch, role=role, state=state)
    root = _root(client, "requirements")
    tab = dom.tab_root(root, "requirements")
    assert tab is not None
    (form,) = dom.forms_posting_to(tab, "/description")
    assert form.find("textarea", {"name": "description_raw"}) is not None
    assert _CLOSE_NOTE not in tab.text().lower()


# ── v1 parity (flag off) ────────────────────────────────────────────────────


def test_v1_post_of_department_and_location_is_unchanged(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, flag=False)
    data = {"department": "School of Medicine", "location": "Burnaby"}
    resp = _post(client, monkeypatch, data)
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert location.endswith(f"/jobs/{JOB_ID}")
    assert "?" not in location
    resp.spy.assert_called_once()
    assert resp.spy.call_args.args[1] == data


def test_v1_post_of_a_blank_department_still_sends_none(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, flag=False)
    resp = _post(client, monkeypatch, {"department": "", "location": ""})
    assert resp.spy.call_args.args[1] == {"department": None, "location": None}


def test_the_v1_page_has_neither_the_job_details_form_nor_the_header_link(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, flag=False)
    resp = client.get(f"/jobs/{JOB_ID}")
    root = dom.parse(resp.get_data(as_text=True))
    assert [f for f in root.find_all("form") if f.get("id") == "job-details"] == []
    assert _edit_links(root) == []
    assert root.find_all("input", {"name": "min_years"}) == []
