"""S2 (job-page redesign, Option A) -- redirect and re-render targets.

RED: every candidate handler still redirects to the bare ``/jobs/<id>`` and every
400 re-render still draws the Overview (or, once S1's placeholder is hit, a
page with no Candidates content).

Spec: ``S-contract.md`` section 3.1 rows 2 to 8:

====  ==========================  ==========================================
row   handler                     what lands on the job page
====  ==========================  ==========================================
2     ``upload_resumes``          success redirect and 400 re-renders
3     ``upload_candidate_roster`` success redirect and 400 re-renders
4     ``split_upload``            flash-and-return redirects and 400 re-renders
                                  (success goes to ``split_review``, unchanged)
5     ``export_start``            success and the flashed-BadRequest redirect
6     ``export_download``         the Conflict redirect (job id from the form)
7     ``split_accept``            redirect via ``_redirect_to_split_job``
8     ``split_discard``           redirect via ``_redirect_to_split_job``
====  ==========================  ==========================================

With the flag ON the ``Location`` is ``/jobs/<id>?tab=candidates`` and a 400
re-render shows the Candidates tab (``data-job-tab="candidates"``, the Candidates
nav link ``aria-current="page"``) with the error text. With the flag OFF the
``Location`` is the exact v1 bare ``/jobs/<id>`` (a parity twin of the existing
``Location.endswith`` assertions) and the re-render is the v1 page.

``split_review.html``'s "Back to the job" link gets ``tab=candidates`` only when
v2 is on; with v2 off its markup is byte-identical to today's.

Markup hooks are the ones pinned in ``test_frontend_job_page_v2_candidates.py``.
Fixtures come from ``job_page_v2_dom`` (DTO-built job and rows; the split and
export read models are dicts because no DTO class exists for them).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest

from frontend import api_client
from tests.unit import job_page_v2_dom as dom
from tests.unit.job_page_v2_dom import EXPORT_ID, JOB_ID

_SPLIT_ID = UUID("55555555-5555-4555-8555-555555555555")
_OTHER_SPLIT_ID = UUID("66666666-6666-4666-8666-666666666666")
_ZIP = b"PK\x03\x04 fake zip bytes"
_FLAGS = pytest.mark.parametrize("flag", [True, False], ids=["v2", "v1"])
_Scenario = Callable[[Any, pytest.MonkeyPatch], Any]


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _pdf(name: str = "a.pdf") -> tuple[io.BytesIO, str]:
    return (io.BytesIO(b"%PDF-1.4 fake"), name)


def _reject(message: str) -> api_client.BadRequest:
    return api_client.BadRequest(
        "backend 422",
        status_code=422,
        detail={"message": message},
    )


def _post(client: Any, path: str, data: dict[str, Any]) -> Any:
    return client.post(path, data=data, content_type="multipart/form-data")


def _on_candidates(resp: Any) -> None:
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    where = urlsplit(resp.headers["Location"])
    assert where.path == f"/jobs/{JOB_ID}"
    assert parse_qs(where.query) == {"tab": ["candidates"]}


def _bare(resp: Any) -> None:
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    location = resp.headers["Location"]
    assert location.endswith(f"/jobs/{JOB_ID}")
    assert "?" not in location


# ── scenarios: each one performs a POST that ends on the job page ───────────


def _upload_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    rows = [{"outcome": "accepted", "original_filename": "a.pdf"}]
    mp.setattr(api_client, "upload_resumes", MagicMock(return_value=rows))
    data = {"files": _pdf(), "consent_acknowledged": "true"}
    return _post(client, f"/jobs/{JOB_ID}/resumes", data)


def _roster_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    report = {"matched": 1, "unmatched_csv_rows": []}
    mp.setattr(api_client, "upload_candidate_roster", MagicMock(return_value=report))
    data = {"file": (io.BytesIO(b"Name,Email\n"), "roster.csv")}
    return _post(client, f"/jobs/{JOB_ID}/candidate-roster", data)


def _split_rejected(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(side_effect=_reject("Only PDF exports can be split."))
    mp.setattr(api_client, "split_upload", spy)
    data = {"files": _pdf(), "consent_acknowledged": "true"}
    return _post(client, f"/jobs/{JOB_ID}/resumes/split-upload", data)


def _split_not_queued(client: Any, mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(api_client, "split_upload", MagicMock(return_value={"split_ids": []}))
    data = {"files": _pdf(), "consent_acknowledged": "true"}
    return _post(client, f"/jobs/{JOB_ID}/resumes/split-upload", data)


def _export_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    started = {"id": str(EXPORT_ID), "status": "queued"}
    mp.setattr(api_client, "start_resume_export", MagicMock(return_value=started))
    return client.post(f"/jobs/{JOB_ID}/resumes/exports")


def _export_rejected(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(side_effect=_reject("An export is already running."))
    mp.setattr(api_client, "start_resume_export", spy)
    return client.post(f"/jobs/{JOB_ID}/resumes/exports")


def _download_conflict(client: Any, mp: pytest.MonkeyPatch) -> Any:
    conflict = api_client.Conflict("backend 409", status_code=409, detail={})
    mp.setattr(api_client, "download_resume_export", MagicMock(side_effect=conflict))
    page = client.get(f"/jobs/{JOB_ID}?tab=candidates").get_data(as_text=True)
    tab = dom.parse(page)
    path = f"/resume-exports/{EXPORT_ID}/download"
    (form,) = dom.forms_posting_to(tab, path)
    return client.post(path, data=dom.hidden_inputs(form))


def _split_view(status: str = "proposed") -> dict[str, Any]:
    return {
        "id": str(_SPLIT_ID),
        "job_id": str(JOB_ID),
        "status": status,
        "proposal_version": 3,
        "method": None,
        "fallback_reason": None,
        "failure_reason": "no_text_layer" if status == "failed" else None,
        "page_count": 8,
        "source_filename": "export.pdf",
        "blind_review": False,
        "created_at": dom.STAMP,
        "proposal": None,
        "diagnostics": {},
    }


def _accept_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(api_client, "get_resume_split", MagicMock(return_value=_split_view()))
    rows = [{"outcome": "accepted", "original_filename": "a.pdf"}]
    mp.setattr(api_client, "accept_resume_split", MagicMock(return_value=rows))
    return client.post(
        f"/resume-splits/{_SPLIT_ID}/accept",
        data={"proposal_version": "3"},
    )


def _discard_ok(client: Any, mp: pytest.MonkeyPatch) -> Any:
    mp.setattr(api_client, "get_resume_split", MagicMock(return_value=_split_view()))
    done = {"id": str(_SPLIT_ID), "status": "discarded"}
    mp.setattr(api_client, "discard_resume_split", MagicMock(return_value=done))
    return client.post(f"/resume-splits/{_SPLIT_ID}/discard")


_REDIRECTS: dict[str, _Scenario] = {
    "2-upload_resumes": _upload_ok,
    "3-upload_candidate_roster": _roster_ok,
    "4-split_upload-rejected": _split_rejected,
    "4-split_upload-not-queued": _split_not_queued,
    "5-export_start": _export_ok,
    "5-export_start-rejected": _export_rejected,
    "6-export_download-conflict": _download_conflict,
    "7-split_accept": _accept_ok,
    "8-split_discard": _discard_ok,
}


@pytest.fixture
def arm(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """Install settings (flag on/off) and the job-page stubs for one test."""

    def _arm(flag: bool) -> None:
        dom.install(monkeypatch, flag=flag, export=dom.make_export("ready"))

    return _arm


@_FLAGS
@pytest.mark.parametrize("name", list(_REDIRECTS))
def test_the_job_page_redirect_lands_on_the_tab_only_when_v2_is_on(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    flag: bool,
    name: str,
) -> None:
    arm(flag)

    resp = _REDIRECTS[name](client, monkeypatch)

    if flag:
        _on_candidates(resp)
    else:
        _bare(resp)


# ── the flash survives the tab redirect ─────────────────────────────────────


@pytest.mark.parametrize("name", ["2-upload_resumes", "5-export_start"])
def test_the_flash_is_still_queued_when_redirecting_to_the_tab(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    name: str,
) -> None:
    arm(True)

    resp = _REDIRECTS[name](client, monkeypatch)

    _on_candidates(resp)
    with client.session_transaction() as sess:
        assert sess.get("_flashes")


# ── split_upload success goes to the review page, flag or not ───────────────


@_FLAGS
def test_split_upload_success_still_goes_to_the_split_review_page(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    flag: bool,
) -> None:
    arm(flag)
    queued = {"split_ids": [str(_SPLIT_ID), str(_OTHER_SPLIT_ID)]}
    monkeypatch.setattr(api_client, "split_upload", MagicMock(return_value=queued))
    data = {"files": _pdf(), "consent_acknowledged": "true"}

    resp = _post(client, f"/jobs/{JOB_ID}/resumes/split-upload", data)

    assert resp.status_code == 302
    where = urlsplit(resp.headers["Location"])
    assert where.path == f"/resume-splits/{_SPLIT_ID}"
    assert where.query == ""


# ── export_download: the job id comes from the form, never echoed raw ───────


@_FLAGS
def test_a_download_conflict_with_a_junk_job_id_goes_to_the_index(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    flag: bool,
) -> None:
    arm(flag)
    conflict = api_client.Conflict("backend 409", status_code=409, detail={})
    monkeypatch.setattr(
        api_client,
        "download_resume_export",
        MagicMock(side_effect=conflict),
    )
    page = client.get(f"/jobs/{JOB_ID}?tab=candidates").get_data(as_text=True)
    path = f"/resume-exports/{EXPORT_ID}/download"
    (form,) = dom.forms_posting_to(dom.parse(page), path)
    data = dom.hidden_inputs(form)
    data["job_id"] = "not-a-uuid"

    resp = client.post(path, data=data)

    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "tab=" not in location
    assert "not-a-uuid" not in location
    assert "/jobs/" not in location


@_FLAGS
def test_split_accept_with_an_unreadable_split_still_goes_to_the_index(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    flag: bool,
) -> None:
    arm(flag)
    monkeypatch.setattr(
        api_client,
        "get_resume_split",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    rows = [{"outcome": "accepted"}]
    monkeypatch.setattr(api_client, "accept_resume_split", MagicMock(return_value=rows))

    resp = client.post(
        f"/resume-splits/{_SPLIT_ID}/accept",
        data={"proposal_version": "3"},
    )

    assert resp.status_code == 302
    assert "tab=" not in resp.headers["Location"]
    assert "/jobs/" not in resp.headers["Location"]


# ── 400 re-renders: the Candidates tab with the error ───────────────────────


def _no_consent_upload(client: Any, mp: pytest.MonkeyPatch) -> Any:
    return _post(client, f"/jobs/{JOB_ID}/resumes", {"files": _pdf()})


def _no_file_upload(client: Any, mp: pytest.MonkeyPatch) -> Any:
    return _post(client, f"/jobs/{JOB_ID}/resumes", {"consent_acknowledged": "true"})


def _rejected_upload(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(side_effect=_reject("The archive held no résumés."))
    mp.setattr(api_client, "upload_resumes", spy)
    data = {"files": _pdf(), "consent_acknowledged": "true"}
    return _post(client, f"/jobs/{JOB_ID}/resumes", data)


def _no_file_roster(client: Any, mp: pytest.MonkeyPatch) -> Any:
    return _post(client, f"/jobs/{JOB_ID}/candidate-roster", {"note": "none"})


def _rejected_roster(client: Any, mp: pytest.MonkeyPatch) -> Any:
    spy = MagicMock(side_effect=_reject("The roster has no header row."))
    mp.setattr(api_client, "upload_candidate_roster", spy)
    data = {"file": (io.BytesIO(b"x"), "roster.csv")}
    return _post(client, f"/jobs/{JOB_ID}/candidate-roster", data)


def _no_consent_split(client: Any, mp: pytest.MonkeyPatch) -> Any:
    return _post(client, f"/jobs/{JOB_ID}/resumes/split-upload", {"files": _pdf()})


def _no_file_split(client: Any, mp: pytest.MonkeyPatch) -> Any:
    data = {"consent_acknowledged": "true"}
    return _post(client, f"/jobs/{JOB_ID}/resumes/split-upload", data)


_RERENDERS: dict[str, tuple[_Scenario, str]] = {
    "2-upload-no-consent": (_no_consent_upload, "You must confirm the candidate"),
    "2-upload-no-file": (_no_file_upload, "Select at least one résumé file"),
    "2-upload-rejected": (_rejected_upload, "The archive held no résumés."),
    "3-roster-no-file": (_no_file_roster, "Select a candidate-roster CSV file"),
    "3-roster-rejected": (_rejected_roster, "The roster has no header row."),
    "4-split-no-consent": (_no_consent_split, "You must confirm the candidates"),
    "4-split-no-file": (_no_file_split, "Select at least one combined Taleo export"),
}


@pytest.mark.parametrize("name", list(_RERENDERS))
def test_a_400_rerender_shows_the_candidates_tab_with_the_error_when_v2_is_on(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    name: str,
) -> None:
    arm(True)
    scenario, message = _RERENDERS[name]

    resp = scenario(client, monkeypatch)

    body = resp.get_data(as_text=True)
    assert resp.status_code == 400, body[:300]
    root = dom.parse(body)
    assert dom.tab_root(root) is not None, "the re-render is not the Candidates tab"
    current = [a.text() for a in root.find_all("a", {"aria-current": "page"})]
    assert current == ["Candidates"]
    assert message in root.text()
    errors = root.find_all("p", {"class": "error"})
    assert any(message in e.text() for e in errors)


@pytest.mark.parametrize("name", list(_RERENDERS))
def test_a_400_rerender_is_the_v1_page_when_v2_is_off(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    name: str,
) -> None:
    arm(False)
    scenario, message = _RERENDERS[name]

    resp = scenario(client, monkeypatch)

    body = resp.get_data(as_text=True)
    assert resp.status_code == 400, body[:300]
    assert "data-job-layout" not in body
    assert "data-job-tab" not in body
    assert message in dom.parse(body).text()


_GUARD_ONLY = (
    "2-upload-no-consent",
    "2-upload-no-file",
    "3-roster-no-file",
    "4-split-no-consent",
    "4-split-no-file",
)


@pytest.mark.parametrize("name", _GUARD_ONLY)
def test_a_guard_failure_rerender_never_reaches_the_backend(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    name: str,
) -> None:
    arm(True)
    spies = [MagicMock(), MagicMock(), MagicMock()]
    monkeypatch.setattr(api_client, "upload_resumes", spies[0])
    monkeypatch.setattr(api_client, "upload_candidate_roster", spies[1])
    monkeypatch.setattr(api_client, "split_upload", spies[2])

    resp = _RERENDERS[name][0](client, monkeypatch)

    assert resp.status_code == 400
    for spy in spies:
        spy.assert_not_called()


# ── split_review.html: the "Back to the job" link ───────────────────────────


def _review_page(client: Any, mp: pytest.MonkeyPatch, status: str) -> str:
    mp.setattr(
        api_client,
        "get_resume_split",
        MagicMock(return_value=_split_view(status)),
    )
    resp = client.get(f"/resume-splits/{_SPLIT_ID}")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return str(resp.get_data(as_text=True))


def _back_link(body: str) -> str:
    links = [
        a.get("href") or ""
        for a in dom.parse(body).find_all("a")
        if a.text() == "Back to the job"
    ]
    assert len(links) == 1, "the review page must have one 'Back to the job' link"
    return links[0]


@pytest.mark.parametrize("status", ["queued", "failed", "accepted"])
def test_the_back_link_gets_the_candidates_tab_only_when_v2_is_on(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    status: str,
) -> None:
    arm(True)

    href = urlsplit(_back_link(_review_page(client, monkeypatch, status)))

    assert href.path == f"/jobs/{JOB_ID}"
    assert parse_qs(href.query) == {"tab": ["candidates"]}


@pytest.mark.parametrize("status", ["queued", "failed", "accepted"])
def test_the_back_link_is_byte_identical_to_v1_when_v2_is_off(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
    status: str,
) -> None:
    arm(False)

    body = _review_page(client, monkeypatch, status)

    assert _back_link(body) == f"/jobs/{JOB_ID}"
    assert f'<a href="/jobs/{JOB_ID}">Back to the job</a>' in body
    assert "tab=" not in body


def test_a_409_rerender_of_the_review_page_keeps_the_tab_in_its_back_link(
    client: Any,
    arm: Callable[[bool], None],
    monkeypatch: Any,
) -> None:
    arm(True)
    monkeypatch.setattr(
        api_client,
        "get_resume_split",
        MagicMock(return_value=_split_view("accepted")),
    )
    conflict = api_client.Conflict(
        "x",
        status_code=409,
        detail={"message": "already accepted"},
    )
    monkeypatch.setattr(
        api_client,
        "discard_resume_split",
        MagicMock(side_effect=conflict),
    )

    resp = client.post(f"/resume-splits/{_SPLIT_ID}/discard")

    body = resp.get_data(as_text=True)
    assert resp.status_code == 409
    assert "already accepted" in body
    href = urlsplit(_back_link(body))
    assert parse_qs(href.query) == {"tab": ["candidates"]}
