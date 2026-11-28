"""S2 (job-page redesign, Option A) -- the Candidates tab.

RED: ``_job_v2_tab_candidates.html`` does not exist; ``?tab=candidates`` still
renders the S1 placeholder ("coming in S2") and ``_render_job_detail`` still
does every read (and mints the export token) on every tab.

Spec: ``docs/plans/2026-11-job-page-redesign/S-contract.md`` section 3 "S2",
section 7 (polls), section 8 (empty states) and section 9 (role views). Open
questions Q2 (split and roster cards keep v1 parity on a non-open job), Q3 (the
table is the unchanged ``resumes_table.html``, no new columns) and Q5 (no count
on the tab label) are taken at their recommended defaults.

Fixtures come from ``tests/unit/job_page_v2_dom.py``: the job and the rows are
DTO-built via ``model_dump(mode="json")``; the export and split read models have
no DTO class, so those are dicts shaped like the existing tests' dicts.

**Markup hooks this file pins** (the contract fixes text and targets, not
attribute names, so the tester chose these and the coder must follow them):

* tab content wrapper: ONE element with ``data-job-tab="candidates"`` that holds
  everything below (the banner, the cards, the export menu, the status
  placeholder and the table);
* intake cards: an element per card with ``data-intake-card`` set to ``single``
  (card 1), ``zip`` (card 2), ``taleo-pdf`` (card 3) or ``roster`` (card 4). It
  holds the card's label, its "use this when" sentence and its ``<form>``;
* intake banner: at most one element with ``data-intake-banner`` set to
  ``parsing``, ``failed``, ``parsed``, ``closed`` or ``archived``; absent on an
  open job and for non-writers. Its text carries the section 8 wording;
* export menu: an element with ``data-export-menu`` holding the start form, the
  ``data-export-status="<status>"`` element (the existing v1 hook) and, for a
  ``ready`` export, the download form;
* pending splits: an element with ``data-pending-splits`` INSIDE card 3, present
  only when the listing is non-empty;
* read-only note: for a non-writer the tab text says ``Read-only for your role``;
* unchanged hooks reused as they are: ``id="resumes-table"`` (with its poll
  attributes) and ``id="resume-status-breakdown"`` (the lazy placeholder).

Every element ``id`` on the page is unique, and every ``<label for>`` names an id
that exists on the page.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from frontend import api_client
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import JOB_ID, Node

_CARDS = ("single", "zip", "taleo-pdf", "roster")
_WRITER_ROLES: tuple[str | None, ...] = ("admin", "recruiter", None)
_READER_ROLES: tuple[str, ...] = ("hiring_manager", "auditor")
_ALL_ROLES: tuple[str | None, ...] = (*_WRITER_ROLES, *_READER_ROLES)


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _body(client: Any, tab: str = "candidates", *, status: int = 200) -> str:
    resp = client.get(
        f"/jobs/{JOB_ID}?tab={tab}",
        headers={"Cookie": "ra_session=tok-live"},
    )
    text = resp.get_data(as_text=True)
    assert resp.status_code == status, text[:400]
    return text


def _tab(body: str) -> tuple[Node, Node]:
    root = dom.parse(body)
    tab = dom.tab_root(root)
    assert tab is not None, 'the page has no data-job-tab="candidates" element'
    return root, tab


def _banner(tab: Node) -> Node | None:
    return tab.find(attrs={"data-intake-banner": None})


def _card_keys(tab: Node) -> set[str]:
    nodes = tab.find_all(attrs={"data-intake-card": None})
    return {str(n.get("data-intake-card")) for n in nodes}


# ── the tab exists and is exclusive ─────────────────────────────────────────


def test_the_candidates_tab_renders_its_content_wrapper(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch)
    root, tab = _tab(_body(client))
    assert "coming in S2" not in root.text()
    current = [a.text() for a in root.find_all("a", {"aria-current": "page"})]
    assert current == ["Candidates"]


def test_no_other_tab_renders_the_candidates_wrapper(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch)
    for tab in ("overview", "requirements", "shortlist", "settings"):
        root = dom.parse(_body(client, tab))
        assert dom.tab_root(root) is None, tab
        assert not root.find_all(attrs={"data-intake-card": None}), tab


def test_the_tab_label_carries_no_count(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q5: a count in the label would cost a ``list_resumes`` on every tab."""
    dom.install(monkeypatch, resumes=[dom.make_resume(i) for i in range(1, 25)])
    root = dom.parse(_body(client, "overview"))
    anchors = [a.text() for a in root.find_all("a")]
    labels = [t for t in anchors if t.startswith("Candidates")]
    assert labels == ["Candidates"]


