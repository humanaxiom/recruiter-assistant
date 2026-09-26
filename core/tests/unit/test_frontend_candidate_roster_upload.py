"""RED — Sponsor requirements PR2 slice 3, the job-page UI for the Taleo
candidate-roster import.

The backend route (``POST /api/v1/jobs/{job_id}/candidate-roster``) and its
service already exist and are GREEN (``test_route_candidate_roster.py`` /
``test_candidate_roster_reconciliation.py``, slice 2). Nothing on the Flask
side calls it yet — no ``api_client`` function, no route, no form on
``job_detail.html``. This file pins all three.

**The one hard requirement on the response copy**: the internal-status uplift
(``test_internal_uplift_scoring.py``) is applied at RANK time, unlike the
read-time work-authorization band, so a roster ingested after a shortlist
already exists is invisible on that shortlist until it is regenerated. The
recruiter must be told this in plain language on every successful import —
not only when the report shows a change, and not via any attempt at
stale-shortlist detection (out of scope; a plain, always-correct sentence is
the requirement).

Every ``RosterReconciliationReport`` fixture here is built through the real
DTO (``src.services.candidate_roster_service.RosterReconciliationReport``,
dumped in JSON mode), mirroring ``test_route_candidate_roster.py``'s own
``_dummy_report`` — never a hand-written dict — since the frontend reads
this the same way it reads every other API body: as JSON over HTTP.
"""

from __future__ import annotations

import io
import re
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


@pytest.fixture
def plain_client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _job(job_id: Any, *, status: str = "open") -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": status,
        "blind_review": True,
        "parsed_at": "2026-07-17T00:00:00Z",
        "description_parsed": {"required_skills": []},
    }


def _report(**over: Any) -> dict[str, Any]:
    from src.services.candidate_roster_service import RosterReconciliationReport

    base: dict[str, Any] = dict(
        matched=11,
        work_authorization_changed=2,
        work_authorization_unchanged=9,
        internal_apsa_changed=3,
        internal_apsa_unchanged=8,
        internal_cupe_changed=0,
        internal_cupe_unchanged=11,
        unmatched_csv_rows=[4, 17],
        unmatched_resumes=[],
        ambiguous_name_matches=[],
        conflicting=[],
        unrecognised_work_authorization_source=[],
    )
    base.update(over)
    return RosterReconciliationReport(**base).model_dump(mode="json")


_STALE_SENTENCE_SUBSTRINGS = ("shortlist", "regenerat")


def _assert_stale_shortlist_sentence(messages_text: str) -> None:
    lowered = messages_text.lower()
    assert all(s in lowered for s in _STALE_SENTENCE_SUBSTRINGS), (
        "the roster-import result must tell the recruiter that an existing "
        "shortlist does not reflect this update until it is regenerated -- "
        f"got: {messages_text!r}"
    )


def _file_part() -> tuple[io.BytesIO, str]:
    return (io.BytesIO(b"Name,Email\nA,a@x.test\n"), "roster.csv")


# --------------------------------------------------------------- the form


def test_the_roster_upload_form_renders_on_the_job_page_for_a_writer(
    monkeypatch: Any, plain_client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])

    body = plain_client.get(f"/jobs/{job_id}").get_data(as_text=True)

    assert f'action="/jobs/{job_id}/candidate-roster"' in body
    assert 'enctype="multipart/form-data"' in body
    assert 'type="file"' in body


# ------------------------------------------------------------- the happy path


def test_uploading_a_roster_shows_the_reconciliation_counts_and_redirects(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    spy = MagicMock(return_value=_report())
    monkeypatch.setattr(api_client, "upload_candidate_roster", spy)

    resp = client.post(
        f"/jobs/{job_id}/candidate-roster",
        data={"file": _file_part()},
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    spy.assert_called_once()
    assert spy.call_args.args[0] == job_id

    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert re.search(r"11\s+matched", page, re.IGNORECASE), page[:800]
    _assert_stale_shortlist_sentence(page)


def test_the_stale_shortlist_sentence_appears_even_with_zero_changes(
    monkeypatch: Any, client: Any
) -> None:
    """Not conditional on any detected change -- a plain, always-correct
    sentence, exactly as specified (no stale-shortlist DETECTION)."""
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    zero_report = _report(
        matched=0,
        work_authorization_changed=0,
        work_authorization_unchanged=0,
        internal_apsa_changed=0,
        internal_apsa_unchanged=0,
        internal_cupe_changed=0,
        internal_cupe_unchanged=0,
        unmatched_csv_rows=[],
    )
    monkeypatch.setattr(
        api_client, "upload_candidate_roster", MagicMock(return_value=zero_report)
    )

    resp = client.post(
        f"/jobs/{job_id}/candidate-roster",
        data={"file": _file_part()},
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    page = client.get(resp.headers["Location"]).get_data(as_text=True)
    _assert_stale_shortlist_sentence(page)


# ------------------------------------------------------------------ error paths


def test_no_file_selected_re_renders_with_an_error_before_calling_the_backend(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    spy = MagicMock()
    monkeypatch.setattr(api_client, "upload_candidate_roster", spy)

    resp = client.post(
        f"/jobs/{job_id}/candidate-roster",
        data={},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    spy.assert_not_called()


def test_a_backend_bad_request_is_shown_as_a_friendly_error(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise api_client.BadRequest(
            "manifest too large", status_code=422, detail="manifest too large"
        )

    monkeypatch.setattr(api_client, "upload_candidate_roster", _raise)

    resp = client.post(
        f"/jobs/{job_id}/candidate-roster",
        data={"file": _file_part()},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert "manifest too large" in resp.get_data(as_text=True)


def test_missing_csrf_token_is_rejected_before_the_backend(
    monkeypatch: Any, plain_client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock()
    monkeypatch.setattr(api_client, "upload_candidate_roster", spy)

    resp = plain_client.post(
        f"/jobs/{job_id}/candidate-roster",
        data={"file": _file_part()},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 403
    spy.assert_not_called()
