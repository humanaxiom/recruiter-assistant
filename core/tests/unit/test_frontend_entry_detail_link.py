"""ITEM 3 ("WHY THIS RANK?" LINK) — new file.

``ShortlistEntry.id`` already exists on the schema (``core/src/schemas/
matching.py`` ~645) and ``list_shortlist``/the card-render read already
returns full entries carrying it — but ``shortlist_cards.html`` never reads
``entry.id`` at all today, so every test below fails on a plain substring
search (``/shortlist/<id>`` is byte-absent from the rendered HTML).

Contract pinned here: each card gets a plain GET link,
``<a class="link-sm" href="/shortlist/<entry.id>">Why this rank?</a>``
(``url_for('shortlist_entry_detail', entry_id=entry.id)``) — no new CSRF
slot (a GET mints nothing and consumes nothing).

Per the standing "frontend sees JSON, not DTOs" lesson, every entry fixture
here is built via ``ShortlistEntry(...).model_dump(mode="json")`` — never a
hand-written dict — exactly like ``test_frontend_shortlist.py``'s own
``_dto_entry`` helper (mirrored below rather than imported, to keep this
file collectible standalone).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from src.schemas.matching import ScoreBreakdown, ShortlistEntry

_NOW = dt.datetime(2026, 7, 15, tzinfo=dt.UTC)


@pytest.fixture
def client(csrf_client: Any) -> Any:
    """The shared CSRF-carrying browser client (see
    ``tests/unit/conftest.py``) — this file only issues GETs, which the
    guard never touches, but sharing the fixture keeps this file consistent
    with its siblings and avoids a second, divergent client-building path."""
    return csrf_client


def _dto_entry(
    *,
    entry_id: Any | None = None,
    job_id: Any | None = None,
    resume_id: Any | None = None,
    display_label: str = "Candidate A",
) -> dict[str, Any]:
    """A REAL ``ShortlistEntry`` DTO, ``model_dump(mode="json")``'d — never a
    hand-written dict carrying a UUID/datetime (HANDOFF lesson 6)."""
    entry = ShortlistEntry(
        id=entry_id or uuid4(),
        job_id=job_id or uuid4(),
        resume_id=resume_id or uuid4(),
        rank=1,
        score_final=0.75,
        score_breakdown=ScoreBreakdown(
            skill=0.7,
            experience=0.6,
            education=0.5,
            seniority=0.5,
            vector=0.4,
            structured=0.55,
        ),
        evidence=None,
        generated_at=_NOW,
        blinded=True,
        display_label=display_label,
    )
    return entry.model_dump(mode="json")


# ── GET /jobs/<id>/shortlist-cards (the poll fragment) ───────────────────


def test_poll_fragment_card_links_to_the_entry_detail_route(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry_id = uuid4()
    entry = _dto_entry(entry_id=entry_id, job_id=job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))

    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)

    assert f"/shortlist/{entry_id}" in body
    assert "Why this rank?" in body


def test_poll_fragment_link_appears_exactly_once_per_card(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry_id_a = uuid4()
    entry_id_b = uuid4()
    entries = [
        _dto_entry(entry_id=entry_id_a, job_id=job_id, display_label="Candidate A"),
        _dto_entry(entry_id=entry_id_b, job_id=job_id, display_label="Candidate B"),
    ]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)

    assert body.count(f"/shortlist/{entry_id_a}") == 1
    assert body.count(f"/shortlist/{entry_id_b}") == 1


# ── GET /jobs/<id>/shortlist (the full page, includes the same cards) ────


def test_full_page_card_links_to_the_entry_detail_route(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry_id = uuid4()
    entry = _dto_entry(entry_id=entry_id, job_id=job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    monkeypatch.setattr(
        api_client, "list_resumes", MagicMock(return_value=[{"status": "parsed"}])
    )

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    assert f"/shortlist/{entry_id}" in body
    assert "Why this rank?" in body


# ── following the link reaches the detail route ───────────────────────────


def test_following_the_link_reaches_the_entry_detail_page(
    monkeypatch: Any, client: Any
) -> None:
    """Not just present in the markup — a real, resolvable link. Mocks
    ``api_client.get_shortlist_entry`` (the detail route's own backend call)
    with the SAME entry so a click-through actually renders 200."""
    job_id = uuid4()
    entry_id = uuid4()
    entry = _dto_entry(entry_id=entry_id, job_id=job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    monkeypatch.setattr(
        api_client, "get_shortlist_entry", MagicMock(return_value=entry)
    )

    listing = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    assert f"/shortlist/{entry_id}" in listing

    resp = client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200


def test_link_is_a_plain_get_anchor_not_a_form(monkeypatch: Any, client: Any) -> None:
    """No new CSRF slot: this is a bare ``<a href>``, never a ``<form
    method="post">`` — a GET consumes no token and needs none."""
    job_id = uuid4()
    entry_id = uuid4()
    entry = _dto_entry(entry_id=entry_id, job_id=job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))

    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)

    assert (
        f'href="/shortlist/{entry_id}"' in body
        or f"href='/shortlist/{entry_id}'" in body
    )
