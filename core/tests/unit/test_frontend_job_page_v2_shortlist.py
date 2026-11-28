"""S4 (job-page redesign, Option A) -- the Shortlist tab is a teaser.

RED: the Shortlist tab is still the S1 placeholder, so it has no
``data-job-tab="shortlist"`` wrapper, no rows, no link and no disclosures, and
``_render_job_detail`` still reads the résumé list on that tab.

Spec: ``docs/plans/2026-11-job-page-redesign/S-contract.md`` section 3 "S4",
2.4 (the Shortlist tab reads ``list_shortlist`` and mints NO one-shot token),
4.4 (why: the shortlist page mints 2 per card, 100 slots against a cap of 64,
so a teaser that did the same would evict the export slot) and 9 (every role
sees the teaser and the link).

**Markup hooks this file pins** (the contract fixes content, not attribute
names, so the tester chose these and the coder must follow them):

* ``data-job-tab="shortlist"`` wraps the tab (as S2/S3 did for theirs);
* each shown candidate is ONE element carrying ``data-shortlist-row``; its text
  holds ``#<rank>``, the candidate's ``display_label`` and the score as the
  integer percentage the full page shows (``score_final * 100``, rounded);
* the zero-requirements disclosure is ``id="jd-no-requirements"`` with the full
  shortlist page's own sentence ("yielded no requirements at all");
* a failed read says "could not be loaded" (S3's wording for the same case).

Fixtures are DTO-built (``ShortlistEntry`` via ``model_dump(mode="json")``).
"""

from __future__ import annotations

from typing import Any

import pytest

from frontend import api_client, csrf
from frontend.app import app
from src.schemas.jobs import JDExtracted, Skill
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import JOB_ID, Node

_ROLES: tuple[str | None, ...] = (
    None,
    "admin",
    "recruiter",
    "hiring_manager",
    "auditor",
)
_ANY = pytest.mark.parametrize("role", _ROLES, ids=lambda r: r or "cas-off")
_SCALES = (32, 50)
_ZERO = {"description_parsed": JDExtracted(title="x")}
_HAS_SKILLS = {
    "description_parsed": JDExtracted(title="x", required_skills=[Skill(name="Go")])
}


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


@pytest.fixture
def plain_client() -> Any:
    """No page token in the session: the one-shot slot map starts empty."""
    app.config.update(TESTING=True)
    return app.test_client()


def _slots(client: Any) -> dict[str, str]:
    with client.session_transaction() as sess:
        stored = sess.get(csrf.SESSION_KEY)
    return dict(stored) if isinstance(stored, dict) else {}


