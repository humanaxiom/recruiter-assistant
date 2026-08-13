"""Slice S3 — job detail: parse-poll, status transitions, blind toggle.

Covers ``api_client.transition_status`` / ``api_client.patch_job`` (via
``httpx.MockTransport``, incl. the 409 illegal-transition path mapped to a
typed ``Conflict``) and the Flask job-detail routes: the HTMX ``parse-status``
poll fragment (which drops its ``hx-trigger`` once parsing completes so polling
stops), the legal-next-state status buttons (draft→open disabled until the LLM
finishes parsing), and the blind-review toggle (which sends ONLY
``{"blind_review": <bool>}``).

**FU-8/ADR-026 — per-job résumé status-breakdown widget (added below).**
ADR-026 decision 5 asks for a per-job résumé-status breakdown
(uploaded/parsing/parsed/failed/withdrawn) on the job-detail page, driven by
``api_client.get_resume_status_breakdown(job_id)`` (``GET
/jobs/{id}/resume-status`` on the backend). This is wired as an HTMX
fragment — mirroring the EXISTING ``parse-status``/``resumes-table`` poll-
fragment pattern in this same file/module — rather than baked into
``_render_job_detail``'s synchronous path: the main ``GET /jobs/<id>`` page
only renders a lazy-loading placeholder (``hx-get`` pointing at the new
fragment route), so it never itself calls ``get_resume_status_breakdown``.
This is deliberate: dozens of EXISTING tests across this file and others
(``test_frontend_resume_upload.py``, ``test_frontend_shortlist.py``, etc.)
already hit ``GET /jobs/<id>`` (or its error-path re-renders) without
mocking a breakdown call, and baking the call into the synchronous render
path would force every one of them to be touched just to keep working —
which is out of scope for an additive slice and against this repo's
"never modify existing tests to make an unrelated implementation pass" rule.
The fragment route is exercised directly below.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client


@pytest.fixture
def client(csrf_client: Any) -> Any:
    """The shared CSRF-carrying browser client (Phase 1.3).

    These tests exercise the route's BUSINESS logic and predate the
    anti-forgery guard; they now present a page token the way a real browser
    does, rather than the guard being relaxed for them. See
    ``tests/unit/conftest.py`` for why this is not autouse."""
    return csrf_client


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _job(
    job_id: Any,
    *,
    status: str = "draft",
    parsed_at: str | None = None,
    description_parsed: Any = None,
) -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": status,
        "blind_review": True,
        "parsed_at": parsed_at,
        "description_parsed": description_parsed,
    }


def _breakdown(
    *,
    uploaded: int = 0,
    parsing: int = 0,
    parsed: int = 0,
    failed: int = 0,
    withdrawn: int = 0,
    degraded: int = 0,
) -> dict[str, int]:
    return {
        "uploaded": uploaded,
        "parsing": parsing,
        "parsed": parsed,
        "failed": failed,
        "withdrawn": withdrawn,
        "degraded": degraded,
    }


# ── api_client.transition_status ─────────────────────────────────────────


def test_transition_status_patches_status_with_to_body() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        captured["method"] = request.method
        captured["body"] = request.content
        return httpx.Response(200, json={"id": str(job_id), "status": "open"})

    result = api_client.transition_status(job_id, "open", client=_client_with(handler))
    assert captured["method"] == "PATCH"
    assert captured["url"].path == f"/jobs/{job_id}/status"
    assert b'"to"' in captured["body"]
    assert b"open" in captured["body"]
    assert result == {"id": str(job_id), "status": "open"}


def test_transition_status_raises_conflict_on_409() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "illegal transition"})

    with pytest.raises(api_client.Conflict) as exc:
        api_client.transition_status(uuid4(), "closed", client=_client_with(handler))
    assert exc.value.status_code == 409


def test_conflict_is_a_backend_error() -> None:
    assert issubclass(api_client.Conflict, api_client.BackendError)


# ── api_client.patch_job ─────────────────────────────────────────────────


def test_patch_job_patches_with_payload() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        captured["method"] = request.method
        captured["body"] = request.content
        return httpx.Response(200, json={"id": str(job_id), "blind_review": False})

    result = api_client.patch_job(
        job_id, {"blind_review": False}, client=_client_with(handler)
    )
    assert captured["method"] == "PATCH"
    assert captured["url"].path == f"/jobs/{job_id}"
    assert b"blind_review" in captured["body"]
    assert result == {"id": str(job_id), "blind_review": False}


def test_patch_job_raises_conflict_on_409() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "nope"})

    with pytest.raises(api_client.Conflict):
        api_client.patch_job(
            uuid4(), {"blind_review": True}, client=_client_with(handler)
        )


# ── GET /jobs/<id>/parse-status — the poll fragment ──────────────────────


def test_parse_status_fragment_shows_parsing_and_polls_while_unparsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_job", MagicMock(return_value=_job(job_id, parsed_at=None))
    )
    resp = client.get(f"/jobs/{job_id}/parse-status")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "parsing" in body.lower()
    assert "hx-trigger" in body  # keeps polling


def test_parse_status_fragment_shows_skill_pills_and_stops_polling_when_parsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    parsed = {
        "title": "Senior Backend Engineer",
        "required_skills": [
            {"name": "PostgreSQL", "min_years": 3},
            {"name": "Kubernetes", "min_years": 2},
        ],
    }
    monkeypatch.setattr(
        api_client,
        "get_job",
        MagicMock(
            return_value=_job(
                job_id, parsed_at="2026-07-17T00:00:00Z", description_parsed=parsed
            )
        ),
    )
    resp = client.get(f"/jobs/{job_id}/parse-status")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "PostgreSQL" in body
    assert "Kubernetes" in body
    assert "hx-trigger" not in body  # polling stopped


# ── GET /jobs/<id> — draft→open button gating ────────────────────────────


def test_job_detail_open_button_disabled_while_unparsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_job",
        MagicMock(return_value=_job(job_id, status="draft", parsed_at=None)),
    )
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "disabled" in body
    assert "Wait for the LLM" in body


def test_job_detail_open_button_enabled_once_parsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_job",
        MagicMock(
            return_value=_job(
                job_id,
                status="draft",
                parsed_at="2026-07-17T00:00:00Z",
                description_parsed={"required_skills": []},
            )
        ),
    )
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    # The Open-for-applicants button exists and is NOT disabled.
    assert "Open for applicants" in body
    # No disabled attribute attached to the open button (nothing else is
    # disabled on the page), so a bare `disabled` must be absent.
    assert "disabled" not in body


def test_open_job_upload_form_shows_parse_time_hint(
    monkeypatch: Any, client: Any
) -> None:
    """The upload form warns that parsing on the local model takes a minute or
    two per large PDF, so the wait isn't mistaken for a hang."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_job", MagicMock(return_value=_job(job_id, status="open"))
    )
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "minute" in body.lower()