# ── the four cards for a writer on an open job ──────────────────────────────


@pytest.mark.parametrize("role", _WRITER_ROLES, ids=lambda r: r or "cas-off")
def test_a_writer_on_an_open_job_sees_all_four_cards(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
) -> None:
    dom.install(monkeypatch, role=role)
    _root, tab = _tab(_body(client))
    assert _card_keys(tab) == set(_CARDS)
    assert _banner(tab) is None


_USE_WHEN = {
    "single": ["use when a single candidate emailed you their file"],
    "zip": ["use when you have a folder of separate files"],
    "taleo-pdf": [
        "use for the one combined pdf hr downloads from taleo",
        "you review the split before anything is imported",
    ],
    "roster": [
        "use to enrich candidates already added",
        "adds no résumés",
        "upload after the résumés have finished parsing",
    ],
}


@pytest.mark.parametrize("key", _CARDS)
def test_each_card_says_when_to_use_it(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    dom.install(monkeypatch)
    _root, tab = _tab(_body(client))
    node = dom.card(tab, key)
    assert node is not None
    text = node.text().lower()
    for phrase in _USE_WHEN[key]:
        assert phrase in text, (key, phrase)


def test_the_roster_card_says_it_adds_no_resumes_and_to_upload_after_parsing(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HANDOFF section 4 lesson 1: a roster uploaded before the parse matches 0
    of 315 and says nothing about why. The ordering sentence is mandatory."""
    dom.install(monkeypatch)
    _root, tab = _tab(_body(client))
    node = dom.card(tab, "roster")
    assert node is not None
    text = node.text()
    assert "Adds no résumés" in text or "adds no résumés" in text
    assert "Upload after the résumés have finished parsing" in text


@pytest.mark.parametrize(
    ("key", "path", "field", "multiple"),
    [
        ("single", f"/jobs/{JOB_ID}/resumes", "files", False),
        ("zip", f"/jobs/{JOB_ID}/resumes", "files", True),
        ("taleo-pdf", f"/jobs/{JOB_ID}/resumes/split-upload", "files", True),
        ("roster", f"/jobs/{JOB_ID}/candidate-roster", "file", False),
    ],
)
def test_each_card_posts_to_its_route_with_a_file_field(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    path: str,
    field: str,
    multiple: bool,
) -> None:
    dom.install(monkeypatch)
    _root, tab = _tab(_body(client))
    node = dom.card(tab, key)
    assert node is not None
    (form,) = node.find_all("form")
    assert (form.get("method") or "").lower() == "post"
    assert (form.get("action") or "").endswith(path)
    assert "multipart/form-data" in (form.get("enctype") or "")
    inputs = form.find_all("input", {"type": "file"})
    files = [i for i in inputs if i.get("name") == field]
    assert len(files) == 1, f"{key} needs exactly one <input type=file name={field}>"
    assert ("multiple" in files[0].attrs) is multiple
    assert dom.hidden_inputs(form).get("csrf_token")


def test_card_two_takes_the_pairing_manifest_and_cover_letter_fields(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch)
    _root, tab = _tab(_body(client))
    node = dom.card(tab, "zip")
    assert node is not None
    names = {i.get("name") for i in node.find_all("input")}
    assert "pairing_manifest" in names
    one = dom.card(tab, "single")
    assert one is not None
    one_names = {i.get("name") for i in one.find_all("input")}
    assert "cover_letter_file" in one_names
    assert any(t.get("name") == "cover_letter_text" for t in one.find_all("textarea"))


@pytest.mark.parametrize("key", ["single", "zip", "taleo-pdf"])
def test_every_card_that_ingests_files_carries_a_required_consent_checkbox(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> None:
    dom.install(monkeypatch)
    _root, tab = _tab(_body(client))
    node = dom.card(tab, key)
    assert node is not None
    want = {"type": "checkbox", "name": "consent_acknowledged"}
    boxes = node.find_all("input", want)
    assert len(boxes) == 1
    assert "required" in boxes[0].attrs
    assert boxes[0].get("value") == "true"


def test_element_ids_are_unique_and_every_label_points_at_one(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cards 1 and 2 post to the same route; a copy-pasted
    ``id="consent_acknowledged"`` would duplicate and a label would toggle the
    wrong box."""
    dom.install(
        monkeypatch,
        splits=[dom.make_split(1)],
        export=dom.make_export("ready"),
    )
    root, _tab_node = _tab(_body(client))
    ids = [str(n.get("id")) for n in root.walk() if n.get("id") is not None]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate element ids: {dupes}"
    for label in root.find_all("label", {"for": None}):
        assert label.get("for") in ids, f"label for={label.get('for')!r} has no target"


# ── the section 8 matrix: role x job status ─────────────────────────────────

_STATE_BANNER: dict[str, str | None] = {
    "parsing": "parsing",
    "failed": "failed",
    "parsed": "parsed",
    "open": None,
    "closed": "closed",
    "archived": "archived",
}
_STATE_WRITER_CARDS: dict[str, set[str]] = {
    "parsing": {"taleo-pdf", "roster"},
    "failed": {"taleo-pdf", "roster"},
    "parsed": {"taleo-pdf", "roster"},
    "open": set(_CARDS),
    "closed": {"taleo-pdf", "roster"},
    "archived": {"taleo-pdf", "roster"},
}
_STATE_PHRASES: dict[str, list[str]] = {
    "parsing": [
        "add résumés yet",
        "once the job is open",
        "about 5 minutes",
    ],
    "failed": [
        "could not be parsed",
        "The model returned no usable JSON.",
        "Re-parse it on the Requirements tab, then open the job",
    ],
    "parsed": ["is parsed but not open", "Open it to accept résumés"],
    "closed": ["This job is closed and no longer accepts résumés"],
    "archived": ["This job is archived"],
}


@pytest.mark.parametrize("state", list(_STATE_BANNER))
@pytest.mark.parametrize("role", _WRITER_ROLES, ids=lambda r: r or "cas-off")
def test_writer_matrix_banner_and_cards_per_status(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
    state: str,
) -> None:
    dom.install(monkeypatch, role=role, state=state)
    _root, tab = _tab(_body(client))
    banner = _banner(tab)
    expected = _STATE_BANNER[state]
    if expected is None:
        assert banner is None
    else:
        assert banner is not None, f"{state}: the banner explaining intake is missing"
        assert banner.get("data-intake-banner") == expected
        text = banner.text()
        for phrase in _STATE_PHRASES[state]:
            assert phrase in text, (state, phrase, text)
    assert _card_keys(tab) == _STATE_WRITER_CARDS[state]
    # Q2: the export menu keeps v1 parity (shown to writers in every status).
    assert tab.find(attrs={"data-export-menu": None}) is not None


@pytest.mark.parametrize("state", list(_STATE_BANNER))
@pytest.mark.parametrize("role", _READER_ROLES)
def test_reader_matrix_is_the_table_only_with_no_banner(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    state: str,
) -> None:
    dom.install(monkeypatch, role=role, state=state)
    _root, tab = _tab(_body(client))
    assert _banner(tab) is None
    assert _card_keys(tab) == set()
    assert tab.find(attrs={"data-export-menu": None}) is None
    assert tab.find_all("form") == []
    assert "Read-only for your role" in tab.text()
    assert tab.find(attrs={"id": "resumes-table"}) is not None


@pytest.mark.parametrize("role", _WRITER_ROLES, ids=lambda r: r or "cas-off")
def test_a_writer_sees_no_read_only_note(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
) -> None:
    dom.install(monkeypatch, role=role)
    _root, tab = _tab(_body(client))
    assert "Read-only for your role" not in tab.text()


def test_the_parsing_banner_links_back_to_this_tab_to_refresh(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch, state="parsing")
    _root, tab = _tab(_body(client))
    banner = _banner(tab)
    assert banner is not None
    links = [a for a in banner.find_all("a") if a.text() == "Refresh status"]
    assert len(links) == 1
    assert "tab=candidates" in (links[0].get("href") or "")


def test_the_parsing_banner_says_why_intake_is_unavailable_and_how_long(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-11-24 incident: upload vanished with no explanation."""
    dom.install(monkeypatch, state="parsing")
    _root, tab = _tab(_body(client))
    banner = _banner(tab)
    assert banner is not None
    text = banner.text()
    assert "can" in text and "add résumés yet" in text
    assert "its description has been parsed" in text
    assert "about 5 minutes" in text
    assert "several postings parse at once" in text


def test_the_failed_banner_links_to_the_requirements_tab(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch, state="failed")
    _root, tab = _tab(_body(client))
    banner = _banner(tab)
    assert banner is not None
    hrefs = [a.get("href") or "" for a in banner.find_all("a")]
    assert any("tab=requirements" in h for h in hrefs), hrefs


def test_the_parsed_banner_carries_an_open_form_that_returns_to_this_tab(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch, state="parsed")
    _root, tab = _tab(_body(client))
    banner = _banner(tab)
    assert banner is not None
    (form,) = dom.forms_posting_to(banner, f"/jobs/{JOB_ID}/status")
    hidden = dom.hidden_inputs(form)
    assert hidden.get("to") == "open"
    assert hidden.get("tab") == "candidates"
    assert hidden.get("csrf_token")
    assert "Open for applicants" in form.text()


@pytest.mark.parametrize("state", ["parsing", "failed", "parsed", "closed", "archived"])
def test_cards_one_and_two_are_absent_unless_the_job_is_open(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    dom.install(monkeypatch, state=state)
    root, _tab_node = _tab(_body(client))
    assert not dom.forms_posting_to(root, f"/jobs/{JOB_ID}/resumes")


# ── the export menu ─────────────────────────────────────────────────────────


def _menu(client: Any) -> Node:
    _root, tab = _tab(_body(client))
    menu = tab.find(attrs={"data-export-menu": None})
    assert menu is not None, "the writer has no export menu"
    return menu


def test_the_export_menu_holds_the_start_form(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch)
    menu = _menu(client)
    (form,) = dom.forms_posting_to(menu, f"/jobs/{JOB_ID}/resumes/exports")
    assert dom.hidden_inputs(form).get("csrf_token")
    assert "export" in form.text().lower()
    assert menu.find(attrs={"data-export-status": None}) is None
    assert not dom.forms_posting_to(menu, "/download")


def test_a_ready_export_adds_a_download_form_with_its_own_one_shot_token(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch, export=dom.make_export("ready"))
    menu = _menu(client)
    status = menu.find(attrs={"data-export-status": "ready"})
    assert status is not None
    path = f"/resume-exports/{dom.EXPORT_ID}/download"
    (download,) = dom.forms_posting_to(menu, path)
    hidden = dom.hidden_inputs(download)
    assert hidden.get("csrf_token")
    assert hidden.get("job_id") == str(JOB_ID)
    (start,) = dom.forms_posting_to(menu, f"/jobs/{JOB_ID}/resumes/exports")
    # The download form carries the one-shot token, not the page token.
    assert hidden["csrf_token"] != dom.hidden_inputs(start)["csrf_token"]
    assert "3 applicants" in menu.text()
    assert not menu.find_all("a", {"href": path})


@pytest.mark.parametrize(
    ("status", "hint"),
    [
        ("queued", "refresh"),
        ("building", "refresh"),
        ("failed", "start it again"),
        ("expired", "start it again"),
    ],
)
def test_a_non_ready_export_shows_its_status_and_no_download_form(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    hint: str,
) -> None:
    dom.install(monkeypatch, export=dom.make_export(status))
    menu = _menu(client)
    node = menu.find(attrs={"data-export-status": status})
    assert node is not None
    assert hint in menu.text().lower()
    assert not dom.forms_posting_to(menu, "/download")


@pytest.mark.parametrize(
    "failure",
    [
        api_client.BackendUnavailable("down"),
        api_client.BadRequest("x", status_code=403, detail={"detail": "no"}),
        api_client.NotFound("gone"),
    ],
    ids=["503", "403", "404"],
)
def test_the_tab_survives_the_latest_export_lookup_failing(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    dom.install(monkeypatch, export=failure)
    menu = _menu(client)
    assert dom.forms_posting_to(menu, f"/jobs/{JOB_ID}/resumes/exports")
    assert menu.find(attrs={"data-export-status": None}) is None


# ── pending splits (inside card 3) ──────────────────────────────────────────


def test_pending_splits_list_review_links_status_and_a_day_formatted_date(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        dom.make_split(1, "proposed"),
        dom.make_split(2, "queued"),
        dom.make_split(3, "failed"),
    ]
    dom.install(monkeypatch, splits=items)
    _root, tab = _tab(_body(client))
    card3 = dom.card(tab, "taleo-pdf")
    assert card3 is not None
    pending = card3.find(attrs={"data-pending-splits": None})
    assert pending is not None, "pending splits must live inside card 3"
    hrefs = {a.get("href") or "" for a in pending.find_all("a")}
    for item in items:
        assert any(h.endswith(f"/resume-splits/{item['id']}") for h in hrefs)
        assert str(item["status"]) in pending.text().lower()
    assert "2026-11-20" in pending.text()
    assert "T12:00" not in pending.text(), "created_at must go through | day"
    assert "+00:00" not in pending.text()


def test_there_is_no_pending_section_when_there_are_no_splits(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch, splits=[])
    _root, tab = _tab(_body(client))
    assert dom.card(tab, "taleo-pdf") is not None
    assert tab.find(attrs={"data-pending-splits": None}) is None


@pytest.mark.parametrize(
    "failure",
    [
        api_client.BackendUnavailable("down"),
        api_client.BadRequest("x", status_code=403, detail={"detail": "no"}),
        api_client.NotFound("gone"),
    ],
    ids=["503", "403", "404"],
)
def test_the_tab_survives_a_failing_pending_listing(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    dom.install(monkeypatch, splits=failure)
    _root, tab = _tab(_body(client))
    assert dom.card(tab, "taleo-pdf") is not None
    assert tab.find(attrs={"data-pending-splits": None}) is None


def test_a_blind_job_shows_no_filename_in_the_pending_list(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = dom.make_split(1, "proposed", filename="Jane Roe export.pdf")
    dom.install(monkeypatch, splits=[item], job_over={"blind_review": True})
    body = _body(client)
    assert f"/resume-splits/{item['id']}" in body
    assert "Jane" not in body and "Roe" not in body


def test_a_normal_job_shows_the_filename_in_the_pending_list(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = dom.make_split(1, "proposed", filename="march-export.pdf")
    dom.install(monkeypatch, splits=[item])
    assert "march-export.pdf" in _body(client)


@pytest.mark.parametrize("role", _READER_ROLES)
def test_pending_splits_and_export_are_never_read_for_a_non_writer(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    spies = dom.install(
        monkeypatch,
        role=role,
        splits=[dom.make_split(1)],
        export=dom.make_export("ready"),
    )
    body = _body(client)
    assert "/resume-splits/" not in body
    assert "/resume-exports/" not in body
    spies.list_resume_splits.assert_not_called()
    spies.get_latest_resume_export.assert_not_called()


# ── the table, the lazy status placeholder and the polls ────────────────────


def test_the_resume_table_renders_rows_and_the_unchanged_empty_state(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch, resumes=[dom.make_resume(1), dom.make_resume(2)])
    _root, tab = _tab(_body(client))
    table = tab.find(attrs={"id": "resumes-table"})
    assert table is not None
    assert len(table.find_all("tbody")[0].find_all("tr")) == 2
    assert "applicant-001.pdf" in table.text()
    assert "Candidate Number002" in table.text()
    headers = [th.text() for th in table.find_all("th")]
    assert headers == ["Candidate", "File", "Status", "Uploaded"], "Q3: no new columns"


def test_an_empty_job_shows_the_table_empty_state(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch, resumes=[])
    _root, tab = _tab(_body(client))
    assert "No résumés uploaded yet." in tab.text()


def test_the_table_marks_withdrawn_and_degraded_candidates(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        dom.make_resume(1, withdrawn_at=dom.TS, withdrawal_reason="declined"),
        dom.make_resume(2, degraded=True),
        dom.make_resume(3),
    ]
    dom.install(monkeypatch, resumes=rows)
    _root, tab = _tab(_body(client))
    body_rows = tab.find_all("tbody")[0].find_all("tr")
    assert body_rows[0].find(attrs={"class": "pill pill-withdrawn"}) is not None
    assert body_rows[1].find(attrs={"class": "pill pill-degraded"}) is not None
    assert body_rows[2].find(attrs={"class": "pill pill-withdrawn"}) is None
    assert body_rows[2].find(attrs={"class": "pill pill-degraded"}) is None


def test_the_table_renders_uploaded_at_through_a_string_safe_path(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every timestamp is a string in the BFF; the render must not raise."""
    dom.install(monkeypatch, resumes=[dom.make_resume(1)])
    _root, tab = _tab(_body(client))
    assert "applicant-001.pdf" in tab.text()


@pytest.mark.parametrize("role", _ALL_ROLES, ids=lambda r: r or "cas-off")
def test_the_status_breakdown_placeholder_lazy_loads_for_every_role(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
) -> None:
    dom.install(monkeypatch, role=role)
    root, tab = _tab(_body(client))
    holders = root.find_all(attrs={"id": "resume-status-breakdown"})
    assert len(holders) == 1
    holder = holders[0]
    assert holder.within(tab)
    assert (holder.get("hx-get") or "").endswith(f"/jobs/{JOB_ID}/resume-status")
    assert holder.get("hx-trigger") == "load"
    assert holder.get("hx-swap") == "outerHTML"
    assert "Loading" in holder.text()


def test_the_table_polls_every_three_seconds_while_a_row_is_pending(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [dom.make_resume(1), dom.make_resume(2, status="parsing", parsed_at=None)]
    dom.install(monkeypatch, resumes=rows)
    _root, tab = _tab(_body(client))
    table = tab.find(attrs={"id": "resumes-table"})
    assert table is not None
    assert (table.get("hx-get") or "").endswith(f"/jobs/{JOB_ID}/resumes-table")
    assert table.get("hx-trigger") == "every 3s"
    assert table.get("hx-swap") == "outerHTML"


def test_the_table_stops_polling_once_every_row_is_terminal(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [dom.make_resume(1), dom.make_resume(2, status="failed", parsed_at=None)]
    dom.install(monkeypatch, resumes=rows)
    _root, tab = _tab(_body(client))
    table = tab.find(attrs={"id": "resumes-table"})
    assert table is not None
    assert "hx-trigger" not in table.attrs
    assert "hx-get" not in table.attrs


def test_the_page_has_exactly_one_resumes_table_and_one_poll_target(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [dom.make_resume(1, status="uploaded", parsed_at=None)]
    dom.install(monkeypatch, resumes=rows)
    root, _tab_node = _tab(_body(client))
    assert len(root.find_all(attrs={"id": "resumes-table"})) == 1
    polls = [n for n in root.walk() if n.get("hx-trigger") == "every 3s"]
    assert len(polls) == 1


# ── reads happen on the Candidates tab only (contract section 2.4) ──────────


@pytest.mark.parametrize("tab", ["overview", "requirements", "shortlist", "settings"])
def test_the_split_and_export_reads_are_not_made_off_the_candidates_tab(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    tab: str,
) -> None:
    spies = dom.install(
        monkeypatch,
        splits=[dom.make_split(1)],
        export=dom.make_export("ready"),
    )
    _body(client, tab)
    spies.list_resume_splits.assert_not_called()
    spies.get_latest_resume_export.assert_not_called()


def test_the_candidates_tab_reads_résumés_splits_and_the_latest_export_once(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spies = dom.install(
        monkeypatch,
        splits=[dom.make_split(1)],
        export=dom.make_export("ready"),
    )
    _body(client)
    spies.list_resumes.assert_called_once()
    spies.list_resume_splits.assert_called_once()
    spies.get_latest_resume_export.assert_called_once()
    assert spies.list_resume_splits.call_args.args[0] == JOB_ID
    assert spies.get_latest_resume_export.call_args.args[0] == JOB_ID


@pytest.mark.parametrize("role", _READER_ROLES)
def test_a_non_writer_candidates_render_reads_only_the_résumés(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    spies = dom.install(monkeypatch, role=role)
    _body(client)
    spies.list_resumes.assert_called_once()
    spies.list_resume_splits.assert_not_called()
    spies.get_latest_resume_export.assert_not_called()


def test_the_flag_off_page_is_still_v1_and_has_no_candidates_wrapper(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch, flag=False)
    body = _body(client)
    assert "data-job-layout" not in body
    assert "data-intake-card" not in body
    assert "data-job-tab" not in body


def test_an_unknown_job_is_still_a_404_on_the_candidates_tab(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dom.install(monkeypatch)

    def _missing(jid: UUID, **kw: Any) -> Any:
        raise api_client.NotFound("no such job")

    monkeypatch.setattr(api_client, "get_job", _missing)
    assert client.get(f"/jobs/{JOB_ID}?tab=candidates").status_code == 404