def _get(client: Any, tab: str = "shortlist") -> tuple[str, Node]:
    resp = client.get(
        f"/jobs/{JOB_ID}?tab={tab}", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200, body[:400]
    return body, dom.parse(body)


def _tab(client: Any) -> Node:
    _body, root = _get(client)
    node = dom.tab_root(root, "shortlist")
    assert node is not None, 'the page has no data-job-tab="shortlist" element'
    return node


def _rows(tab: Node) -> list[Node]:
    return tab.find_all(attrs={"data-shortlist-row": None})


def _entries(n: int, **kw: Any) -> list[dict[str, Any]]:
    return [dom.make_shortlist_entry(i, **kw) for i in range(1, n + 1)]


def _pct(entry: dict[str, Any]) -> str:
    return str(round(entry["score_final"] * 100))


# ── the tab exists, is exclusive, and says nothing about "coming in" ────────


@_ANY
def test_the_shortlist_tab_renders_its_wrapper(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role)
    dom.stub_shortlist(monkeypatch, _entries(2))
    _body, root = _get(client)
    assert dom.tab_root(root, "shortlist") is not None
    assert "coming in S3" not in root.text()
    current = [a.text() for a in root.find_all("a", {"aria-current": "page"})]
    assert current == ["Shortlist"]


@pytest.mark.parametrize("tab", ["overview", "candidates", "requirements", "settings"])
def test_the_wrapper_renders_on_no_other_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    dom.install(monkeypatch)
    dom.stub_shortlist(monkeypatch, _entries(2))
    _body, root = _get(client, tab)
    assert dom.tab_root(root, "shortlist") is None


# ── empty, partial and full ─────────────────────────────────────────────────


def test_an_empty_shortlist_says_so_and_still_links_to_the_full_page(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    dom.stub_shortlist(monkeypatch, [])
    tab = _tab(client)
    assert _rows(tab) == []
    assert "no ranked candidates" in tab.text().lower()
    assert _full_link(tab) is not None


def test_a_partial_shortlist_shows_every_entry_it_has(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    dom.stub_shortlist(monkeypatch, _entries(3))
    tab = _tab(client)
    assert len(_rows(tab)) == 3
    assert "no ranked candidates" not in tab.text().lower()


@pytest.mark.parametrize("count", [5, 6, 12])
def test_a_full_shortlist_shows_at_most_five_rows(
    client: Any, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    dom.install(monkeypatch)
    dom.stub_shortlist(monkeypatch, _entries(count))
    assert len(_rows(_tab(client))) == min(count, 5)


def test_rows_show_rank_label_and_score_in_the_backends_order(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    entries = _entries(7)
    dom.stub_shortlist(monkeypatch, entries)
    rows = _rows(_tab(client))
    assert len(rows) == 5
    for rank, row in enumerate(rows, start=1):
        text = row.text()
        entry = entries[rank - 1]
        assert f"#{rank}" in text.split(), text
        assert entry["display_label"] in text
        assert _pct(entry) in text.replace("%", " ").split(), text
    ranks = [r.text() for r in rows]
    assert "Candidate 6" not in " ".join(ranks)
    assert "Candidate 7" not in " ".join(ranks)


# ── the backend's order is authoritative (reviewer finding, SPONSOR O2) ─────
#
# ``list_shortlist`` orders ``not_eligible`` candidates LAST, whatever their
# stored rank (shortlist_service.py ORDER BY (... = 'not_eligible') ASC, rank).
# Their rank and score are rank-time values the product cannot stand behind
# (``metrics_invalidated``), so the full page shows "—" and no score. A teaser
# that re-sorted by ``rank`` would put such a candidate back at "#1 92 %".


def _top_scorer_later_declared_not_eligible() -> list[dict[str, Any]]:
    # Backend order: eligible ranks 2..6 first, the voided rank-1 row last.
    voided = dom.make_shortlist_entry(
        1, score=0.92, display_label="Voided Top", work_authorization="not_eligible"
    )
    return [dom.make_shortlist_entry(r) for r in range(2, 7)] + [voided]


def test_the_teaser_keeps_the_backends_order_and_never_re_sorts_by_rank(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    dom.stub_shortlist(monkeypatch, _top_scorer_later_declared_not_eligible())
    labels = [r.text() for r in _rows(_tab(client))]
    assert len(labels) == 5
    assert "Voided Top" not in " ".join(labels), labels
    assert "Candidate 2" in labels[0], labels


@_ANY
def test_a_voided_candidate_shows_no_rank_and_no_score(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role)
    voided = dom.make_shortlist_entry(
        1, score=0.92, display_label="Voided Top", work_authorization="not_eligible"
    )
    assert voided["metrics_invalidated"] is True
    dom.stub_shortlist(monkeypatch, [dom.make_shortlist_entry(2), voided])
    rows = _rows(_tab(client))
    assert len(rows) == 2
    text = rows[1].text()
    assert "Voided Top" in text
    assert "—" in text, text
    assert "#1" not in text.split(), text
    assert "92" not in text.replace("%", " ").split(), text
    assert "%" not in text, text


def test_a_non_blind_job_shows_the_label_the_backend_sent(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch)
    dom.stub_shortlist(
        monkeypatch, [dom.make_shortlist_entry(1, display_label="Jordan Smith")]
    )
    assert "Jordan Smith" in _tab(client).text()


# ── blind review ────────────────────────────────────────────────────────────


@_ANY
def test_a_blind_job_shows_no_candidate_names_to_any_role(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    spies = dom.install(
        monkeypatch,
        role=role,
        job_over={"blind_review": True},
        resumes=[dom.make_resume(1), dom.make_resume(2)],
    )
    dom.stub_shortlist(
        monkeypatch,
        [
            dom.make_shortlist_entry(
                i, blinded=True, display_label=f"Candidate {chr(64 + i)}"
            )
            for i in (1, 2)
        ],
    )
    body, root = _get(client)
    tab = dom.tab_root(root, "shortlist")
    assert tab is not None
    labels = [r.text() for r in _rows(tab)]
    assert len(labels) == 2
    assert "Candidate A" in labels[0] and "Candidate B" in labels[1]
    assert "Candidate Number001" not in body
    assert "Candidate Number002" not in body
    assert "applicant-001.pdf" not in body
    assert spies.list_resumes.call_count == 0


# ── the link to the unchanged shortlist page ────────────────────────────────


def _full_link(tab: Node) -> Node | None:
    for link in tab.find_all("a"):
        if "open full shortlist" in link.text().lower():
            return link
    return None


@_ANY
@pytest.mark.parametrize("count", [0, 3, 50])
def test_every_role_gets_an_open_full_shortlist_link(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None, count: int
) -> None:
    dom.install(monkeypatch, role=role)
    dom.stub_shortlist(monkeypatch, _entries(count))
    link = _full_link(_tab(client))
    assert link is not None, "no 'Open full shortlist' link in the tab"
    assert link.get("href") == f"/jobs/{JOB_ID}/shortlist"


# ── disclosures the full page shows ─────────────────────────────────────────


@_ANY
def test_the_zero_requirements_disclosure_shows_on_the_teaser(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, job_over=_ZERO)
    dom.stub_shortlist(monkeypatch, [])
    tab = _tab(client)
    note = tab.find(attrs={"id": "jd-no-requirements"})
    assert note is not None, "no #jd-no-requirements disclosure inside the tab"
    assert "yielded no requirements at all" in note.text()


def test_the_zero_requirements_disclosure_shows_beside_a_stale_shortlist(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, job_over=_ZERO)
    dom.stub_shortlist(monkeypatch, _entries(3))
    tab = _tab(client)
    assert tab.find(attrs={"id": "jd-no-requirements"}) is not None
    assert len(_rows(tab)) == 3


def test_no_disclosure_when_the_jd_has_requirements(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, job_over=_HAS_SKILLS)
    dom.stub_shortlist(monkeypatch, _entries(3))
    assert _tab(client).find(attrs={"id": "jd-no-requirements"}) is None


def test_no_disclosure_for_a_job_that_was_never_parsed(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, state="parsing")
    dom.stub_shortlist(monkeypatch, [])
    assert _tab(client).find(attrs={"id": "jd-no-requirements"}) is None


# ── no controls, no tokens ──────────────────────────────────────────────────


@_ANY
def test_the_teaser_has_no_post_form_and_no_decision_control(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    dom.install(monkeypatch, role=role, job_over={"blind_review": True})
    dom.stub_shortlist(
        monkeypatch, _entries(5, blinded=True, display_label="Candidate Z")
    )
    tab = _tab(client)
    posts = [
        f for f in tab.find_all("form") if (f.get("method") or "").lower() == "post"
    ]
    assert posts == []
    text = tab.text().lower()
    for word in ("reveal", "withdraw", "reinstate", "regenerate"):
        assert word not in text, word


@pytest.mark.parametrize("size", _SCALES)
def test_the_tab_renders_at_most_five_rows_and_mints_nothing_at_scale(
    plain_client: Any, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    dom.install(monkeypatch)
    dom.stub_shortlist(monkeypatch, _entries(size))
    before = _slots(plain_client)

    tab = _tab(plain_client)

    assert len(_rows(tab)) == 5
    assert _slots(plain_client) == before == {}


@pytest.mark.parametrize("size", _SCALES)
def test_a_reload_at_scale_leaves_the_mapping_unchanged(
    plain_client: Any, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    dom.install(monkeypatch)
    dom.stub_shortlist(monkeypatch, _entries(size))
    with plain_client.session_transaction() as sess:
        sess[csrf.SESSION_KEY] = {"seeded-slot": "seeded-token"}

    for _ in range(3):
        _tab(plain_client)

    assert _slots(plain_client) == {"seeded-slot": "seeded-token"}


@pytest.mark.parametrize("size", _SCALES)
def test_a_previously_minted_export_slot_survives_a_shortlist_render(
    plain_client: Any, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    dom.install(monkeypatch, export=dom.make_export("ready"))
    dom.stub_shortlist(monkeypatch, _entries(size))
    plain_client.get(f"/jobs/{JOB_ID}?tab=candidates")
    minted = _slots(plain_client)
    assert len(minted) == 1, "the Candidates tab should have minted the export slot"

    _tab(plain_client)

    assert _slots(plain_client) == minted


# ── a failed read is tolerated ──────────────────────────────────────────────

_FAILURES: list[Exception] = [
    api_client.BackendUnavailable("shortlist backend down"),
    api_client.NotFound("no shortlist"),
    api_client.BadRequest("nope", status_code=422, detail={"detail": "nope"}),
]


@pytest.mark.parametrize("failure", _FAILURES, ids=lambda f: type(f).__name__)
@_ANY
def test_a_failed_shortlist_read_renders_a_tolerant_message_with_a_200(
    client: Any,
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
    failure: Exception,
) -> None:
    dom.install(monkeypatch, role=role)
    dom.stub_shortlist(monkeypatch, failure)

    body, root = _get(client)  # asserts 200

    tab = dom.tab_root(root, "shortlist")
    assert tab is not None
    assert _rows(tab) == []
    assert "could not be loaded" in tab.text().lower()
    assert _full_link(tab) is not None
    assert "Traceback" not in body
    assert root.find("h1") is not None, "the header must still render"


# ── reads ───────────────────────────────────────────────────────────────────


@_ANY
def test_the_shortlist_tab_reads_the_shortlist_and_nothing_else(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None
) -> None:
    spies = dom.install(monkeypatch, role=role, export=dom.make_export("ready"))
    reads = dom.stub_settings_reads(monkeypatch)
    shortlist = dom.stub_shortlist(monkeypatch, _entries(3))

    _get(client)

    assert shortlist.call_count == 1
    assert shortlist.call_args.args[0] == JOB_ID
    assert spies.list_resumes.call_count == 0
    assert spies.list_resume_splits.call_count == 0
    assert spies.get_latest_resume_export.call_count == 0
    assert reads.list_job_assignees.call_count == 0
    assert reads.list_users.call_count == 0


@pytest.mark.parametrize("tab", ["overview", "candidates", "requirements", "settings"])
@_ANY
def test_no_other_tab_reads_the_shortlist(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str | None, tab: str
) -> None:
    dom.install(monkeypatch, role=role)
    shortlist = dom.stub_shortlist(monkeypatch, _entries(3))

    _get(client, tab)

    assert shortlist.call_count == 0