def test_job_detail_only_shows_legal_next_states(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_job",
        MagicMock(return_value=_job(job_id, status="closed", parsed_at="x")),
    )
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    # closed → only archived is legal
    assert 'value="archived"' in body
    assert 'value="open"' not in body
    assert 'value="closed"' not in body


# ── POST /jobs/<id>/status ───────────────────────────────────────────────


def test_post_status_proxies_and_redirects(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value={"id": str(job_id), "status": "open"})
    monkeypatch.setattr(api_client, "transition_status", spy)
    resp = client.post(f"/jobs/{job_id}/status", data={"to": "open"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    assert spy.call_args.args[0] == job_id
    assert spy.call_args.args[1] == "open"


def test_post_status_conflict_surfaces_message_not_500(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "transition_status",
        MagicMock(
            side_effect=api_client.Conflict(
                "bad", status_code=409, detail={"detail": "illegal transition"}
            )
        ),
    )
    monkeypatch.setattr(
        api_client, "get_job", MagicMock(return_value=_job(job_id, status="draft"))
    )
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.post(f"/jobs/{job_id}/status", data={"to": "closed"})
    assert resp.status_code != 500
    assert b"transition" in resp.data or b"llegal" in resp.data


# ── POST /jobs/<id>/blind-review ─────────────────────────────────────────


def test_post_blind_review_sends_only_the_blind_review_bool_true(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value={"id": str(job_id), "blind_review": True})
    monkeypatch.setattr(api_client, "patch_job", spy)
    resp = client.post(f"/jobs/{job_id}/blind-review", data={"blind_review": "true"})
    assert resp.status_code == 302
    assert spy.call_args.args[0] == job_id
    assert spy.call_args.args[1] == {"blind_review": True}


def test_post_blind_review_sends_only_the_blind_review_bool_false(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value={"id": str(job_id), "blind_review": False})
    monkeypatch.setattr(api_client, "patch_job", spy)
    client.post(f"/jobs/{job_id}/blind-review", data={"blind_review": "false"})
    assert spy.call_args.args[1] == {"blind_review": False}


def test_post_blind_review_backend_unavailable_not_500(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "patch_job",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    resp = client.post(f"/jobs/{job_id}/blind-review", data={"blind_review": "true"})
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500


# ── FU-8/ADR-026 — per-job résumé status-breakdown widget ─────────────────


def test_job_detail_page_includes_a_lazy_loading_status_breakdown_placeholder(
    monkeypatch: Any, client: Any
) -> None:
    """The main job-detail render must NOT itself call
    ``get_resume_status_breakdown`` — it only renders a placeholder that
    ``hx-get``s the fragment route below. Deliberately does NOT mock
    ``get_resume_status_breakdown`` here, to prove the main render never
    reaches it (see the module docstring for why)."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_job", MagicMock(return_value=_job(job_id, status="open"))
    )
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "hx-get" in body
    assert f"/jobs/{job_id}/resume-status" in body


def test_resume_status_widget_route_shows_all_five_bucket_counts(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume_status_breakdown",
        MagicMock(
            return_value=_breakdown(
                uploaded=2, parsing=1, parsed=5, failed=1, withdrawn=3
            )
        ),
    )
    resp = client.get(f"/jobs/{job_id}/resume-status")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True).lower()
    for label in ("uploaded", "parsing", "parsed", "failed", "withdrawn"):
        assert label in body
    for count in ("2", "1", "5", "3"):
        assert count in body


def test_resume_status_widget_route_renders_all_zero_without_crashing(
    monkeypatch: Any, client: Any
) -> None:
    """ADR-026 decision 5's explicit acceptance case: a job with no résumés
    at all must render a clean all-zero breakdown, not crash or omit a
    bucket."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume_status_breakdown", MagicMock(return_value=_breakdown())
    )
    resp = client.get(f"/jobs/{job_id}/resume-status")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True).lower()
    assert "withdrawn" in body
    assert "uploaded" in body


def test_resume_status_widget_route_shows_degraded_sub_count(
    monkeypatch: Any, client: Any
) -> None:
    """FU-7 §4 / ADR-030: the widget must show how many of the ``parsed``
    résumés fell back to keyword-scan skills -- e.g. "N parsed (M
    degraded)". ``degraded`` is a SUB-count, so it renders alongside
    ``parsed``, never in place of it."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume_status_breakdown",
        MagicMock(return_value=_breakdown(parsed=5, degraded=2)),
    )
    resp = client.get(f"/jobs/{job_id}/resume-status")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True).lower()
    assert "degraded" in body
    assert "parsed" in body


def test_resume_status_widget_route_zero_degraded_still_renders_the_word(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume_status_breakdown",
        MagicMock(return_value=_breakdown(parsed=5, degraded=0)),
    )
    resp = client.get(f"/jobs/{job_id}/resume-status")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True).lower()
    assert "degraded" in body


def test_resume_status_widget_route_calls_get_resume_status_breakdown_with_job_id(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=_breakdown())
    monkeypatch.setattr(api_client, "get_resume_status_breakdown", spy)
    client.get(f"/jobs/{job_id}/resume-status")
    spy.assert_called_once()
    assert spy.call_args.args[0] == job_id


def test_resume_status_widget_route_404s_when_job_missing(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "get_resume_status_breakdown",
        MagicMock(side_effect=api_client.NotFound("no such job")),
    )
    resp = client.get(f"/jobs/{uuid4()}/resume-status")
    assert resp.status_code == 404


def test_resume_status_widget_route_backend_unavailable_not_500(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "get_resume_status_breakdown",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    resp = client.get(f"/jobs/{uuid4()}/resume-status")
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500
