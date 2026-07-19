"""Slice S5 — shortlist generate + ranked candidate cards.

Covers ``api_client.generate_shortlist`` (POST /jobs/{id}/shortlist enqueue ack,
via ``httpx.MockTransport``), the Flask ``POST /jobs/<id>/shortlist`` generate
route (calls the backend then returns a pollable "Generating…" fragment) and the
``GET /jobs/<id>/shortlist-cards`` HTMX poll fragment (keeps its ``hx-trigger``
while the list is empty and DROPS it once ranked entries exist). Also asserts the
ranked-card rendering: rank, score×100, the five sub-score tiles, matched/missing
skill chips and the evidence panel — plus the graceful ``evidence=None`` fallback.

**Blind invariants (the reason this app exists):** the card renders
``display_label`` ("Candidate A"), never a real name; ``list_shortlist`` is
called with NO ``reveal`` kwarg on the card-render path; and a planted fake
name/email/phone is byte-absent from the rendered cards.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from frontend.app import app

_REAL_NAME = "Zzyzxqrst Wibblesworth"
_REAL_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_REAL_PHONE = "604-555-0192"


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _full_entry(entry_id: Any) -> dict[str, Any]:
    return {
        "id": str(entry_id),
        "job_id": str(uuid4()),
        "resume_id": str(uuid4()),
        "rank": 1,
        "score_final": 0.87,
        "score_breakdown": {
            "skill": 0.90,
            "experience": 0.80,
            "education": 0.70,
            "seniority": 0.60,
            "vector": 0.75,
            "structured": 0.50,
            "motivation": 0.40,
            "skill_contributions": [
                {
                    "skill": "PostgreSQL",
                    "score": 0.9,
                    "is_must_have": True,
                    "reason": None,
                },
                {
                    "skill": "Kubernetes",
                    "score": 0.0,
                    "is_must_have": False,
                    "reason": "missing",
                },
            ],
        },
        "evidence": {
            "requirements": [
                {
                    "requirement": "5+ years backend experience",
                    "status": "met",
                    "evidence": "Led the backend team for six years",
                    "evidence_chunk_ids": ["chunk-11", "chunk-22"],
                    "confidence": 0.92,
                },
                {
                    "requirement": "Kubernetes in production",
                    "status": "missing",
                    "evidence": "",
                    "evidence_chunk_ids": [],
                    "confidence": 0.2,
                },
            ],
            "overall_summary": "Strong backend candidate, no k8s exposure.",
        },
        "blinded": True,
        "display_label": "Candidate A",
    }


# ── api_client.generate_shortlist ────────────────────────────────────────


def test_generate_shortlist_posts_to_the_shortlist_path() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        return httpx.Response(202, json={"job_id": str(job_id), "status": "enqueued"})

    result = api_client.generate_shortlist(job_id, client=_client_with(handler))
    assert captured["method"] == "POST"
    assert captured["url"].path == f"/jobs/{job_id}/shortlist"
    assert result == {"job_id": str(job_id), "status": "enqueued"}


def test_generate_shortlist_maps_5xx_to_backend_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    with pytest.raises(api_client.BackendUnavailable):
        api_client.generate_shortlist(uuid4(), client=_client_with(handler))


def test_generate_shortlist_signature_has_no_reveal_parameter() -> None:
    sig = inspect.signature(api_client.generate_shortlist)
    assert "reveal" not in sig.parameters


# ── POST /jobs/<id>/shortlist — generate route ───────────────────────────


def test_generate_route_calls_backend_and_returns_pollable_fragment(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value={"job_id": str(job_id), "status": "enqueued"})
    monkeypatch.setattr(api_client, "generate_shortlist", spy)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    resp = client.post(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 200
    spy.assert_called_once()
    body = resp.get_data(as_text=True)
    assert "hx-trigger" in body  # the returned fragment polls for results
    assert "Generating" in body


def test_generate_route_backend_unavailable_is_not_a_500(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "generate_shortlist",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    resp = client.post(f"/jobs/{job_id}/shortlist")
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500


# ── GET /jobs/<id>/shortlist-cards — poll fragment ───────────────────────


def test_shortlist_cards_polls_while_empty(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" in body  # keeps polling
    assert "Generating" in body


def test_shortlist_cards_stops_polling_once_entries_exist(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" not in body  # polling stopped


def test_shortlist_cards_gives_up_at_the_attempt_cap(
    monkeypatch: Any, client: Any
) -> None:
    """The bounded poll: at the cap, with still no entries, it STOPS (drops
    hx-trigger) and shows a give-up message instead of polling forever."""
    from frontend.app import _MAX_SHORTLIST_POLL_ATTEMPTS

    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(
        f"/jobs/{job_id}/shortlist-cards?attempt={_MAX_SHORTLIST_POLL_ATTEMPTS}"
    ).get_data(as_text=True)
    assert "hx-trigger" not in body
    assert "No ranked candidates yet" in body


def test_shortlist_cards_below_cap_polls_and_increments_attempt(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards?attempt=5").get_data(
        as_text=True
    )
    assert "hx-trigger" in body
    assert "attempt=6" in body  # the next poll carries an incremented counter


def test_shortlist_cards_clamps_out_of_range_attempt(
    monkeypatch: Any, client: Any
) -> None:
    """A hand-edited/garbage ``attempt`` must never crash or unbound the loop."""
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    assert client.get(f"/jobs/{job_id}/shortlist-cards?attempt=-9").status_code == 200
    assert client.get(f"/jobs/{job_id}/shortlist-cards?attempt=abc").status_code == 200
    huge = client.get(f"/jobs/{job_id}/shortlist-cards?attempt=999999").get_data(
        as_text=True
    )
    assert "hx-trigger" not in huge  # clamped to the cap → gives up, no runaway


def test_generate_button_disabled_until_a_resume_is_parsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsing"}])
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    assert "disabled" in body
    assert "hx-post" not in body  # the disabled button cannot enqueue a ranking


def test_generate_button_enabled_when_a_resume_is_parsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    assert "hx-post" in body  # Generate is wired once a résumé is parsed


def test_shortlist_cards_call_carries_no_reveal_kwarg(
    monkeypatch: Any, client: Any
) -> None:
    """The card-render read is unconditionally blind, exactly like the list
    read: the view must never pass ``reveal`` through on this path."""
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "list_shortlist", spy)
    client.get(f"/jobs/{job_id}/shortlist-cards")
    spy.assert_called_once()
    assert "reveal" not in spy.call_args.kwargs


def test_shortlist_card_has_audited_reveal_button(
    monkeypatch: Any, client: Any
) -> None:
    """FU-1: each card carries an audited-reveal button — a POST form to the
    reveal route with ``context=shortlist`` — so identity can be revealed
    straight from the shortlist (not only from the résumé page)."""
    job_id = uuid4()
    resume_id = uuid4()
    entry = _full_entry(uuid4())
    entry["resume_id"] = str(resume_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert f"/resumes/{resume_id}/reveal" in body
    assert 'method="post"' in body.lower()
    assert 'value="shortlist"' in body
    assert "Reveal identity" in body


def test_shortlist_cards_404s_when_job_missing(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(side_effect=api_client.NotFound("no job")),
    )
    resp = client.get(f"/jobs/{uuid4()}/shortlist-cards")
    assert resp.status_code == 404


# ── ranked-card rendering ────────────────────────────────────────────────


def test_card_renders_rank_score_tiles_chips_and_evidence(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    # rank + display label
    assert "Candidate A" in body
    assert "1" in body
    # final score = round(0.87 * 100)
    assert "87" in body
    # five sub-score tiles
    for label in ("skill", "experience", "education", "seniority", "vector"):
        assert label in body.lower()
    # matched + missing skill chips
    assert "PostgreSQL" in body
    assert "Kubernetes" in body
    assert "missing" in body.lower()
    # evidence panel: quoted evidence, chunk ids, overall summary
    assert "Led the backend team for six years" in body
    assert "chunk-11" in body
    assert "Strong backend candidate, no k8s exposure." in body


def test_card_flags_must_have_skills(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_shortlist",
        MagicMock(return_value=[_full_entry(uuid4())]),
    )
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "must" in body.lower()  # must-have flag surfaced somewhere


def test_card_with_null_evidence_renders_fallback_without_crashing(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry = _full_entry(uuid4())
    entry["evidence"] = None
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    assert resp.status_code == 200
    assert "Evidence not available for this candidate" in resp.get_data(as_text=True)


def test_card_with_empty_evidence_requirements_renders_fallback(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry = _full_entry(uuid4())
    entry["evidence"] = {"requirements": [], "overall_summary": ""}
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    assert resp.status_code == 200
    assert "Evidence not available for this candidate" in resp.get_data(as_text=True)


# ── blind invariants ─────────────────────────────────────────────────────


def test_card_uses_display_label_never_a_real_name(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry = _full_entry(uuid4())
    # Plant PII on fields the card must never surface.
    entry["candidate_name"] = _REAL_NAME
    entry["candidate"] = {
        "name": _REAL_NAME,
        "email": _REAL_EMAIL,
        "phone": _REAL_PHONE,
    }
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    raw = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert "Candidate A" in raw
    assert _REAL_NAME not in raw
    assert _REAL_EMAIL not in raw
    assert _REAL_PHONE not in raw


def test_shortlist_list_page_shows_generate_button_and_export_link(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    # Generate button posts to the generate route.
    assert f"/jobs/{job_id}/shortlist" in body
    assert "Generate" in body
    # Existing Export CSV link preserved.
    assert "Export CSV" in body
    assert f"/jobs/{job_id}/shortlist/export" in body


def test_shortlist_list_page_read_carries_no_reveal_kwarg(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "list_shortlist", spy)
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    client.get(f"/jobs/{job_id}/shortlist")
    spy.assert_called_once()
    assert "reveal" not in spy.call_args.kwargs
