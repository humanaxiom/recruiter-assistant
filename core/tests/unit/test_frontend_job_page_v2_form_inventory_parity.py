"""S3 (job-page redesign, Option A) -- the "feature vanished" guard.

RED: the Requirements and Settings tabs are placeholders, so the v2 pages carry
none of the v1 page's details, requirements, re-parse, description, assignee,
status (Settings) and blind-review forms.

Spec: ``S-contract.md`` section 3 "S3", the parity row. For a writer on a job
with a ready export, pending splits and assignees, the multiset of POST form
actions on the v1 page (flag off) must equal the multiset over the five v2 tabs
(flag on), save the deliberate duplicates:

* ``upload_resumes`` is rendered twice in v2 (cards 1 and 2), open job only;
* ``transition_status`` ``to=open`` is rendered again OUTSIDE Settings on a
  parsed draft: once in the header (the contract's deliberate duplicate) and
  once in the Candidates "parsed" banner (pinned by S2's candidates tests).

A form is keyed by ``(action, to)``: ``to`` is the hidden status target, empty
for every other form, so "Close" and "Archive" are distinct features. Per-row
URLs are normalised: the job id, the export id and every other uuid (an
assignee row, a split row) become placeholders, and a query string is dropped.

The v1 page is the baseline, so its half of every assertion passes today; the
inventory of the baseline itself is pinned so the comparison cannot be vacuous.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pytest

from src.schemas.jobs import JDExtracted
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import EXPORT_ID, JOB_ID, Node

_TABS = ("overview", "candidates", "requirements", "shortlist", "settings")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
_ZERO: dict[str, Any] = {"description_parsed": JDExtracted(title="x")}
_Key = tuple[str, str]

_VARIANTS: dict[str, tuple[str, dict[str, Any]]] = {
    "open": ("open", {}),
    "failed-draft": ("failed", {}),
    "parsed-draft": ("parsed", {}),
    "zero-requirements-draft": ("parsed", _ZERO),
    "closed": ("closed", {}),
}
_JOB = "/jobs/<job>"

# Where each v1 feature lives in v2 (contract section 3, S3 and S2 tables).
_HOME: dict[str, str] = {
    f"{_JOB}/details": "settings",
    f"{_JOB}/assignees": "settings",
    f"{_JOB}/assignees/<id>/remove": "settings",
    f"{_JOB}/status": "settings",
    f"{_JOB}/blind-review": "settings",
    f"{_JOB}/requirements": "requirements",
    f"{_JOB}/reparse": "requirements",
    f"{_JOB}/description": "requirements",
    f"{_JOB}/resumes": "candidates",
    f"{_JOB}/resumes/exports": "candidates",
    f"{_JOB}/resumes/split-upload": "candidates",
    f"{_JOB}/candidate-roster": "candidates",
    "/resume-exports/<export>/download": "candidates",
}


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _normalise(action: str) -> str:
    path = action.split("?", 1)[0]
    path = path.replace(str(JOB_ID), "<job>").replace(str(EXPORT_ID), "<export>")
    return _UUID.sub("<id>", path)


def _keys(root: Node) -> Counter[_Key]:
    found: Counter[_Key] = Counter()
    for form in root.find_all("form"):
        if (form.get("method") or "").lower() != "post":
            continue
        action = _normalise(form.get("action") or "")
        to = dom.hidden_inputs(form).get("to", "") if action.endswith("/status") else ""
        found[(action, to)] += 1
    return found


def _arrange(
    monkeypatch: pytest.MonkeyPatch, *, flag: bool, state: str, over: dict[str, Any]
) -> None:
    dom.install(
        monkeypatch,
        flag=flag,
        state=state,
        job_over=over,
        splits=[dom.make_split(1), dom.make_split(2)],
        export=dom.make_export("ready"),
    )
    dana = dom.make_user(1, cas_username="dana", display_name="Dana Lee")
    priya = dom.make_user(2, cas_username="priya", display_name="Priya Singh")
    dom.stub_settings_reads(monkeypatch, assignees=[dana, priya], users=[dana, priya])


def _page(client: Any, tab: str | None) -> Node:
    url = f"/jobs/{JOB_ID}" if tab is None else f"/jobs/{JOB_ID}?tab={tab}"
    resp = client.get(url)
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200, body[:400]
    return dom.parse(body)


def _v1(client: Any, monkeypatch: pytest.MonkeyPatch, variant: str) -> Counter[_Key]:
    state, over = _VARIANTS[variant]
    _arrange(monkeypatch, flag=False, state=state, over=over)
    root = _page(client, None)
    assert dom.tab_root(root, "candidates") is None, "the flag-off page is not v1"
    return _keys(root)


def _v2(
    client: Any, monkeypatch: pytest.MonkeyPatch, variant: str
) -> dict[str, Counter[_Key]]:
    state, over = _VARIANTS[variant]
    _arrange(monkeypatch, flag=True, state=state, over=over)
    return {tab: _keys(_page(client, tab)) for tab in _TABS}


def _union(per_tab: dict[str, Counter[_Key]]) -> Counter[_Key]:
    total: Counter[_Key] = Counter()
    for counts in per_tab.values():
        total.update(counts)
    return total


def _allowed_extra(variant: str) -> Counter[_Key]:
    state, _over = _VARIANTS[variant]
    if state == "open":
        return Counter({(f"{_JOB}/resumes", ""): 1})
    if state == "parsed":
        return Counter({(f"{_JOB}/status", "open"): 2})
    return Counter()


# ── the baseline is not vacuous ─────────────────────────────────────────────


def test_the_v1_baseline_inventory_for_an_open_job(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = set(_v1(client, monkeypatch, "open"))
    wanted = {
        (f"{_JOB}/details", ""),
        (f"{_JOB}/requirements", ""),
        (f"{_JOB}/assignees", ""),
        (f"{_JOB}/assignees/<id>/remove", ""),
        (f"{_JOB}/status", "closed"),
        (f"{_JOB}/status", "archived"),
        (f"{_JOB}/blind-review", ""),
        (f"{_JOB}/resumes", ""),
        (f"{_JOB}/resumes/exports", ""),
        (f"{_JOB}/resumes/split-upload", ""),
        (f"{_JOB}/candidate-roster", ""),
        ("/resume-exports/<export>/download", ""),
    }
    assert wanted <= keys, f"the v1 page lost {sorted(wanted - keys)}"


def test_the_v1_baseline_inventory_for_a_failed_draft(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = set(_v1(client, monkeypatch, "failed-draft"))
    assert (f"{_JOB}/reparse", "") in keys
    assert (f"{_JOB}/description", "") in keys
    assert (f"{_JOB}/status", "open") in keys


# ── the guard: nothing vanished, nothing is duplicated by accident ──────────


@pytest.mark.parametrize("variant", list(_VARIANTS))
def test_no_v1_form_vanished_in_v2_and_no_new_form_appeared(
    client: Any, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    before = set(_v1(client, monkeypatch, variant))
    after = set(_union(_v2(client, monkeypatch, variant)))
    assert before - after == set(), f"v2 dropped {sorted(before - after)}"
    assert after - before == set(), f"v2 added {sorted(after - before)}"


@pytest.mark.parametrize("variant", list(_VARIANTS))
def test_no_form_is_duplicated_in_v2_except_the_deliberate_ones(
    client: Any, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    before = _v1(client, monkeypatch, variant)
    after = _union(_v2(client, monkeypatch, variant))
    expected = before + _allowed_extra(variant)
    assert after == expected, (
        f"v2 form multiset differs: missing {sorted((expected - after).items())}, "
        f"unexpected {sorted((after - expected).items())}"
    )


@pytest.mark.parametrize("variant", list(_VARIANTS))
def test_each_feature_lives_on_the_tab_the_contract_assigns(
    client: Any, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    per_tab = _v2(client, monkeypatch, variant)
    for tab, counts in per_tab.items():
        for action, to in counts:
            home = _HOME.get(action)
            assert home is not None, f"unexpected form {action} on {tab}"
            header_open = action == f"{_JOB}/status" and to == "open"
            assert tab == home or header_open, f"{action} (to={to}) is on {tab}"
    for key in _v1(client, monkeypatch, variant):
        assert per_tab[_HOME[key[0]]][key] >= 1, f"{key} is not on {_HOME[key[0]]}"
