"""The shortlist must show which requirements came from the MANAGER.

SPONSOR 2026-09-02 §I4, the last piece. The note is extracted, scored, and
persisted with per-requirement contributions — but a sub-score worth 10% of a
hiring decision that renders as an unlabelled number is exactly the "number
without a cited source" this repo refuses everywhere else.

**The distinction is the whole feature.** The manager's note is deliberately
kept out of `description_raw` so the posting's requirements and the manager's
own stay separable. If the card then renders both in one undifferentiated row
of chips, that separation is destroyed at the last step and the manager cannot
tell whether the thing they asked for was even considered.

So two properties, and the second is the one that is easy to get wrong:

1. The manager's requirements appear, matched or missed.
2. They are **visually distinguishable** from the posting's, and labelled as
   the manager's — not merged into `skill_contributions`' chip row.

A third, quieter property: a job with **no** manager note must render exactly
as it did before this feature. Most jobs have no note, and a stray empty
heading on every card is how a feature that helps a few people annoys everyone.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _entry(manager_contribs: list[dict[str, Any]] | None) -> dict[str, Any]:
    breakdown: dict[str, Any] = {
        "skill": 0.8,
        "experience": 0.7,
        "education": 0.6,
        "seniority": 0.5,
        "vector": 0.9,
        "structured": 0.75,
        "skill_contributions": [
            {"skill": "Python", "score": 1.0, "is_must_have": True, "reason": None}
        ],
    }
    if manager_contribs is not None:
        breakdown["manager_prompt_contributions"] = manager_contribs
        breakdown["manager_prompt"] = 0.5
        breakdown["manager_prompt_measured"] = True
    return {
        "id": str(uuid4()),
        "job_id": str(uuid4()),
        "resume_id": str(uuid4()),
        "rank": 1,
        "score_final": 0.78,
        "score_breakdown": breakdown,
        "evidence": None,
        "generated_at": "2026-09-02T00:00:00Z",
        "blinded": True,
        "display_label": "Candidate A",
    }


def _cards(monkeypatch: Any, client: Any, entry: dict[str, Any]) -> str:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))
    return client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)


_MANAGER = [
    {"skill": "MEG analysis", "score": 1.0, "is_must_have": True, "reason": None},
    {"skill": "Kafka", "score": 0.0, "is_must_have": False, "reason": "missing"},
]


def test_the_managers_requirements_are_rendered(monkeypatch: Any, client: Any) -> None:
    """Property 1. Without this the sub-score is an unexplained 10%."""
    body = _cards(monkeypatch, client, _entry(_MANAGER))
    assert "MEG analysis" in body
    assert "Kafka" in body


def test_they_are_labelled_as_the_managers_not_the_postings(
    monkeypatch: Any, client: Any
) -> None:
    """Property 2, and the one that matters. A manager looking at this card has
    to be able to see that the thing THEY asked for was considered — which an
    undifferentiated chip row cannot tell them."""
    body = " ".join(_cards(monkeypatch, client, _entry(_MANAGER)).split())
    assert re.search(r"[Aa]dded by (the |you|your)|[Yy]our additional", body), (
        "the manager's requirements are rendered with no attribution — merged "
        "into the posting's chips, the provenance the whole field exists to "
        "preserve is destroyed at the display layer"
    )


def test_a_missed_manager_requirement_is_visible_as_missed(
    monkeypatch: Any, client: Any
) -> None:
    """A manager whose requirement went unmet must see that, not just a lower
    number. This is the actionable half — it tells them whether to relax the
    requirement or widen the search."""
    body = _cards(monkeypatch, client, _entry(_MANAGER))
    kafka = body[body.index("Kafka") - 200 : body.index("Kafka") + 60]
    assert "missing" in kafka


def test_the_managers_chips_are_visually_distinct(
    monkeypatch: Any, client: Any
) -> None:
    """Distinguishable by markup, not only by position — position is lost the
    moment the card is narrow enough to wrap."""
    body = _cards(monkeypatch, client, _entry(_MANAGER))
    assert "chip-manager" in body


def test_a_job_with_no_manager_note_renders_as_before(
    monkeypatch: Any, client: Any
) -> None:
    """Most jobs have no note. A stray empty heading on every card is how a
    feature that helps a few people annoys everyone."""
    body = " ".join(_cards(monkeypatch, client, _entry(None)).split())
    assert "chip-manager" not in body
    assert not re.search(r"[Aa]dded by (the |you|your)|[Yy]our additional", body)
    # The posting's own chips are untouched.
    assert "Python" in body


def test_an_empty_manager_list_also_renders_nothing(
    monkeypatch: Any, client: Any
) -> None:
    """A job whose note named no skills stores an empty list, not a missing
    key. It must render like "no note", not like an empty section."""
    body = " ".join(_cards(monkeypatch, client, _entry([])).split())
    assert "chip-manager" not in body
