"""RED — a stalled shortlist must say WHY, and must not promise it will fix itself.

**The reason a user waited hours instead of reporting a bug.** The shortlist
page said:

    Waiting for AI to rank candidates… the ranking model was briefly
    unavailable, so this run is queued to retry automatically. It will pick up
    again on its own — no action needed.

Every clause of that was false. The model was up. The run had failed on an
*invalid output* (empty content — the evidence budget was starved), which is
deterministic at ``temperature=0``: try 1, 2 and 3 failed identically and
always would. "No action needed" was exactly backwards.

**And the true reason was already in the database.** ``jobs
.shortlist_state_reason`` held ``"response content was empty (possibly
reasoning model exhausted token budget)"``; ``ShortlistStateOut.reason``
carried it to the API; ``shortlist_status`` was already passed into this
template. The template threw it away and printed a guess instead — the same
"written, carried, and read by nothing" shape that produced three dead columns
on the jobs list a week earlier.

``RankingUnavailableError`` deliberately covers BOTH failure modes, because
both must fail closed (ADR-029). That is right. What was wrong is a *message*
that describes only one of them, and asserts transience the product cannot
know.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from frontend import api_client

_EMPTY_CONTENT = (
    "response content was empty (possibly reasoning model exhausted token "
    "budget); reasoning_present=True"
)


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _status(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "state": "awaiting_llm",
        "reason": _EMPTY_CONTENT,
        "at": "2026-09-09T16:50:37+00:00",
    }
    base.update(over)
    return base


def _render(
    client: Any, monkeypatch: pytest.MonkeyPatch, *, entries: list[Any], status: Any
) -> str:
    monkeypatch.setattr(api_client, "list_shortlist", lambda jid, **kw: entries)
    monkeypatch.setattr(api_client, "get_shortlist_status", lambda jid, **kw: status)
    return client.get(f"/jobs/{uuid4()}/shortlist-cards").get_data(as_text=True)


# ── the recorded reason has to reach the screen ─────────────────────────────


def test_the_recorded_reason_is_shown(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE fix. One string, already in the database, that turns "it is stuck"
    into something a person can act on or report."""
    html = _render(client, monkeypatch, entries=[], status=_status())
    assert "empty" in html.lower(), html[:600]


def test_the_reason_is_shown_even_when_stale_cards_are_on_screen(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Regenerate case: a previous run's cards are still rendered while the
    new run has failed closed. That branch was the most misleading of the
    three — it showed candidates AND promised an automatic replacement, so a
    manager could reasonably read the stale list as current."""
    html = _render(client, monkeypatch, entries=[None], status=_status())
    assert "empty" in html.lower()


# ── and must stop asserting what it cannot know ─────────────────────────────


@pytest.mark.parametrize("entries", [[], [None]], ids=["empty", "stale-cards"])
def test_no_branch_claims_the_model_was_briefly_unavailable(
    client: Any, monkeypatch: pytest.MonkeyPatch, entries: list[Any]
) -> None:
    """The product cannot tell "the model is down" from "the model returned
    something unusable" — ``RankingUnavailableError`` is deliberately one type
    for both. So it must not name one of them.

    Asserted on the RENDERED page, not on the template source. Source
    matching is what let a ``.strftime`` on a JSON string take the jobs list
    down a week ago, and it would also trip over the historical quote in this
    template's own explanatory comment."""
    html = _render(client, monkeypatch, entries=entries, status=_status()).lower()
    assert "briefly" not in html


@pytest.mark.parametrize("entries", [[], [None]], ids=["empty", "stale-cards"])
def test_nothing_tells_the_user_no_action_is_needed(
    client: Any, monkeypatch: pytest.MonkeyPatch, entries: list[Any]
) -> None:
    """An invalid-output failure is deterministic at temperature=0. A retry
    reproduces it exactly, forever. Telling somebody to sit and wait is the
    single most expensive thing this page can do — it is what turned a
    config defect into hours of a user's time."""
    html = _render(client, monkeypatch, entries=entries, status=_status()).lower()
    assert "no action needed" not in html


def test_the_user_is_told_a_repeat_will_not_resolve_itself(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries ARE queued, so the page should say so — but bounded by the truth
    that if the same reason keeps coming back, waiting will not help."""
    html = _render(client, monkeypatch, entries=[], status=_status()).lower()
    assert "regenerate" in html or "same" in html


# ── the honest cases must not regress ───────────────────────────────────────


def test_a_run_in_flight_still_reads_as_in_flight(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ranking`` is a genuinely in-flight run, recorded server-side before the
    enqueue. That message was never wrong and must keep its promise."""
    html = _render(
        client, monkeypatch, entries=[], status=_status(state="ranking", reason=None)
    ).lower()
    assert "empty" not in html
    assert "generating" in html or "in progress" in html


def test_a_missing_reason_does_not_break_the_page(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reason`` is nullable on the DTO, and a row written before the column
    existed reads null. Rendering must degrade, not 500 — the lesson from the
    jobs list, which took the whole page down over one absent field."""
    html = _render(client, monkeypatch, entries=[], status=_status(reason=None))
    assert '<div id="shortlist-cards"' in html


def test_no_status_at_all_still_renders(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_client, "list_shortlist", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "get_shortlist_status", lambda jid, **kw: None)
    resp = client.get(f"/jobs/{uuid4()}/shortlist-cards")
    assert resp.status_code == 200
