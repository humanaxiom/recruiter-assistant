"""RED — Sponsor requirements PR2 slice 3, the shortlist card's read-only
"SFU internal" chip.

The uplift itself is pipeline arithmetic (``test_internal_uplift_scoring.py``);
this file is only about DISPLAYING it on a shortlist card: a short, read-only
line such as "SFU internal (APSA) +5" on a card whose résumé is flagged
``internal_apsa``/``internal_cupe`` True, sourced from the SAME reproducibility
stamp (``pipeline_meta.internal_uplift_amount``) that explains every other
number on the card — never a hardcoded literal, and never today's Settings
value, for the same "explaining a historical score with today's config would
be dishonest" reason ``pipeline_meta.weights`` already exists for.

**Every fixture here is built via ``ShortlistEntry(...).model_dump(mode="json")``**
— never a hand-written dict — mirroring
``test_templates_render_api_shaped_rows.py``. The frontend is a BFF reading
JSON over HTTP: every timestamp on the wire is a string, and a hand-written
fixture is exactly where a raw ``datetime`` gets typed by mistake and reaches
the template only in production.

**No new CSRF token slot.** The chip is a `<span>` — nothing to submit, so it
must mint nothing. The per-card one-shot budget
(``frontend.csrf.MAX_TOKENS_PER_SESSION = 64``, 2 slots/card already,
measured dead from 22 cards — see
``test_frontend_work_authorization_on_shortlist.py``) is asserted UNCHANGED at
32 and 50 cards, per this repo's standing policy for any test of a per-card
control: assume nothing, render the real count.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app
from tests.unit.test_frontend_work_authorization_on_shortlist import _session_mapping

_TS = dt.datetime(2026, 9, 9, 8, 30, tzinfo=dt.UTC)


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture(autouse=True)
def _no_ranking_state(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={"job_id": None, "state": None, "reason": None, "at": None}
        ),
    )
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))


def _breakdown_kwargs() -> dict[str, float]:
    return dict(
        skill=0.8,
        experience=0.7,
        education=0.6,
        seniority=0.5,
        vector=0.65,
        structured=0.7,
    )


def _pipeline_meta(**over: Any) -> dict[str, Any]:
    from src.schemas.matching import DEFAULT_WEIGHTS, PipelineMeta

    base: dict[str, Any] = dict(
        model_gen="gpt-oss:20b",
        model_emb="nomic-embed-text",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=DEFAULT_WEIGHTS,
        generated_at=_TS,
        internal_uplift_amount=0.05,
    )
    base.update(over)
    return PipelineMeta(**base).model_dump(mode="json")


def _shortlist_entry(
    job_id: Any,
    resume_id: Any,
    *,
    rank: int = 1,
    display_label: str = "Candidate A",
    internal_apsa: bool = False,
    internal_cupe: bool = False,
    pipeline_meta: dict[str, Any] | None = None,
    **over: Any,
) -> dict[str, Any]:
    """Build one shortlist-card row exactly as ``GET /jobs/{id}/shortlist``
    puts it on the wire — from ``ShortlistEntry``, dumped in JSON mode."""
    from src.schemas.matching import ScoreBreakdown, ShortlistEntry

    base: dict[str, Any] = dict(
        id=uuid4(),
        job_id=job_id,
        resume_id=resume_id,
        rank=rank,
        score_final=0.75,
        score_structured=0.7,
        score_evidence=0.6,
        score_breakdown=ScoreBreakdown(**_breakdown_kwargs()),
        evidence=None,
        pipeline_meta=pipeline_meta,
        evidence_evaluated=True,
        generated_at=_TS,
        blinded=True,
        display_label=display_label,
        work_authorization="eligible",
        internal_apsa=internal_apsa,
        internal_cupe=internal_cupe,
    )
    base.update(over)
    return ShortlistEntry(**base).model_dump(mode="json")


def _many_entries(job_id: Any, n: int, **flags: Any) -> list[dict[str, Any]]:
    return [
        _shortlist_entry(
            job_id, uuid4(), rank=i + 1, display_label=f"Candidate {i}", **flags
        )
        for i in range(n)
    ]


# ------------------------------------------------------------- the chip itself


def test_an_apsa_candidate_shows_the_chip_with_its_stamped_uplift_amount(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entry = _shortlist_entry(
        job_id,
        uuid4(),
        internal_apsa=True,
        pipeline_meta=_pipeline_meta(internal_uplift_amount=0.05),
    )
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    assert "SFU internal" in body
    assert "APSA" in body
    assert "+5" in body, (
        "the displayed uplift must come from pipeline_meta.internal_uplift_"
        "amount (0.05 here), never a hardcoded literal"
    )


def test_a_cupe_candidate_shows_the_chip_too(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    entry = _shortlist_entry(
        job_id,
        uuid4(),
        internal_cupe=True,
        pipeline_meta=_pipeline_meta(internal_uplift_amount=0.05),
    )
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    assert "SFU internal" in body
    assert "CUPE" in body


def test_the_displayed_amount_tracks_a_non_default_stamped_uplift(
    monkeypatch: Any, client: Any
) -> None:
    """A job ranked when the uplift was configured to 0.10 must show +10, not
    the current default — the same "explain the historical number, not
    today's config" rule ``pipeline_meta.weights`` already follows."""
    job_id = uuid4()
    entry = _shortlist_entry(
        job_id,
        uuid4(),
        internal_apsa=True,
        pipeline_meta=_pipeline_meta(internal_uplift_amount=0.10),
    )
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    assert "+10" in body
    assert "+5" not in body


