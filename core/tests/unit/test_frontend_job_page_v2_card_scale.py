"""S2 (job-page redesign, Option A) -- per-card controls at 32 and 50 cards.

HANDOFF section 4 lesson 8: a per-card one-shot CSRF token has a budget
(``csrf.MAX_TOKENS_PER_SESSION = 64``, FIFO-evicted) and a list can exceed it, so
any test of a per-card control renders 32 and 50 cards. The Candidates tab has
no per-row control by design: its ONE one-shot slot is the export download.

RED: the Candidates tab renders no table and no download form yet (S1
placeholder), so the rows and the single download form are absent.

Fixtures are DTO-built (``ResumeListItem`` via ``model_dump(mode="json")``); the
export read model has no DTO class, so it is a dict (see ``job_page_v2_dom``).
"""

from __future__ import annotations

from typing import Any

import pytest

from frontend import csrf
from frontend.app import app
from tests.e2e.driver import page_token
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import EXPORT_ID, JOB_ID, Node

_SIZES = (32, 50)


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _slots(client: Any) -> dict[str, str]:
    with client.session_transaction() as sess:
        stored = sess.get(csrf.SESSION_KEY)
    return dict(stored) if isinstance(stored, dict) else {}


def _render(client: Any) -> tuple[str, Node]:
    resp = client.get(f"/jobs/{JOB_ID}?tab=candidates")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200, body[:400]
    tab = dom.tab_root(dom.parse(body))
    assert tab is not None, 'no data-job-tab="candidates" element'
    return body, tab


@pytest.mark.parametrize("size", _SIZES)
def test_the_session_holds_exactly_one_one_shot_slot_at_scale(
    client: Any, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    rows = [dom.make_resume(i) for i in range(1, size + 1)]
    dom.install(monkeypatch, resumes=rows, export=dom.make_export("ready"))

    _body, tab = _render(client)

    table = tab.find(attrs={"id": "resumes-table"})
    assert table is not None
    assert len(table.find_all("tbody")[0].find_all("tr")) == size
    assert len(_slots(client)) == 1
    assert len(_slots(client)) < csrf.MAX_TOKENS_PER_SESSION


@pytest.mark.parametrize("size", _SIZES)
def test_a_reload_at_scale_reuses_the_live_slot_and_adds_none(
    client: Any, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    rows = [dom.make_resume(i) for i in range(1, size + 1)]
    dom.install(monkeypatch, resumes=rows, export=dom.make_export("ready"))

    _render(client)
    before = _slots(client)
    _render(client)
    _render(client)

    assert len(before) == 1
    assert _slots(client) == before


@pytest.mark.parametrize("size", _SIZES)
def test_there_is_exactly_one_download_form_and_no_per_row_form(
    client: Any, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    rows = [dom.make_resume(i) for i in range(1, size + 1)]
    dom.install(monkeypatch, resumes=rows, export=dom.make_export("ready"))

    body, tab = _render(client)

    downloads = dom.forms_posting_to(tab, f"/resume-exports/{EXPORT_ID}/download")
    assert len(downloads) == 1
    table = tab.find(attrs={"id": "resumes-table"})
    assert table is not None
    assert table.find_all("form") == [], "the table must carry no per-row form"
    # Every csrf_token on the tab is the page token except the one download token.
    page = page_token(body)
    assert page
    tokens = [dom.hidden_inputs(f).get("csrf_token") for f in tab.find_all("form")]
    assert all(tokens), "a form on the tab has an empty csrf_token"
    expected = [dom.hidden_inputs(downloads[0])["csrf_token"]]
    assert [t for t in tokens if t != page] == expected


@pytest.mark.parametrize("size", _SIZES)
def test_a_previously_minted_slot_survives_a_render_at_scale(
    client: Any, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    """A card-heavy render must not evict an older slot belonging to the user.

    Seed one slot for an unrelated action, then render at scale: both survive.
    """
    rows = [dom.make_resume(i) for i in range(1, size + 1)]
    dom.install(monkeypatch, resumes=rows, export=dom.make_export("ready"))
    with client.session_transaction() as sess:
        sess[csrf.SESSION_KEY] = {"seeded-slot": "seeded-token"}

    _render(client)

    slots = _slots(client)
    assert slots.get("seeded-slot") == "seeded-token"
    assert len(slots) == 2


@pytest.mark.parametrize("size", _SIZES)
def test_no_other_tab_adds_a_slot_at_scale(
    client: Any, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    rows = [dom.make_resume(i) for i in range(1, size + 1)]
    dom.install(monkeypatch, resumes=rows, export=dom.make_export("ready"))

    for tab in ("overview", "requirements", "shortlist", "settings"):
        resp = client.get(f"/jobs/{JOB_ID}?tab={tab}")
        assert resp.status_code == 200
        assert _slots(client) == {}, tab
