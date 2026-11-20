"""RED -- slice E1: the Flask screens for the in-app Taleo split.

**Contract pinned for the coder** (everything is server-rendered; no JS):

``frontend.api_client`` (see ``test_frontend_split_api_client.py``):
``split_upload``, ``get_resume_split``, ``correct_resume_split``,
``accept_resume_split``, ``discard_resume_split``.

Routes (all POST routes are guarded by the ordinary reusable page CSRF token,
with NO exemption and NO new one-shot slot):

* ``POST /jobs/<id>/resumes/split-upload`` -- multipart ``files`` (repeated),
  ``consent_acknowledged``, ``csrf_token``. Success: 302 to
  ``/resume-splits/<first id>``. API 4xx: 302 back to the job with the API's
  message as a flash. No consent / no file: 400 with no backend call.
* ``GET /resume-splits/<id>`` -- ``split_review.html``.
* ``POST /resume-splits/<id>/correct|accept|discard``.

Review form field names: per applicant row ``n`` (1-based, dense) the text
inputs ``resume_pages_n`` and ``cover_pages_n`` pre-filled in the page-list
grammar (``"1-2,5"``); two spare empty rows ``n+1`` and ``n+2``; one
``ignored_pages`` input; hidden ``proposal_version``; hidden ``csrf_token`` in
every form. The correct form posts to ``/correct``; Accept and Discard are
their own small forms. A blank row (both inputs empty) is dropped from the
payload sent to ``correct_resume_split``.

``view`` dicts below mirror ``resume_splits._view`` and are JSON-shaped: every
timestamp and enum is a STRING, and the proposal / diagnostics are produced by
the API's own pydantic models via ``model_dump(mode="json")``.

NOT PINNED -- API GAP: ``resume_splits.py`` has no listing endpoint, so
``job_detail.html`` cannot show "pending splits for this job". Only the review
link after upload is pinned.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.schemas.resumes import ResumeUploadResult
from src.schemas.taleo_split import (
    ProposalApplicant,
    ProposalDiagnostics,
    SplitProposal,
)
from src.settings import Settings

_STAMP = "2026-11-20T12:00:00+00:00"


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


@pytest.fixture
def plain_client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


# ----------------------------------------------------------------- builders


def _job(job_id: Any, *, blind: bool = False) -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": "open",
        "blind_review": blind,
        "parsed_at": "2026-07-17T00:00:00Z",
        "description_parsed": {"required_skills": []},
    }


def _applicant(
    resume: list[int], cover: list[int] | None = None, name: str = ""
) -> ProposalApplicant:
    return ProposalApplicant(
        candidate_name=name, resume_pages=resume, cover_letter_pages=cover or []
    )


def _view(
    split_id: UUID,
    *,
    job_id: UUID | None = None,
    status: str = "proposed",
    version: int = 3,
    method: str = "llm",
    fallback_reason: str | None = None,
    failure_reason: str | None = None,
    applicants: list[ProposalApplicant] | None = None,
    ignored: list[int] | None = None,
    diagnostics: ProposalDiagnostics | None = None,
    blind: bool = False,
    page_count: int = 8,
) -> dict[str, Any]:
    proposal: dict[str, Any] | None = None
    if status == "proposed":
        proposal = SplitProposal(
            applicants=(
                applicants
                if applicants is not None
                else [
                    _applicant([1, 2], [3]),
                    _applicant([4, 5]),
                    _applicant([6, 7, 8]),
                ]
            ),
            ignored_pages=ignored or [],
        ).model_dump(mode="json")
        # ``resume_splits._view`` blanks the name on blind jobs and labels rows.
        for i, a in enumerate(proposal["applicants"], start=1):
            a["label"] = f"Applicant {i}"
            if blind:
                a["candidate_name"] = ""
    return {
        "id": str(split_id),
        "job_id": str(job_id or uuid4()),
        "status": status,
        "proposal_version": version,
        "method": method if status == "proposed" else None,
        "fallback_reason": fallback_reason,
        "failure_reason": failure_reason,
        "page_count": page_count,
        "source_filename": None if blind else "export.pdf",
        "blind_review": blind,
        "created_at": _STAMP,
        "proposal": proposal,
        "diagnostics": (diagnostics or ProposalDiagnostics()).model_dump(mode="json"),
    }


def _install(monkeypatch: Any, view: dict[str, Any]) -> MagicMock:
    spy = MagicMock(return_value=view)
    monkeypatch.setattr(api_client, "get_resume_split", spy)
    return spy


class _Page(HTMLParser):
    """Collects <form>s with their <input>s and the buttons inside them."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._form: dict[str, Any] | None = None
        self._button: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            self._form = {"attrs": a, "inputs": [], "buttons": []}
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            self._form["inputs"].append(a)
        elif tag == "button" and self._form is not None:
            self._button = {"attrs": a, "text": ""}
            self._form["buttons"].append(self._button)

    def handle_data(self, data: str) -> None:
        if self._button is not None:
            self._button["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        elif tag == "button":
            self._button = None


def _forms(body: str) -> list[dict[str, Any]]:
    page = _Page()
    page.feed(body)
    return page.forms


def _form_for(body: str, suffix: str) -> dict[str, Any] | None:
    for f in _forms(body):
        if f["attrs"].get("action", "").endswith(suffix):
            return f
    return None


def _inputs(form: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {i["name"]: i for i in form["inputs"] if "name" in i}


def _get(client: Any, split_id: UUID) -> tuple[int, str]:
    resp = client.get(f"/resume-splits/{split_id}")
    return resp.status_code, resp.get_data(as_text=True)


# ------------------------------------------------- job page: the upload form


def test_job_page_has_the_split_upload_form_for_a_writer(
    monkeypatch: Any, plain_client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])

    body = plain_client.get(f"/jobs/{job_id}").get_data(as_text=True)

    assert "Split a combined Taleo export" in body
    form = _form_for(body, f"/jobs/{job_id}/resumes/split-upload")
    assert form is not None, "no form posting to the split-upload route"
    assert form["attrs"].get("method", "").lower() == "post"
    assert form["attrs"].get("enctype") == "multipart/form-data"
    fields = _inputs(form)
    assert fields["files"]["type"] == "file"
    assert "multiple" in fields["files"]
    assert ".pdf" in fields["files"].get("accept", "").lower()
    assert fields["consent_acknowledged"]["type"] == "checkbox"
    assert fields["consent_acknowledged"].get("value") == "true"
    assert fields["csrf_token"]["type"] == "hidden"
    assert fields["csrf_token"]["value"]


def test_split_form_csrf_token_is_the_reusable_page_token(
    monkeypatch: Any, plain_client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    first = plain_client.get(f"/jobs/{job_id}").get_data(as_text=True)
    second = plain_client.get(f"/jobs/{job_id}").get_data(as_text=True)

    path = f"/jobs/{job_id}/resumes/split-upload"
    token_a = _inputs(_form_for(first, path) or {"inputs": []})["csrf_token"]["value"]
    token_b = _inputs(_form_for(second, path) or {"inputs": []})["csrf_token"]["value"]
    assert token_a == token_b  # reusable: not a one-shot slot


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_job_page_hides_the_split_form_from_a_non_writer(
    monkeypatch: Any, plain_client: Any, role: str
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client,
        "get_cas_user",
        MagicMock(
            return_value={
                "authenticated": True,
                "username": "jordan",
                "cas_enabled": True,
                "role": role,
            }
        ),
    )
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])

    body = plain_client.get(f"/jobs/{job_id}").get_data(as_text=True)

    assert "resumes/split-upload" not in body
    assert "Split a combined Taleo export" not in body


