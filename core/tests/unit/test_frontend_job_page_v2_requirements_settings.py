"""S3 (job-page redesign, Option A) -- the Requirements and Settings tabs.

RED: both tabs are still the S1 placeholders ("coming in S3"/"coming in S4"), so
none of the v1 controls exist on them, and ``_render_job_detail`` still reads
the résumé list on every tab.

Spec: ``docs/plans/2026-11-job-page-redesign/S-contract.md`` section 3 "S3",
section 2.4 (reads per tab), section 9 (role matrix). Open questions Q7 (keep
the blind-review toggle BUTTON), Q8 (no confirm dialog on close/archive) and Q9
(description and additional requirements stay TWO forms posting to TWO routes;
they may share one visual panel) are taken at their recommended defaults.

The v1 tests whose v2 twins live here (contract section 5.1):
``test_frontend_job_assignees``, ``test_frontend_job_details_edit``,
``test_frontend_job_reparse``, ``test_manager_requirements_editable``,
``test_frontend_zero_requirements_disclosure`` (the every-role warning) and
``test_frontend_job_detail`` (status buttons). Wording is v1's wherever v1's
sentence carries meaning.

**Markup hooks this file pins** (the contract fixes text and targets, not
attribute names, so the tester chose these and the coder must follow them):

* ``data-job-tab="requirements"`` and ``data-job-tab="settings"`` wrap each
  tab's content (as S2 did for ``candidates``). The header sits OUTSIDE them;
* the manager's note read-back is the v1 ``class="manager-note"`` element;
* the parse-status poll is the unchanged ``parse_status.html`` fragment
  (``id="parse-status"``, ``id="jd-no-requirements-warning"``), included as it
  is, so it keeps its own ``hx-get``/``hx-trigger`` while a parse is in flight;
* the department/campus ``<input list>`` points at a ``<datalist>`` of options.

Nothing else is pinned: forms are found by the route they post to, never by id.
Fixtures come from ``tests/unit/job_page_v2_dom.py`` (DTO-built, ISO-string
timestamps). The provisional-title note stays in the HEADER (contract 3 S3), so
it is asserted on every tab and outside every tab wrapper.
"""

from __future__ import annotations

from typing import Any

import pytest

from frontend import api_client
from src.campus import CAMPUS_CODES
from src.schemas.jobs import JDExtracted, Skill
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import JOB_ID, Node

_WRITER_ROLES: tuple[str | None, ...] = ("admin", "recruiter", None)
_READER_ROLES: tuple[str, ...] = ("hiring_manager", "auditor")
_ALL_ROLES: tuple[str | None, ...] = (*_WRITER_ROLES, *_READER_ROLES)
_WRITER = pytest.mark.parametrize("role", _WRITER_ROLES, ids=lambda r: r or "cas-off")
_READER = pytest.mark.parametrize("role", _READER_ROLES)
_ANY = pytest.mark.parametrize("role", _ALL_ROLES, ids=lambda r: r or "cas-off")

_NO_SKILLS = JDExtracted(title="x")
_NICE_ONLY = JDExtracted(title="x", nice_to_have_skills=[Skill(name="Kafka")])
_ZERO = {"description_parsed": _NO_SKILLS}
_STATUS_LABELS = {
    "open": "Open for applicants",
    "closed": "Close",
    "archived": "Archive",
}


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _body(client: Any, tab: str) -> str:
    resp = client.get(
        f"/jobs/{JOB_ID}?tab={tab}",
        headers={"Cookie": "ra_session=tok-live"},
    )
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200, text[:400]
    return text


def _page(client: Any, tab: str) -> tuple[Node, Node]:
    root = dom.parse(_body(client, tab))
    node = dom.tab_root(root, tab)
    assert node is not None, f'the page has no data-job-tab="{tab}" element'
    return root, node


def _forms(tab: Node, suffix: str) -> list[Node]:
    return dom.forms_posting_to(tab, f"/jobs/{JOB_ID}{suffix}")


def _current(root: Node) -> list[str]:
    return [a.text() for a in root.find_all("a", {"aria-current": "page"})]


