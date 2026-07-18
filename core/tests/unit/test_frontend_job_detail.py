"""Slice S3 — job detail: parse-poll, status transitions, blind toggle.

Covers ``api_client.transition_status`` / ``api_client.patch_job`` (via
``httpx.MockTransport``, incl. the 409 illegal-transition path mapped to a
typed ``Conflict``) and the Flask job-detail routes: the HTMX ``parse-status``
poll fragment (which drops its ``hx-trigger`` once parsing completes so polling
stops), the legal-next-state status buttons (draft→open disabled until the LLM
finishes parsing), and the blind-review toggle (which sends ONLY
``{"blind_review": <bool>}``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from frontend.app import app


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


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
