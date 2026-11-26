"""S2 (job-page redesign, Option A) -- the CSRF round trip, Candidates rows.

RED: the Candidates tab renders no forms yet (S1 placeholder), and
``_render_job_detail`` mints the export one-shot token on every tab.

Spec: ``S-contract.md`` section 4.3, rows 9 to 13: upload (cards 1 and 2),
start export, split upload, roster, and the one-shot export download.

**Why a plain client and not the ``csrf_client`` fixture.** ``csrf_client``
presents the page token in an ``X-CSRF-Token`` header on every request, so the
guard would pass whatever the form field held and a form with a wrong or empty
token would still "round trip". These tests use a bare ``app.test_client()``: the
page GET puts the page token in the cookie session, the form's own hidden
``csrf_token`` is extracted with ``html.parser`` (via ``job_page_v2_dom``), and
that value is the ONLY credential the POST carries.

Markup hooks are the ones pinned in ``test_frontend_job_page_v2_candidates.py``
(``data-intake-card``, ``data-export-menu``, ``data-job-tab``).

The one-shot rules pinned here: the Candidates tab mints at most one slot (the
export download, only when the latest export is ready) and every other tab mints
none; a wrong token (including the page token) never opens the download.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest

from frontend import api_client, csrf
from frontend.app import app
from tests.e2e.driver import page_token
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import EXPORT_ID, JOB_ID, Node

_SPLIT_ID = UUID("55555555-5555-4555-8555-555555555555")
_ZIP = b"PK\x03\x04 fake zip bytes"
_DISPOSITION = 'attachment; filename="job-resumes.zip"'


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _get(client: Any, tab: str = "candidates") -> str:
    resp = client.get(f"/jobs/{JOB_ID}?tab={tab}")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200, body[:400]
    return body


def _slots(client: Any) -> dict[str, str]:
    with client.session_transaction() as sess:
        stored = sess.get(csrf.SESSION_KEY)
    return dict(stored) if isinstance(stored, dict) else {}


def _pdf() -> tuple[io.BytesIO, str]:
    return (io.BytesIO(b"%PDF-1.4 fake"), "a.pdf")


def _archive() -> tuple[io.BytesIO, str]:
    return (io.BytesIO(b"PK\x03\x04 fake"), "résumés.zip")


def _csv() -> tuple[io.BytesIO, str]:
    return (io.BytesIO(b"Name,Email\nAda,ada@example.org\n"), "roster.csv")


# ── one case per form (contract 4.3 rows 9 to 12) ───────────────────────────


@dataclass(frozen=True)
class _Case:
    card: str | None  # the data-intake-card key, or None for the export menu
    suffix: str  # the form's action ends with this
    api: str  # the api_client function the handler calls
    returns: Any
    extra: Callable[[], dict[str, Any]]
    lands_on_tab: bool  # False: success goes to the split review page


_CASES: dict[str, _Case] = {
    "upload_single": _Case(
        card="single",
        suffix=f"/jobs/{JOB_ID}/resumes",
        api="upload_resumes",
        returns=[{"outcome": "accepted", "original_filename": "a.pdf"}],
        extra=lambda: {"files": _pdf(), "consent_acknowledged": "true"},
        lands_on_tab=True,
    ),
    "upload_zip": _Case(
        card="zip",
        suffix=f"/jobs/{JOB_ID}/resumes",
        api="upload_resumes",
        returns=[{"outcome": "accepted", "original_filename": "a.pdf"}],
        extra=lambda: {"files": _archive(), "consent_acknowledged": "true"},
        lands_on_tab=True,
    ),
    "export_start": _Case(
        card=None,
        suffix=f"/jobs/{JOB_ID}/resumes/exports",
        api="start_resume_export",
        returns={"id": str(EXPORT_ID), "status": "queued"},
        extra=lambda: {},
        lands_on_tab=True,
    ),
    "split_upload": _Case(
        card="taleo-pdf",
        suffix=f"/jobs/{JOB_ID}/resumes/split-upload",
        api="split_upload",
        returns={"split_ids": [str(_SPLIT_ID)]},
        extra=lambda: {"files": _pdf(), "consent_acknowledged": "true"},
        lands_on_tab=False,
    ),
    "roster": _Case(
        card="roster",
        suffix=f"/jobs/{JOB_ID}/candidate-roster",
        api="upload_candidate_roster",
        returns={"matched": 1, "unmatched_csv_rows": []},
        extra=lambda: {"file": _csv()},
        lands_on_tab=True,
    ),
}


def _scope(root: Node, case: _Case) -> Node:
    tab = dom.tab_root(root)
    assert tab is not None, 'no data-job-tab="candidates" element'
    if case.card is None:
        found = tab.find(attrs={"data-export-menu": None})
    else:
        found = dom.card(tab, case.card)
    assert found is not None, f"no {case.card or 'export menu'} on the tab"
    return found


def _form(root: Node, case: _Case) -> Node:
    forms = dom.forms_posting_to(_scope(root, case), case.suffix)
    assert len(forms) == 1, f"expected one form posting to {case.suffix}"
    return forms[0]


def _stub(monkeypatch: pytest.MonkeyPatch, case: _Case) -> MagicMock:
    spy = MagicMock(return_value=case.returns)
    monkeypatch.setattr(api_client, case.api, spy)
    return spy


def _submit(client: Any, form: Node, case: _Case, *, token: bool = True) -> Any:
    data: dict[str, Any] = dom.hidden_inputs(form)
    if not token:
        data.pop("csrf_token", None)
    data.update(case.extra())
    return client.post(
        form.get("action") or "", data=data, content_type="multipart/form-data"
    )


@pytest.mark.parametrize("name", list(_CASES))
def test_form_roundtrips(
    client: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    case = _CASES[name]
    dom.install(monkeypatch)
    spy = _stub(monkeypatch, case)
    body = _get(client)
    form = _form(dom.parse(body), case)
    token = dom.hidden_inputs(form).get("csrf_token")
    assert token, "the form carries no csrf_token"
    assert token == page_token(body), "an ordinary form must carry the PAGE token"

    resp = _submit(client, form, case)

    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    spy.assert_called_once()
    where = urlsplit(resp.headers["Location"])
    if case.lands_on_tab:
        assert where.path == f"/jobs/{JOB_ID}"
        assert parse_qs(where.query) == {"tab": ["candidates"]}
    else:
        assert where.path == f"/resume-splits/{_SPLIT_ID}"


@pytest.mark.parametrize("name", list(_CASES))
def test_a_form_with_its_token_stripped_is_403_not_a_silent_success(
    client: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    case = _CASES[name]
    dom.install(monkeypatch)
    spy = _stub(monkeypatch, case)
    form = _form(dom.parse(_get(client)), case)

    resp = _submit(client, form, case, token=False)

    assert resp.status_code == 403
    spy.assert_not_called()


@pytest.mark.parametrize("name", list(_CASES))
def test_a_form_with_a_forged_token_is_403(
    client: Any, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    case = _CASES[name]
    dom.install(monkeypatch)
    spy = _stub(monkeypatch, case)
    form = _form(dom.parse(_get(client)), case)
    data: dict[str, Any] = dom.hidden_inputs(form)
    data["csrf_token"] = "forged-" + data["csrf_token"]
    data.update(case.extra())

    resp = client.post(
        form.get("action") or "", data=data, content_type="multipart/form-data"
    )

    assert resp.status_code == 403
    spy.assert_not_called()


# ── the structural guard, Candidates slice ──────────────────────────────────


@pytest.mark.parametrize("state", ["parsing", "failed", "parsed", "open", "closed"])
def test_every_post_form_on_the_candidates_tab_has_a_nonempty_token(
    client: Any, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    dom.install(
        monkeypatch,
        state=state,
        splits=[dom.make_split(1)],
        export=dom.make_export("ready"),
    )
    root = dom.parse(_get(client))
    forms = [
        f for f in root.find_all("form") if (f.get("method") or "").lower() == "post"
    ]
    assert forms, "a writer's Candidates tab has no POST form at all"
    for form in forms:
        token = dom.hidden_inputs(form).get("csrf_token")
        assert token, f"form {form.get('action')!r} has an empty csrf_token"


# ── row 13: the one-shot export download ────────────────────────────────────


def _download_form(root: Node) -> Node:
    tab = dom.tab_root(root)
    assert tab is not None
    path = f"/resume-exports/{EXPORT_ID}/download"
    forms = dom.forms_posting_to(tab, path)
    assert len(forms) == 1, "the Candidates tab has no download form"
    return forms[0]


def test_export_download_token_roundtrips_once(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, export=dom.make_export("ready"))
    spy = MagicMock(return_value=(_ZIP, _DISPOSITION))
    monkeypatch.setattr(api_client, "download_resume_export", spy)
    body = _get(client)
    form = _download_form(dom.parse(body))
    data = dom.hidden_inputs(form)
    url = form.get("action") or ""
    assert data.get("csrf_token")
    assert data["csrf_token"] != page_token(body), "must be the one-shot token"

    first = client.post(url, data=data)
    assert first.status_code == 200
    assert first.data == _ZIP
    assert first.headers["Content-Type"].startswith("application/zip")
    assert first.headers["Cache-Control"] == "no-store"
    assert first.headers["Content-Disposition"] == _DISPOSITION
    assert spy.call_args.args[0] == EXPORT_ID

    second = client.post(url, data=data)
    assert second.status_code == 403
    assert spy.call_count == 1


def test_the_page_token_does_not_open_the_export_download(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, export=dom.make_export("ready"))
    spy = MagicMock(return_value=(_ZIP, _DISPOSITION))
    monkeypatch.setattr(api_client, "download_resume_export", spy)
    body = _get(client)
    form = _download_form(dom.parse(body))
    data = dom.hidden_inputs(form)
    data["csrf_token"] = page_token(body) or ""
    assert data["csrf_token"], "the page carries no page token to try"

    resp = client.post(form.get("action") or "", data=data)

    assert resp.status_code == 403
    spy.assert_not_called()


def test_a_reload_after_the_download_remints_a_working_form(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, export=dom.make_export("ready"))
    spy = MagicMock(return_value=(_ZIP, _DISPOSITION))
    monkeypatch.setattr(api_client, "download_resume_export", spy)
    form = _download_form(dom.parse(_get(client)))
    url = form.get("action") or ""
    assert client.post(url, data=dom.hidden_inputs(form)).status_code == 200

    again = _download_form(dom.parse(_get(client)))

    assert client.post(url, data=dom.hidden_inputs(again)).status_code == 200
    assert spy.call_count == 2


# ── the mint happens on the Candidates tab and nowhere else ─────────────────


@pytest.mark.parametrize("tab", ["overview", "requirements", "shortlist", "settings"])
def test_the_export_download_slot_is_minted_only_on_the_candidates_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, tab: str
) -> None:
    dom.install(monkeypatch, export=dom.make_export("ready"))

    _get(client, tab)
    assert _slots(client) == {}, f"the {tab} tab minted a one-shot token"

    _get(client, "candidates")
    assert len(_slots(client)) == 1, "the Candidates tab must mint exactly one"


def test_a_non_ready_export_mints_nothing_even_on_the_candidates_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    dom.install(monkeypatch, export=dom.make_export("building"))
    _get(client)
    assert _slots(client) == {}


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_a_non_writer_mints_nothing_on_any_tab(
    client: Any, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    dom.install(monkeypatch, role=role, export=dom.make_export("ready"))
    cookie = {"Cookie": "ra_session=tok-live"}
    for tab in ("overview", "candidates", "requirements", "shortlist"):
        resp = client.get(f"/jobs/{JOB_ID}?tab={tab}", headers=cookie)
        assert resp.status_code == 200
    assert _slots(client) == {}