# ── the tabs exist, are exclusive, and say nothing about S3/S4 ──────────────


@_ANY
def test_the_requirements_tab_renders_its_wrapper(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role)
    root, _tab = _page(client, "requirements")
    assert "coming in S3" not in root.text()
    assert _current(root) == ["Requirements"]


@_WRITER
def test_the_settings_tab_renders_its_wrapper_for_a_writer(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role)
    root, _tab = _page(client, "settings")
    assert "coming in S4" not in root.text()
    assert _current(root) == ["Settings"]


@pytest.mark.parametrize(
    "tab", ["overview", "candidates", "requirements", "shortlist", "settings"]
)
def test_each_wrapper_renders_on_its_own_tab_and_no_other(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    dom.install(monkeypatch)
    root = dom.parse(_body(client, tab))
    for name in ("requirements", "settings"):
        found = dom.tab_root(root, name)
        if name == tab:
            assert found is not None
        else:
            assert found is None, f"the {name} wrapper rendered on {tab}"


# ── Requirements: the controls, writer only ─────────────────────────────────


@_WRITER
def test_a_writer_sees_the_additional_requirements_form(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role)
    _root, tab = _page(client, "requirements")
    (form,) = _forms(tab, "/requirements")
    area = form.find("textarea", {"name": "additional_requirements"})
    assert area is not None
    assert "Save requirements" in [b.text() for b in form.find_all("button")]


@_READER
def test_a_non_writer_sees_no_form_on_the_requirements_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(
        monkeypatch,
        role=role,
        state="failed",
        job_over={"additional_requirements": "Must have MEG analysis."},
    )
    _root, tab = _page(client, "requirements")
    assert tab.find_all("form") == []
    assert tab.find_all("textarea") == []
    assert tab.find_all("button") == []


def test_the_forms_are_two_forms_on_two_routes_not_one_merged_save(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Q9: the requirements save costs an LLM call and keeps the ranking, the
    description save clears the parse. Merging them would change behaviour."""
    dom.install(monkeypatch, state="parsed")
    _root, tab = _page(client, "requirements")
    (req,) = _forms(tab, "/requirements")
    (desc,) = _forms(tab, "/description")
    assert req is not desc
    assert req.find("textarea", {"name": "description_raw"}) is None
    assert desc.find("textarea", {"name": "additional_requirements"}) is None


def test_the_additional_requirements_form_says_it_does_not_rerank(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    _root, tab = _page(client, "requirements")
    (form,) = _forms(tab, "/requirements")
    text = form.text()
    assert "does not re-rank anyone" in text
    assert "Regenerate" in text


def test_the_additional_requirements_form_is_empty_without_a_note(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    _root, tab = _page(client, "requirements")
    (form,) = _forms(tab, "/requirements")
    area = form.find("textarea", {"name": "additional_requirements"})
    assert area is not None and area.text() == ""
    assert tab.find(attrs={"class": "manager-note"}) is None


# ── Requirements: the manager's note read-back ──────────────────────────────

_NOTE = "Must have MEG analysis experience."
_NOTE_PARSED = {
    "must_have_skills": [{"name": "MEG analysis"}],
    "nice_to_have_skills": [{"name": "Kafka"}],
    "other_requirements": ["Willing to travel to Burnaby weekly"],
}


@_ANY
def test_the_managers_note_is_read_back_to_every_role(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, job_over={"additional_requirements": _NOTE})
    _root, tab = _page(client, "requirements")
    note = tab.find(attrs={"class": "manager-note"})
    assert note is not None, "the note is not read back"
    assert _NOTE in note.text()
    assert "Your additional requirements" in note.text()


@_WRITER
def test_the_existing_note_is_prefilled_for_editing(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, job_over={"additional_requirements": _NOTE})
    _root, tab = _page(client, "requirements")
    (form,) = _forms(tab, "/requirements")
    area = form.find("textarea", {"name": "additional_requirements"})
    assert area is not None
    assert area.text() == _NOTE


def test_the_note_shows_how_it_was_scored(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(
        monkeypatch,
        role="hiring_manager",
        job_over={
            "additional_requirements": _NOTE,
            "additional_requirements_parsed": _NOTE_PARSED,
        },
    )
    _root, tab = _page(client, "requirements")
    note = tab.find(attrs={"class": "manager-note"})
    assert note is not None
    text = note.text()
    assert "Scored as:" in text
    assert "MEG analysis · required" in text
    assert "Kafka" in text
    assert "Not scored as skills" in text
    assert "Willing to travel to Burnaby weekly" in text


# ── Requirements: the description editor, draft only ────────────────────────


@pytest.mark.parametrize(
    ("state", "offered"),
    [
        ("parsing", True),
        ("failed", True),
        ("parsed", True),
        ("open", False),
        ("closed", False),
        ("archived", False),
    ],
)
@_WRITER
def test_the_description_editor_is_offered_only_on_a_draft(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
    state: str,
    offered: bool,
) -> None:
    dom.install(monkeypatch, role=role, state=state)
    root, tab = _page(client, "requirements")
    forms = _forms(tab, "/description")
    assert len(forms) == (1 if offered else 0)
    areas = root.find_all("textarea", {"name": "description_raw"})
    assert len(areas) == (1 if offered else 0)


def test_the_description_editor_is_prefilled_and_says_it_reparses(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = "Operate the editing suite and train staff."
    dom.install(monkeypatch, state="parsed", job_over={"description_raw": raw})
    _root, tab = _page(client, "requirements")
    (form,) = _forms(tab, "/description")
    area = form.find("textarea", {"name": "description_raw"})
    assert area is not None and area.text() == raw
    assert "Save & re-parse" in [b.text() for b in form.find_all("button")]
    assert "a fresh parse is queued" in form.text()


@_READER
def test_a_non_writer_never_sees_the_description_editor(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role, state="parsed")
    root, _tab = _page(client, "requirements")
    assert root.find_all("textarea", {"name": "description_raw"}) == []


# ── Requirements: re-parse, only when there is something to retry ───────────


@pytest.mark.parametrize(
    ("state", "over", "offered"),
    [
        ("parsing", {}, False),
        ("failed", {}, True),
        ("parsed", {}, False),
        ("parsed", _ZERO, True),
        ("parsed", {"description_parsed": _NICE_ONLY}, False),
        ("open", _ZERO, False),
        ("open", {}, False),
        ("closed", _ZERO, False),
    ],
    ids=[
        "parsing",
        "failed",
        "parsed-with-skills",
        "zero-requirements-draft",
        "nice-to-have-only-draft",
        "zero-requirements-open",
        "open",
        "zero-requirements-closed",
    ],
)
@_WRITER
def test_reparse_is_offered_only_after_a_failure_or_an_empty_draft_parse(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
    state: str,
    over: dict[str, Any],
    offered: bool,
) -> None:
    dom.install(monkeypatch, role=role, state=state, job_over=over)
    _root, tab = _page(client, "requirements")
    forms = _forms(tab, "/reparse")
    assert len(forms) == (1 if offered else 0)
    if offered:
        assert "Re-parse JD" in [b.text() for b in forms[0].find_all("button")]


@_READER
def test_a_non_writer_never_sees_reparse_even_after_a_failure(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role, state="failed")
    root, _tab = _page(client, "requirements")
    assert dom.forms_posting_to(root, f"/jobs/{JOB_ID}/reparse") == []


def test_the_reparse_form_sits_outside_the_polled_fragment(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fragment is swapped wholesale every 3s; a form inside it would be
    torn out from under a user mid-click (v1 comment, job_detail.html)."""
    dom.install(monkeypatch, state="failed")
    _root, tab = _page(client, "requirements")
    (form,) = _forms(tab, "/reparse")
    fragment = tab.find(attrs={"id": "parse-status"})
    assert fragment is not None
    assert not form.within(fragment)


@_ANY
def test_a_failed_parse_shows_its_reason_to_every_role(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, state="failed")
    _root, tab = _page(client, "requirements")
    errors = tab.find_all("p", {"class": "error"})
    reason = "Parse failed: The model returned no usable JSON."
    assert any(reason in e.text() for e in errors)


# ── Requirements: the parse-status poll and the parsed skills ───────────────


@_ANY
def test_an_in_flight_parse_polls_the_parse_status_route(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, state="parsing")
    _root, tab = _page(client, "requirements")
    poll = tab.find(attrs={"id": "parse-status"})
    assert poll is not None
    assert (poll.get("hx-get") or "").endswith(f"/jobs/{JOB_ID}/parse-status")
    assert poll.get("hx-trigger") == "every 3s"
    assert poll.get("hx-swap") == "outerHTML"
    assert "parsing" in poll.text()


@pytest.mark.parametrize("state", ["failed", "parsed", "open", "closed"])
def test_a_finished_parse_does_not_poll(
    client: Any, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    dom.install(monkeypatch, state=state)
    root, tab = _page(client, "requirements")
    poll = tab.find(attrs={"id": "parse-status"})
    assert poll is not None
    assert poll.get("hx-get") is None
    assert poll.get("hx-trigger") is None
    assert not root.find_all(attrs={"hx-trigger": "every 3s"})


def test_the_parsed_skills_render_as_pills(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    _root, tab = _page(client, "requirements")
    pills = tab.find_all("span", {"class": "pill pill-skill"})
    assert [p.text() for p in pills] == ["Video editing"]


# ── Requirements: the zero-requirements warning, for every role ─────────────


@pytest.mark.parametrize("state", ["parsed", "open"])
@_ANY
def test_the_zero_requirements_warning_renders_for_every_role(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None, state: str
) -> None:
    dom.install(monkeypatch, role=role, state=state, job_over=_ZERO)
    _root, tab = _page(client, "requirements")
    warning = tab.find(attrs={"id": "jd-no-requirements-warning"})
    assert warning is not None, "no zero-requirements warning"
    assert "yielded no requirements at all" in warning.text()


@pytest.mark.parametrize(
    ("state", "over"),
    [
        ("open", {}),
        ("parsed", {"description_parsed": _NICE_ONLY}),
        ("parsing", {}),
        ("failed", {}),
    ],
)
def test_no_warning_when_the_jd_has_requirements_or_is_unparsed(
    client: Any, monkeypatch: pytest.MonkeyPatch, state: str, over: dict[str, Any]
) -> None:
    dom.install(monkeypatch, state=state, job_over=over)
    root, _tab = _page(client, "requirements")
    assert root.find(attrs={"id": "jd-no-requirements-warning"}) is None


# ── Requirements and Settings do not borrow each other's controls ───────────


def test_the_settings_controls_are_not_on_the_requirements_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    _root, tab = _page(client, "requirements")
    for suffix in ("/details", "/assignees", "/status", "/blind-review"):
        assert _forms(tab, suffix) == [], suffix


def test_the_requirements_controls_are_not_on_the_settings_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, state="failed")
    _root, tab = _page(client, "settings")
    for suffix in ("/requirements", "/reparse", "/description"):
        assert _forms(tab, suffix) == [], suffix


# ── Settings: department and campus ─────────────────────────────────────────


def test_the_department_and_campus_form_is_prefilled(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    _root, tab = _page(client, "settings")
    (form,) = _forms(tab, "/details")
    dept = form.find("input", {"name": "department"})
    campus = form.find("input", {"name": "location"})
    assert dept is not None and dept.get("value") == "School of Medicine"
    assert campus is not None and campus.get("value") == "Burnaby"
    assert "Save" in [b.text() for b in form.find_all("button")]
    assert "Whatever you set here wins" in form.text()


def test_the_campus_options_come_from_the_canonicaliser(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    _root, tab = _page(client, "settings")
    (form,) = _forms(tab, "/details")
    campus = form.find("input", {"name": "location"})
    assert campus is not None and campus.get("list")
    listing = form.find("datalist", {"id": campus.get("list")})
    assert listing is not None, "the campus input points at no <datalist>"
    options = {o.get("value") for o in listing.find_all("option")}
    assert options == set(CAMPUS_CODES)


def test_an_empty_department_and_campus_render_as_empty_values(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, job_over={"department": None, "location": None})
    _root, tab = _page(client, "settings")
    (form,) = _forms(tab, "/details")
    for name in ("department", "location"):
        field = form.find("input", {"name": name})
        assert field is not None and (field.get("value") or "") == ""


# ── the provisional-title note stays in the header, on every tab ────────────

_NOTE_TEXT = "This title came from the uploaded filename"


@pytest.mark.parametrize(
    "tab", ["overview", "candidates", "requirements", "shortlist", "settings"]
)
def test_a_provisional_title_is_explained_in_the_header_on_every_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    dom.install(
        monkeypatch,
        job_over={
            "title": "20260612 00138559 APSA JDFN 20260612",
            "title_provisional": True,
        },
    )
    root = dom.parse(_body(client, tab))
    assert root.text().count(_NOTE_TEXT) == 1
    wrapper = dom.tab_root(root, tab)
    if wrapper is not None:
        assert _NOTE_TEXT not in wrapper.text(), "the note belongs to the header"


@pytest.mark.parametrize("role", _READER_ROLES)
def test_a_non_writer_sees_the_provisional_title_note_too(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role, job_over={"title_provisional": True})
    for tab in ("overview", "requirements"):
        assert _NOTE_TEXT in dom.parse(_body(client, tab)).text(), tab


@pytest.mark.parametrize("tab", ["overview", "requirements", "settings"])
def test_a_chosen_title_gets_no_such_note(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    dom.install(monkeypatch, job_over={"title_provisional": False})
    assert _NOTE_TEXT not in dom.parse(_body(client, tab)).text()


# ── Settings: assigned hiring managers ──────────────────────────────────────


def _dana() -> dict[str, Any]:
    return dom.make_user(1, cas_username="dana", display_name="Dana Lee")


def _priya() -> dict[str, Any]:
    return dom.make_user(2, cas_username="priya", display_name="Priya Singh")


def test_the_assignees_render_with_a_remove_form_each_and_an_add_select(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    dana, priya = _dana(), _priya()
    dom.stub_settings_reads(monkeypatch, assignees=[dana], users=[dana, priya])
    _root, tab = _page(client, "settings")
    assert "Assigned hiring managers" in tab.text()
    assert "Dana Lee" in tab.text()
    (remove,) = _forms(tab, f"/assignees/{dana['id']}/remove")
    assert "Remove" in [b.text() for b in remove.find_all("button")]
    (add,) = _forms(tab, "/assignees")
    select = add.find("select", {"name": "user_id"})
    assert select is not None
    values = [o.get("value") for o in select.find_all("option")]
    assert values == [dana["id"], priya["id"]]
    assert "priya" in select.text()
    assert "Add" in [b.text() for b in add.find_all("button")]


def test_every_assignee_gets_its_own_remove_form(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    dana, priya = _dana(), _priya()
    dom.stub_settings_reads(monkeypatch, assignees=[dana, priya], users=[dana, priya])
    _root, tab = _page(client, "settings")
    for user in (dana, priya):
        assert len(_forms(tab, f"/assignees/{user['id']}/remove")) == 1


def test_no_assignee_says_the_job_is_invisible_to_hiring_managers(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    dom.stub_settings_reads(monkeypatch, assignees=[], users=[_dana()])
    _root, tab = _page(client, "settings")
    text = tab.text()
    assert "No hiring manager is assigned" in text
    assert "this requisition is invisible to hiring managers" in text
    assert "could not be loaded" not in text
    assert len(_forms(tab, "/assignees")) == 1


def test_a_failed_assignee_read_says_could_not_load_not_the_empty_claim(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed read must not be worded as a positive claim about the roster."""
    dom.install(monkeypatch)
    dom.stub_settings_reads(
        monkeypatch,
        assignees=api_client.BackendUnavailable("down"),
        users=[_dana()],
    )
    _root, tab = _page(client, "settings")
    text = tab.text()
    assert "Assignments could not be loaded" in text
    assert "invisible to hiring managers" not in text
    assert "No hiring manager is assigned" not in text


def test_a_failed_user_list_still_renders_the_add_form_with_no_options(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    dom.stub_settings_reads(
        monkeypatch,
        assignees=[_dana()],
        users=api_client.BackendUnavailable("down"),
    )
    _root, tab = _page(client, "settings")
    (add,) = _forms(tab, "/assignees")
    select = add.find("select", {"name": "user_id"})
    assert select is not None
    assert select.find_all("option") == []


# ── Settings: status ────────────────────────────────────────────────────────


def _status_forms(tab: Node) -> dict[str, Node]:
    out: dict[str, Node] = {}
    for form in _forms(tab, "/status"):
        out[dom.hidden_inputs(form).get("to", "")] = form
    return out


@pytest.mark.parametrize(
    ("state", "legal"),
    [
        ("parsing", {"open", "archived"}),
        ("failed", {"open", "archived"}),
        ("parsed", {"open", "archived"}),
        ("open", {"closed", "archived"}),
        ("closed", {"archived"}),
        ("archived", set()),
    ],
)
@_WRITER
def test_only_the_legal_next_states_are_offered(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
    state: str,
    legal: set[str],
) -> None:
    dom.install(monkeypatch, role=role, state=state)
    _root, tab = _page(client, "settings")
    forms = _status_forms(tab)
    assert set(forms) == legal
    assert len(_forms(tab, "/status")) == len(legal)
    for to, form in forms.items():
        hidden = dom.hidden_inputs(form)
        assert hidden["tab"] == "settings", "the Settings forms must stay on Settings"
        assert hidden["csrf_token"]
        assert [b.text() for b in form.find_all("button")] == [_STATUS_LABELS[to]]


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("parsing", "Wait for the LLM to finish parsing the JD."),
        ("failed", "Parsing failed"),
    ],
)
def test_open_is_disabled_before_the_parse_finishes_and_says_why(
    client: Any, monkeypatch: pytest.MonkeyPatch, state: str, reason: str
) -> None:
    dom.install(monkeypatch, state=state)
    _root, tab = _page(client, "settings")
    forms = _status_forms(tab)
    opener = forms["open"].find("button")
    assert opener is not None
    assert "disabled" in opener.attrs, "Open must be disabled before the parse"
    assert reason in (opener.get("title") or "")
    archiver = forms["archived"].find("button")
    assert archiver is not None and "disabled" not in archiver.attrs


def test_open_is_enabled_once_the_jd_is_parsed(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, state="parsed")
    _root, tab = _page(client, "settings")
    opener = _status_forms(tab)["open"].find("button")
    assert opener is not None
    assert "disabled" not in opener.attrs
    assert "cannot be opened" not in tab.text()


@pytest.mark.parametrize(
    ("state", "sentence"),
    [
        ("parsing", "Wait for the LLM to finish parsing the JD before opening"),
        ("failed", "Parsing failed, so this job cannot be opened for applicants"),
    ],
)
def test_a_blocked_open_is_explained_in_a_sentence(
    client: Any, monkeypatch: pytest.MonkeyPatch, state: str, sentence: str
) -> None:
    dom.install(monkeypatch, state=state)
    _root, tab = _page(client, "settings")
    assert sentence in tab.text()


# ── Settings: blind review (Q7, the toggle button stays) ────────────────────


@pytest.mark.parametrize(
    ("blind", "button", "posts"),
    [
        (False, "Turn on blind review", "true"),
        (True, "Turn off blind review", "false"),
    ],
)
def test_the_blind_review_button_flips_with_the_jobs_state(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    blind: bool,
    button: str,
    posts: str,
) -> None:
    dom.install(monkeypatch, job_over={"blind_review": blind})
    _root, tab = _page(client, "settings")
    (form,) = _forms(tab, "/blind-review")
    assert [b.text() for b in form.find_all("button")] == [button]
    hidden = dom.hidden_inputs(form)
    assert hidden["blind_review"] == posts
    assert hidden["csrf_token"]
    assert form.find_all("input", {"type": "checkbox"}) == []


# ── Settings: writer only, and a non-writer falls back to Overview ──────────

_SETTINGS_SUFFIXES = ("/details", "/assignees", "/blind-review")


@_READER
def test_settings_falls_back_to_overview_for_a_non_writer(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role)
    body = _body(client, "settings")
    root = dom.parse(body)
    assert dom.tab_root(root, "settings") is None
    assert _current(root) == ["Overview"]
    assert "Settings" not in [a.text() for a in root.find_all("a")]
    for suffix in (*_SETTINGS_SUFFIXES, "/status"):
        assert dom.forms_posting_to(root, f"/jobs/{JOB_ID}{suffix}") == [], suffix


@_READER
def test_a_non_writer_has_no_settings_control_on_any_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role)
    for tab in ("overview", "candidates", "requirements", "shortlist"):
        root = dom.parse(_body(client, tab))
        for suffix in (*_SETTINGS_SUFFIXES, "/status"):
            found = dom.forms_posting_to(root, f"/jobs/{JOB_ID}{suffix}")
            assert found == [], f"{suffix} on {tab}"


# ── reads: per tab, writer only ─────────────────────────────────────────────


def _assert_no_reads_beyond_get_job(
    spies: dom.Spies, reads: dom.ReadSpies, *, except_: tuple[str, ...] = ()
) -> None:
    named = {
        "list_resumes": spies.list_resumes,
        "list_resume_splits": spies.list_resume_splits,
        "get_latest_resume_export": spies.get_latest_resume_export,
        "list_job_assignees": reads.list_job_assignees,
        "list_users": reads.list_users,
        "list_shortlist": reads.list_shortlist,
    }
    for name, spy in named.items():
        if name in except_:
            continue
        assert spy.call_count == 0, f"{name} was called"


@_ANY
def test_the_requirements_tab_reads_nothing_beyond_get_job(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    spies = dom.install(monkeypatch, role=role, export=dom.make_export("ready"))
    reads = dom.stub_settings_reads(monkeypatch, assignees=[_dana()], users=[_dana()])
    _body(client, "requirements")
    _assert_no_reads_beyond_get_job(spies, reads)


@_WRITER
def test_the_settings_tab_reads_the_assignees_and_the_hiring_managers_only(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    spies = dom.install(monkeypatch, role=role, export=dom.make_export("ready"))
    reads = dom.stub_settings_reads(monkeypatch, assignees=[_dana()], users=[_dana()])
    _body(client, "settings")
    assert reads.list_job_assignees.call_count == 1
    assert reads.list_job_assignees.call_args.args[0] == JOB_ID
    assert reads.list_users.call_count == 1
    assert reads.list_users.call_args.kwargs == {"role": "hiring_manager"}
    _assert_no_reads_beyond_get_job(
        spies, reads, except_=("list_job_assignees", "list_users")
    )


@pytest.mark.parametrize("tab", ["overview", "candidates", "requirements", "shortlist"])
@_WRITER
def test_a_writer_does_not_read_the_settings_data_on_other_tabs(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None, tab: str
) -> None:
    dom.install(monkeypatch, role=role)
    reads = dom.stub_settings_reads(monkeypatch, assignees=[_dana()], users=[_dana()])
    _body(client, tab)
    assert reads.list_job_assignees.call_count == 0
    assert reads.list_users.call_count == 0


@pytest.mark.parametrize(
    "tab", ["overview", "candidates", "requirements", "shortlist", "settings"]
)
@_READER
def test_a_non_writer_never_triggers_the_settings_reads(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str, tab: str
) -> None:
    dom.install(monkeypatch, role=role)
    reads = dom.stub_settings_reads(monkeypatch, assignees=[_dana()], users=[_dana()])
    _body(client, tab)
    assert reads.list_job_assignees.call_count == 0
    assert reads.list_users.call_count == 0
