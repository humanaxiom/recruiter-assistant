"""ITEM 1 (A DROPPED REGENERATE IS REMEMBERED) — new file, frontend slice.

``shortlist_cards.html`` never reads a ``rerun_requested`` key off the
status payload at all today, so every test below fails on a plain substring
search for the new hint paragraph.

Contract pinned here: when the status payload's ``rerun_requested`` is
``true``, the template renders
``<p id="rerun-queued" class="hint">A run is in progress; your regenerate
request will run when it finishes.</p>`` — on BOTH the full page
(``GET /jobs/<id>/shortlist``) and the poll fragment
(``GET /jobs/<id>/shortlist-cards``), since the fragment is what a real
browser is actually polling and re-swapping every 3s.

Per the standing "frontend sees JSON, not DTOs" lesson, every status payload
here is built via ``ShortlistStatusResponse(...).model_dump(mode="json")`` —
never a hand-written dict.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from src.schemas.matching import ShortlistStatusResponse

_NOW = dt.datetime(2026, 9, 17, tzinfo=dt.UTC)

_HINT_ID = 'id="rerun-queued"'
_HINT_TEXT = "A run is in progress; your regenerate request will run when it finishes."


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _status_payload(
    *, job_id: Any, state: str | None = "ranking", rerun_requested: bool
) -> dict[str, Any]:
    """A REAL ``ShortlistStatusResponse`` DTO, ``model_dump(mode="json")``'d
    — never a hand-written dict."""
    resp = ShortlistStatusResponse(
        job_id=job_id,
        state=state,
        reason=None,
        at=_NOW if state is not None else None,
        rerun_requested=rerun_requested,
    )
    return resp.model_dump(mode="json")


# ── GET /jobs/<id>/shortlist-cards (the poll fragment) ────────────────────


def test_poll_fragment_shows_the_rerun_queued_hint_when_true(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value=_status_payload(job_id=job_id, rerun_requested=True)
        ),
    )

    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)

    assert _HINT_ID in body
    assert _HINT_TEXT in body


def test_poll_fragment_omits_the_rerun_queued_hint_when_false(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value=_status_payload(job_id=job_id, rerun_requested=False)
        ),
    )

    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)

    assert _HINT_ID not in body
    assert _HINT_TEXT not in body


def test_poll_fragment_omits_the_hint_when_status_carries_no_state_at_all(
    monkeypatch: Any, client: Any
) -> None:
    """The overwhelmingly common case: no run in flight, nothing queued —
    ``ShortlistStatusResponse``'s own default (``rerun_requested=False``)
    must still degrade cleanly, never raise on a missing key."""
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value=_status_payload(
                job_id=job_id, state=None, rerun_requested=False
            )
        ),
    )

    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)

    assert _HINT_ID not in body


# ── GET /jobs/<id>/shortlist (the full page) ──────────────────────────────


def test_full_page_shows_the_rerun_queued_hint_when_true(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value=_status_payload(job_id=job_id, rerun_requested=True)
        ),
    )

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    assert _HINT_ID in body
    assert _HINT_TEXT in body


def test_full_page_omits_the_rerun_queued_hint_when_false(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value=_status_payload(job_id=job_id, rerun_requested=False)
        ),
    )

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    assert _HINT_ID not in body
    assert _HINT_TEXT not in body