# ------------------------------------------------------------ upload route


def _pdf_part(name: str = "export.pdf") -> tuple[Any, str]:
    import io

    return (io.BytesIO(b"%PDF-1.4 fake"), name)


def test_upload_forwards_files_and_consent_then_redirects_to_first_review(
    monkeypatch: Any, client: Any
) -> None:
    job_id, first, second = uuid4(), uuid4(), uuid4()
    spy = MagicMock(
        return_value={
            "splits": [
                {"id": str(first), "status": "queued"},
                {"id": str(second), "status": "queued"},
            ],
            "split_ids": [str(first), str(second)],
        }
    )
    monkeypatch.setattr(api_client, "split_upload", spy)

    resp = client.post(
        f"/jobs/{job_id}/resumes/split-upload",
        data={
            "files": [_pdf_part("a.pdf"), _pdf_part("b.pdf")],
            "consent_acknowledged": "true",
        },
        content_type="multipart/form-data",
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/resume-splits/{first}")
    spy.assert_called_once()
    assert spy.call_args.args[0] == job_id
    sent = spy.call_args.args[1]
    assert [f[0] for f in sent] == ["a.pdf", "b.pdf"]
    assert all(f[1] == b"%PDF-1.4 fake" for f in sent)
    assert spy.call_args.kwargs["consent_acknowledged"] is True


def test_upload_without_consent_is_refused_before_the_backend(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    spy = MagicMock()
    monkeypatch.setattr(api_client, "split_upload", spy)

    resp = client.post(
        f"/jobs/{job_id}/resumes/split-upload",
        data={"files": [_pdf_part()]},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert "consent" in resp.get_data(as_text=True).lower()
    spy.assert_not_called()


def test_upload_without_a_file_is_refused_before_the_backend(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    spy = MagicMock()
    monkeypatch.setattr(api_client, "split_upload", spy)

    resp = client.post(
        f"/jobs/{job_id}/resumes/split-upload",
        data={"consent_acknowledged": "true"},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    spy.assert_not_called()


@pytest.mark.parametrize(
    "detail",
    [
        {"code": "file.rejected", "message": "export has 400 pages; the cap is 300"},
        {"detail": "only PDF files can be split"},
    ],
)
def test_upload_api_4xx_flashes_the_api_message_on_the_job_page(
    monkeypatch: Any, client: Any, detail: dict[str, Any]
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])

    def _raise(*a: Any, **k: Any) -> Any:
        raise api_client.BadRequest("x", status_code=422, detail=detail)

    monkeypatch.setattr(api_client, "split_upload", _raise)

    resp = client.post(
        f"/jobs/{job_id}/resumes/split-upload",
        data={"files": [_pdf_part()], "consent_acknowledged": "true"},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    expected = str(detail.get("message") or detail.get("detail"))
    assert expected in page


def test_upload_backend_down_is_503_and_unknown_job_is_404(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()

    def _down(*a: Any, **k: Any) -> Any:
        raise api_client.BackendUnavailable("down")

    def _missing(*a: Any, **k: Any) -> Any:
        raise api_client.NotFound("gone")

    data = {"files": [_pdf_part()], "consent_acknowledged": "true"}
    monkeypatch.setattr(api_client, "split_upload", _down)
    resp = client.post(
        f"/jobs/{job_id}/resumes/split-upload",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 503

    data = {"files": [_pdf_part()], "consent_acknowledged": "true"}
    monkeypatch.setattr(api_client, "split_upload", _missing)
    resp = client.post(
        f"/jobs/{job_id}/resumes/split-upload",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 404


def test_upload_redirect_carries_no_candidate_name_on_a_blind_job(
    monkeypatch: Any, client: Any
) -> None:
    job_id, split_id = uuid4(), uuid4()
    monkeypatch.setattr(
        api_client,
        "split_upload",
        MagicMock(
            return_value={
                "splits": [{"id": str(split_id), "status": "queued"}],
                "split_ids": [str(split_id)],
            }
        ),
    )
    resp = client.post(
        f"/jobs/{job_id}/resumes/split-upload",
        data={"files": [_pdf_part("Jane Roe.pdf")], "consent_acknowledged": "true"},
        content_type="multipart/form-data",
    )
    location = resp.headers["Location"]
    assert "Jane" not in location and "Roe" not in location
    assert re.fullmatch(r".*/resume-splits/[0-9a-f-]{36}", location)


# ----------------------------------------------------------- review: states


@pytest.mark.parametrize("status", ["queued", "segmenting"])
def test_in_flight_split_says_still_working_and_has_no_forms(
    monkeypatch: Any, client: Any, status: str
) -> None:
    split_id = uuid4()
    _install(monkeypatch, _view(split_id, status=status))

    code, body = _get(client, split_id)

    assert code == 200
    low = body.lower()
    assert "still working" in low
    assert "refresh" in low
    assert status in low
    assert _form_for(body, "/accept") is None
    assert _form_for(body, "/correct") is None


def test_failed_split_shows_failure_and_offers_no_accept(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    _install(
        monkeypatch,
        _view(split_id, status="failed", failure_reason="unreadable_pdf"),
    )

    code, body = _get(client, split_id)

    assert code == 200
    assert "failed" in body.lower()
    assert _form_for(body, "/accept") is None


def test_unknown_split_is_404_and_backend_down_is_503(
    monkeypatch: Any, client: Any
) -> None:
    def _missing(*a: Any, **k: Any) -> Any:
        raise api_client.NotFound("gone")

    def _down(*a: Any, **k: Any) -> Any:
        raise api_client.BackendUnavailable("down")

    monkeypatch.setattr(api_client, "get_resume_split", _missing)
    assert client.get(f"/resume-splits/{uuid4()}").status_code == 404
    monkeypatch.setattr(api_client, "get_resume_split", _down)
    assert client.get(f"/resume-splits/{uuid4()}").status_code == 503


# ------------------------------------------------------ review: proposed page


def test_proposed_page_has_one_row_per_applicant_prefilled_plus_two_spares(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    _install(
        monkeypatch,
        _view(
            split_id,
            applicants=[_applicant([1, 2, 5], [3]), _applicant([6], [])],
            ignored=[8],
        ),
    )

    code, body = _get(client, split_id)

    assert code == 200
    form = _form_for(body, f"/resume-splits/{split_id}/correct")
    assert form is not None
    f = _inputs(form)
    assert f["resume_pages_1"]["value"] == "1-2,5"
    assert f["cover_pages_1"]["value"] == "3"
    assert f["resume_pages_2"]["value"] == "6"
    assert f["cover_pages_2"].get("value", "") == ""
    for spare in (3, 4):
        assert f[f"resume_pages_{spare}"].get("value", "") == ""
        assert f[f"cover_pages_{spare}"].get("value", "") == ""
    assert "resume_pages_5" not in f
    assert f["ignored_pages"]["value"] == "8"
    assert f["proposal_version"]["type"] == "hidden"
    assert f["proposal_version"]["value"] == "3"
    assert f["csrf_token"]["value"]
    assert "Applicant 1" in body and "Applicant 2" in body


def test_page_lists_use_the_range_grammar(monkeypatch: Any, client: Any) -> None:
    split_id = uuid4()
    _install(
        monkeypatch,
        _view(
            split_id,
            applicants=[_applicant([1, 2, 5], [7, 8, 9])],
            ignored=[3, 4],
        ),
    )

    _, body = _get(client, split_id)

    f = _inputs(_form_for(body, "/correct") or {"inputs": []})
    assert f["resume_pages_1"]["value"] == "1-2,5"
    assert f["cover_pages_1"]["value"] == "7-9"
    assert f["ignored_pages"]["value"] == "3-4"


def test_accept_and_discard_are_their_own_csrf_protected_forms(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    _install(monkeypatch, _view(split_id, version=6))

    _, body = _get(client, split_id)

    accept = _form_for(body, f"/resume-splits/{split_id}/accept")
    discard = _form_for(body, f"/resume-splits/{split_id}/discard")
    correct = _form_for(body, f"/resume-splits/{split_id}/correct")
    assert accept and discard and correct
    for form in (accept, discard, correct):
        assert form["attrs"]["method"].lower() == "post"
        assert _inputs(form)["csrf_token"]["value"]
    assert _inputs(accept)["proposal_version"]["value"] == "6"
    assert "Save correction" in " ".join(b["text"] for b in correct["buttons"])
    assert "Accept" in " ".join(b["text"] for b in accept["buttons"])
    assert "Discard" in " ".join(b["text"] for b in discard["buttons"])


def test_review_page_is_free_of_client_side_behaviour(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    _install(monkeypatch, _view(split_id))

    _, body = _get(client, split_id)

    low = body.lower()
    assert "hx-" not in low
    assert "onclick" not in low and "onchange" not in low
    assert "javascript:" not in low


def test_candidate_name_is_shown_only_when_the_api_returns_one(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    _install(
        monkeypatch,
        _view(
            split_id,
            applicants=[
                _applicant([1, 2], name="Jane Roe"),
                _applicant([3, 4]),
            ],
        ),
    )

    _, body = _get(client, split_id)

    assert "Jane Roe" in body
    assert "Applicant 1" in body and "Applicant 2" in body


def test_blind_review_never_renders_a_name_even_if_one_slips_through(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    view = _view(
        split_id,
        blind=True,
        applicants=[_applicant([1, 2], name="Jane Roe"), _applicant([3, 4])],
    )
    # Simulate an API regression: the name leaks into a blind-review payload.
    view["proposal"]["applicants"][0]["candidate_name"] = "Jane Roe"
    view["source_filename"] = "Jane Roe resume export.pdf"
    _install(monkeypatch, view)

    _, body = _get(client, split_id)

    assert "Jane" not in body and "Roe" not in body
    assert "Applicant 1" in body


def test_blind_review_page_shows_no_filename(monkeypatch: Any, client: Any) -> None:
    split_id = uuid4()
    view = _view(split_id, blind=True)
    view["source_filename"] = "Jane Roe export.pdf"
    _install(monkeypatch, view)

    _, body = _get(client, split_id)

    assert "Jane Roe export.pdf" not in body


# ------------------------------------------------- review: banner, diagnostics


def test_heuristic_method_shows_the_fallback_banner(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    _install(
        monkeypatch,
        _view(split_id, method="heuristic", fallback_reason="llm_unavailable"),
    )

    _, body = _get(client, split_id)

    low = body.lower()
    assert "fallback" in low
    assert "model" in low
    assert "check every" in low or "review" in low


def test_llm_method_has_no_fallback_banner(monkeypatch: Any, client: Any) -> None:
    split_id = uuid4()
    _install(monkeypatch, _view(split_id, method="llm"))

    _, body = _get(client, split_id)

    assert "fallback" not in body.lower()


def test_clean_proposal_shows_no_error_and_allows_accept(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    _install(monkeypatch, _view(split_id))

    _, body = _get(client, split_id)

    assert re.search(r'class="[^"]*\berror\b', body) is None
    assert _form_for(body, "/accept") is not None
    accept = _form_for(body, "/accept") or {"buttons": []}
    assert not any("disabled" in b["attrs"] for b in accept["buttons"])


@pytest.mark.parametrize(
    ("diag", "needles"),
    [
        (ProposalDiagnostics(missing=[7, 9]), ("missing", "7", "9")),
        (ProposalDiagnostics(duplicated=[4]), ("twice|more than once|duplicate", "4")),
        (ProposalDiagnostics(merged=[2]), ("Applicant 2",)),
        (ProposalDiagnostics(misfiled_cover=[5]), ("cover", "5")),
    ],
    ids=["missing", "duplicated", "merged", "misfiled_cover"],
)
def test_blocking_findings_are_errors_and_accept_is_unavailable(
    monkeypatch: Any,
    client: Any,
    diag: ProposalDiagnostics,
    needles: tuple[str, ...],
) -> None:
    split_id = uuid4()
    _install(monkeypatch, _view(split_id, diagnostics=diag))

    _, body = _get(client, split_id)

    assert re.search(r'class="[^"]*\berror\b', body), "no error-styled message"
    low = body.lower()
    for needle in needles:
        assert any(alt.lower() in low for alt in needle.split("|")), needle
    accept = _form_for(body, "/accept")
    if accept is not None:
        assert all("disabled" in b["attrs"] for b in accept["buttons"])
    assert re.search(r"cannot|can't|blocked|fix", low)
    # Saving a correction must stay possible, or the manager is stuck.
    assert _form_for(body, "/correct") is not None
    assert _form_for(body, "/discard") is not None


@pytest.mark.parametrize(
    ("diag", "applicant"),
    [
        (ProposalDiagnostics(cover_only=[3]), "Applicant 3"),
        (ProposalDiagnostics(low_text=[2]), "Applicant 2"),
    ],
    ids=["cover_only", "low_text"],
)
def test_excluded_applicants_are_disclosed_but_do_not_block(
    monkeypatch: Any, client: Any, diag: ProposalDiagnostics, applicant: str
) -> None:
    split_id = uuid4()
    _install(monkeypatch, _view(split_id, diagnostics=diag))

    _, body = _get(client, split_id)

    assert re.search(
        rf"{applicant}[^<]*will not be imported", body, re.IGNORECASE
    ), "exclusion not disclosed next to the applicant"
    accept = _form_for(body, "/accept")
    assert accept is not None
    assert not any("disabled" in b["attrs"] for b in accept["buttons"])


# ------------------------------------------------------------ correct route


def _correct_form(**rows: str) -> dict[str, str]:
    data = {"proposal_version": "3", "ignored_pages": ""}
    data.update(rows)
    return data


def test_correct_parses_ranges_into_the_api_json_and_sends_the_version(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    spy = MagicMock(return_value=_view(split_id, version=4))
    monkeypatch.setattr(api_client, "correct_resume_split", spy)

    resp = client.post(
        f"/resume-splits/{split_id}/correct",
        data=_correct_form(
            resume_pages_1="1-2,5",
            cover_pages_1="3",
            resume_pages_2=" 6 - 7 , 9 ",
            cover_pages_2="",
            resume_pages_3="",
            cover_pages_3="",
            resume_pages_4="",
            cover_pages_4="",
            ignored_pages="8, 10-11",
        ),
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/resume-splits/{split_id}")
    spy.assert_called_once()
    assert spy.call_args.args[0] == split_id
    kw = spy.call_args.kwargs
    assert kw["proposal_version"] == 3
    assert [(a["resume_pages"], a["cover_letter_pages"]) for a in kw["applicants"]] == [
        ([1, 2, 5], [3]),
        ([6, 7, 9], []),
    ]
    assert kw["ignored_pages"] == [8, 10, 11]


def test_correct_drops_blank_rows_and_keeps_cover_only_rows(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    spy = MagicMock(return_value=_view(split_id))
    monkeypatch.setattr(api_client, "correct_resume_split", spy)

    client.post(
        f"/resume-splits/{split_id}/correct",
        data=_correct_form(
            resume_pages_1="1-2",
            cover_pages_1="",
            resume_pages_2="",
            cover_pages_2="",
            resume_pages_3="",
            cover_pages_3="4",
            resume_pages_4="5",
            cover_pages_4="6",
        ),
    )

    kw = spy.call_args.kwargs
    assert [(a["resume_pages"], a["cover_letter_pages"]) for a in kw["applicants"]] == [
        ([1, 2], []),
        ([], [4]),
        ([5], [6]),
    ]


@pytest.mark.parametrize("bad", ["abc", "3-1", "0", "1-", "-2", "1;2"])
def test_correct_rejects_a_malformed_page_list_without_calling_the_backend(
    monkeypatch: Any, client: Any, bad: str
) -> None:
    split_id = uuid4()
    spy = MagicMock()
    monkeypatch.setattr(api_client, "correct_resume_split", spy)
    _install(monkeypatch, _view(split_id))

    resp = client.post(
        f"/resume-splits/{split_id}/correct",
        data=_correct_form(resume_pages_1=bad, cover_pages_1=""),
    )

    assert resp.status_code in (400, 422)
    assert "page" in resp.get_data(as_text=True).lower()
    spy.assert_not_called()


def test_correct_rejects_a_non_numeric_proposal_version(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    spy = MagicMock()
    monkeypatch.setattr(api_client, "correct_resume_split", spy)
    _install(monkeypatch, _view(split_id))

    resp = client.post(
        f"/resume-splits/{split_id}/correct",
        data={"proposal_version": "x", "resume_pages_1": "1"},
    )

    assert resp.status_code in (400, 422)
    spy.assert_not_called()


def test_correct_409_rerenders_with_someone_else_changed_this(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()

    def _stale(*a: Any, **k: Any) -> Any:
        raise api_client.Conflict(
            "x",
            status_code=409,
            detail={"code": "resource.conflict", "message": "stale"},
        )

    monkeypatch.setattr(api_client, "correct_resume_split", _stale)
    _install(monkeypatch, _view(split_id, version=5))

    resp = client.post(
        f"/resume-splits/{split_id}/correct",
        data=_correct_form(resume_pages_1="1-2"),
    )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 409
    assert "someone else changed this" in body.lower()
    # Re-rendered from the CURRENT proposal, carrying the new version.
    f = _inputs(_form_for(body, "/correct") or {"inputs": []})
    assert f["proposal_version"]["value"] == "5"


def test_correct_422_shows_the_api_error(monkeypatch: Any, client: Any) -> None:
    split_id = uuid4()

    def _bad(*a: Any, **k: Any) -> Any:
        raise api_client.BadRequest(
            "x",
            status_code=422,
            detail={"detail": "pages must be within 1..8"},
        )

    monkeypatch.setattr(api_client, "correct_resume_split", _bad)
    _install(monkeypatch, _view(split_id))

    resp = client.post(
        f"/resume-splits/{split_id}/correct",
        data=_correct_form(resume_pages_1="99"),
    )

    assert resp.status_code in (400, 422)
    assert "pages must be within 1..8" in resp.get_data(as_text=True)


# ------------------------------------------------------------- accept route


def _results(*, accepted: int, rejected: int) -> list[dict[str, Any]]:
    rows = [
        ResumeUploadResult(
            original_filename=f"applicant-{i:03d}_resume.pdf",
            outcome="accepted",
            resume_id=uuid4(),
        )
        for i in range(1, accepted + 1)
    ]
    rows += [
        ResumeUploadResult(
            original_filename=f"applicant-{accepted + i:03d}_cover_letter.pdf",
            outcome="rejected",
            reason="applicant has only cover-letter pages; not ingested",
        )
        for i in range(1, rejected + 1)
    ]
    return [r.model_dump(mode="json") for r in rows]


def test_accept_posts_the_version_flashes_counts_and_redirects_to_the_job(
    monkeypatch: Any, client: Any
) -> None:
    split_id, job_id = uuid4(), uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    monkeypatch.setattr(
        api_client,
        "get_resume_split",
        MagicMock(return_value=_view(split_id, job_id=job_id)),
    )
    spy = MagicMock(return_value=_results(accepted=2, rejected=1))
    monkeypatch.setattr(api_client, "accept_resume_split", spy)

    resp = client.post(
        f"/resume-splits/{split_id}/accept", data={"proposal_version": "3"}
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    spy.assert_called_once()
    assert spy.call_args.args[0] == split_id
    assert spy.call_args.kwargs["proposal_version"] == 3
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert re.search(r"2\s+(accepted|imported)", page, re.IGNORECASE), page[:600]
    assert re.search(r"1\s+rejected", page, re.IGNORECASE)


def test_accept_flash_on_a_blind_job_carries_no_candidate_name(
    monkeypatch: Any, client: Any
) -> None:
    split_id, job_id = uuid4(), uuid4()
    monkeypatch.setattr(
        api_client, "get_job", lambda jid, **kw: _job(job_id, blind=True)
    )
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    monkeypatch.setattr(
        api_client,
        "get_resume_split",
        MagicMock(return_value=_view(split_id, job_id=job_id, blind=True)),
    )
    rows = _results(accepted=1, rejected=1)
    # Even if a result row somehow carried a name, the flash must not repeat it.
    rows[0]["original_filename"] = "Jane Roe resume.pdf"
    rows[0]["cover_letter_filename"] = "Jane Roe cover.pdf"
    monkeypatch.setattr(api_client, "accept_resume_split", MagicMock(return_value=rows))

    resp = client.post(
        f"/resume-splits/{split_id}/accept", data={"proposal_version": "3"}
    )
    page = client.get(resp.headers["Location"]).get_data(as_text=True)

    assert "Jane" not in resp.headers["Location"]
    assert "Jane" not in page and "Roe" not in page


def test_accept_409_rerenders_with_the_reason(monkeypatch: Any, client: Any) -> None:
    split_id = uuid4()

    def _blocked(*a: Any, **k: Any) -> Any:
        raise api_client.Conflict(
            "x",
            status_code=409,
            detail={
                "code": "resource.conflict",
                "message": "the proposal has missing, duplicated, merged or "
                "misfiled pages",
                "missing": [7],
            },
        )

    monkeypatch.setattr(api_client, "accept_resume_split", _blocked)
    _install(monkeypatch, _view(split_id))

    resp = client.post(
        f"/resume-splits/{split_id}/accept", data={"proposal_version": "3"}
    )

    body = resp.get_data(as_text=True)
    assert resp.status_code == 409
    assert "missing, duplicated, merged or misfiled pages" in body
    assert _form_for(body, "/correct") is not None


def test_accept_rejects_a_missing_version_without_calling_the_backend(
    monkeypatch: Any, client: Any
) -> None:
    split_id = uuid4()
    spy = MagicMock()
    monkeypatch.setattr(api_client, "accept_resume_split", spy)
    _install(monkeypatch, _view(split_id))

    resp = client.post(f"/resume-splits/{split_id}/accept", data={})

    assert resp.status_code in (400, 422)
    spy.assert_not_called()


# ------------------------------------------------------------ discard route


def test_discard_posts_and_redirects_to_the_job(monkeypatch: Any, client: Any) -> None:
    split_id, job_id = uuid4(), uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume_split",
        MagicMock(return_value=_view(split_id, job_id=job_id)),
    )
    spy = MagicMock(return_value={"id": str(split_id), "status": "discarded"})
    monkeypatch.setattr(api_client, "discard_resume_split", spy)

    resp = client.post(f"/resume-splits/{split_id}/discard")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    spy.assert_called_once()
    assert spy.call_args.args[0] == split_id


def test_discard_409_does_not_500(monkeypatch: Any, client: Any) -> None:
    split_id = uuid4()

    def _conflict(*a: Any, **k: Any) -> Any:
        raise api_client.Conflict(
            "x",
            status_code=409,
            detail={"code": "resource.conflict", "message": "already accepted"},
        )

    monkeypatch.setattr(api_client, "discard_resume_split", _conflict)
    _install(monkeypatch, _view(split_id, status="accepted"))

    resp = client.post(f"/resume-splits/{split_id}/discard")

    assert resp.status_code == 409
    assert "already accepted" in resp.get_data(as_text=True)


# ------------------------------------------------------------ CSRF + access


_POST_ROUTES = [
    ("/jobs/{jid}/resumes/split-upload", "split_upload"),
    ("/resume-splits/{sid}/correct", "correct_resume_split"),
    ("/resume-splits/{sid}/accept", "accept_resume_split"),
    ("/resume-splits/{sid}/discard", "discard_resume_split"),
]


@pytest.mark.parametrize(("template", "fn"), _POST_ROUTES)
def test_every_new_post_route_rejects_a_missing_csrf_token(
    monkeypatch: Any, plain_client: Any, template: str, fn: str
) -> None:
    spy = MagicMock()
    monkeypatch.setattr(api_client, fn, spy)
    url = template.format(jid=uuid4(), sid=uuid4())

    resp = plain_client.post(
        url,
        data={"proposal_version": "3", "consent_acknowledged": "true"},
    )

    assert resp.status_code == 403
    spy.assert_not_called()


@pytest.mark.parametrize(("template", "fn"), _POST_ROUTES)
def test_every_new_post_route_rejects_a_wrong_csrf_token(
    monkeypatch: Any, plain_client: Any, template: str, fn: str
) -> None:
    spy = MagicMock()
    monkeypatch.setattr(api_client, fn, spy)
    with plain_client.session_transaction() as session:
        session["_csrf_page_token"] = "the-real-token"
    url = template.format(jid=uuid4(), sid=uuid4())

    resp = plain_client.post(
        url,
        data={
            "csrf_token": "forged",
            "proposal_version": "3",
            "consent_acknowledged": "true",
        },
    )

    assert resp.status_code == 403
    spy.assert_not_called()


def test_token_rendered_on_the_review_page_opens_its_forms(
    monkeypatch: Any, plain_client: Any
) -> None:
    """Round trip with NO header shortcut: the hidden field alone must pass,
    and re-posting the same rendered form must still pass (reusable)."""
    split_id, job_id = uuid4(), uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume_split",
        MagicMock(return_value=_view(split_id, job_id=job_id)),
    )
    spy = MagicMock(return_value={"id": str(split_id), "status": "discarded"})
    monkeypatch.setattr(api_client, "discard_resume_split", spy)
    body = plain_client.get(f"/resume-splits/{split_id}").get_data(as_text=True)
    token = _inputs(_form_for(body, "/discard") or {"inputs": []})["csrf_token"][
        "value"
    ]

    for _ in range(2):
        resp = plain_client.post(
            f"/resume-splits/{split_id}/discard", data={"csrf_token": token}
        )
        assert resp.status_code == 302
    assert spy.call_count == 2


def test_cross_origin_post_is_rejected_even_with_a_valid_token(
    monkeypatch: Any, client: Any
) -> None:
    spy = MagicMock()
    monkeypatch.setattr(api_client, "discard_resume_split", spy)

    resp = client.post(
        f"/resume-splits/{uuid4()}/discard",
        headers={"Origin": "https://evil.example"},
    )

    assert resp.status_code == 403
    spy.assert_not_called()


def _cas(monkeypatch: Any, status: dict[str, Any]) -> None:
    settings = Settings(cas_enabled=True, cas_service_base_url="http://ra.test")
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(api_client, "get_cas_user", MagicMock(return_value=status))


def test_review_and_post_routes_require_login(monkeypatch: Any, client: Any) -> None:
    _cas(monkeypatch, {"authenticated": False, "username": None})
    guard = MagicMock(side_effect=AssertionError("must not reach the backend"))
    for fn in (
        "get_resume_split",
        "split_upload",
        "correct_resume_split",
        "accept_resume_split",
        "discard_resume_split",
    ):
        monkeypatch.setattr(api_client, fn, guard)
    sid, jid = uuid4(), uuid4()

    responses = [
        client.get(f"/resume-splits/{sid}"),
        client.post(f"/resume-splits/{sid}/correct", data={"proposal_version": "1"}),
        client.post(f"/resume-splits/{sid}/accept", data={"proposal_version": "1"}),
        client.post(f"/resume-splits/{sid}/discard"),
        client.post(
            f"/jobs/{jid}/resumes/split-upload",
            data={"consent_acknowledged": "true"},
            content_type="multipart/form-data",
        ),
    ]

    for resp in responses:
        assert resp.status_code == 302
        assert "/auth/cas/login" in resp.headers["Location"]


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_backend_403_for_a_non_writer_is_handled_not_a_500(
    monkeypatch: Any, client: Any, role: str
) -> None:
    """The backend gates every one of these on admin/recruiter (same as résumé
    upload); Flask must turn that 403 into a page, never an unhandled error."""
    _cas(
        monkeypatch,
        {"authenticated": True, "username": "jordan", "role": role},
    )

    def _forbidden(*a: Any, **k: Any) -> Any:
        raise api_client.BadRequest(
            "backend 403",
            status_code=403,
            detail={"detail": "session role not permitted for this route"},
        )

    for fn in (
        "get_resume_split",
        "split_upload",
        "correct_resume_split",
        "accept_resume_split",
        "discard_resume_split",
    ):
        monkeypatch.setattr(api_client, fn, _forbidden)
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(jid))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    sid, jid = uuid4(), uuid4()

    responses = [
        client.get(f"/resume-splits/{sid}"),
        client.post(f"/resume-splits/{sid}/correct", data={"proposal_version": "1"}),
        client.post(f"/resume-splits/{sid}/accept", data={"proposal_version": "1"}),
        client.post(f"/resume-splits/{sid}/discard"),
        client.post(
            f"/jobs/{jid}/resumes/split-upload",
            data={"files": [_pdf_part()], "consent_acknowledged": "true"},
            content_type="multipart/form-data",
        ),
    ]

    for resp in responses:
        assert resp.status_code < 500