def test_no_chip_when_neither_flag_is_set(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()
    entry = _shortlist_entry(
        job_id, uuid4(), pipeline_meta=_pipeline_meta(internal_uplift_amount=0.05)
    )
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    assert "SFU internal" not in body


def test_a_legacy_row_with_neither_field_supplied_still_renders(
    monkeypatch: Any, client: Any
) -> None:
    """A row from before this feature existed carries neither
    ``internal_apsa``/``internal_cupe`` nor a stamped uplift amount at all —
    omitted entirely, not set False, the way a genuinely pre-feature payload
    looks. The page must not 500."""
    job_id = uuid4()
    entry = _shortlist_entry(job_id, uuid4())
    del entry["internal_apsa"]
    del entry["internal_cupe"]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[entry]))

    resp = client.get(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    assert "SFU internal" not in resp.get_data(as_text=True)


# ------------------------------------------ no new CSRF token slot, ever


def test_the_chip_mints_no_token_at_32_cards(monkeypatch: Any, client: Any) -> None:
    """32 cards x (reveal + withdraw) = 64 slots = exactly the cap (see
    ``test_frontend_work_authorization_on_shortlist.py``'s own regression
    pin). The chip must not add a third slot per card -- rendering 32 cards,
    some carrying the chip, must leave the session's token count IDENTICAL to
    32 cards with no chip data at all."""
    job_id = uuid4()
    plain = _many_entries(job_id, 32)
    with_chip = _many_entries(
        job_id,
        32,
        internal_apsa=True,
        pipeline_meta=_pipeline_meta(internal_uplift_amount=0.05),
    )

    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=plain))
    client.get(f"/jobs/{job_id}/shortlist")
    plain_count = len(_session_mapping(client))

    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=with_chip))
    client.get(f"/jobs/{job_id}/shortlist")
    chip_count = len(_session_mapping(client))

    assert chip_count == plain_count == 64


def test_the_chip_mints_no_token_at_50_cards(monkeypatch: Any, client: Any) -> None:
    """50 cards (the ``match_coarse_k`` cap) -- FIFO eviction already caps the
    session at 64 slots from reveal+withdraw alone; the chip must not change
    that count either."""
    job_id = uuid4()
    plain = _many_entries(job_id, 50)
    with_chip = _many_entries(
        job_id,
        50,
        internal_apsa=True,
        pipeline_meta=_pipeline_meta(internal_uplift_amount=0.05),
    )

    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=plain))
    client.get(f"/jobs/{job_id}/shortlist")
    plain_count = len(_session_mapping(client))

    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=with_chip))
    client.get(f"/jobs/{job_id}/shortlist")
    chip_count = len(_session_mapping(client))

    assert chip_count == plain_count
