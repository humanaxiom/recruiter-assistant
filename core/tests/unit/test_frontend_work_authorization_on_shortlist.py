"""RED pin — SPONSOR 2026-09-02 §O2 follow-on, REVISED design.

The first version of this file pinned a per-card, per-résumé, one-shot
``action="work_auth"`` slot — the same shape as reveal/withdraw. Security +
review measured that against the real cap (``MAX_TOKENS_PER_SESSION = 64``,
FIFO eviction in ``csrf.issue_token``) and it breaks: a shortlist render mints
reveal + withdraw + work_auth per card (3N slots). At 22 cards the earliest
reveal tokens are already evicted; at 32 cards every reveal token is dead; at
50 (the ``match_coarse_k`` cap) 50 reveal + 36 withdraw tokens are dead. The
cookie ceiling test (``test_serialized_session_cookie_stays_under_the_4093_
byte_ceiling_at_cap``) forecloses raising the cap, and
``test_exempt_routes_still_reject_a_page_token`` forecloses letting the
existing per-résumé routes take the session-wide page token instead.

**The fix moves the card control off the one-shot mechanism entirely.** A new
route, ``POST /jobs/<job_id>/shortlist/<resume_id>/work-authorization``
(endpoint ``shortlist_work_authorization``), is guarded by the ORDINARY
``_csrf_gate`` hook — the same session-wide, reusable page token every other
shortlist-page control (Generate, job status, manager requirements) already
uses. It is NOT one-shot and NOT per-résumé, so minting it costs nothing per
card: every card on every render carries the SAME token, and rendering 50 (or
5,000) cards never touches the 64-slot budget at all. ``job_id`` and
``resume_id`` both live in the URL, so the form needs no hidden ``context``/
``job_id`` fields either.

The résumé-detail page's OWN control (``resume_work_authorization``) is
UNCHANGED — it still mints its own per-résumé one-shot ``action="work_auth"``
slot, because that page only ever renders ONE such form, not up to 50.

This file pins the new route, the new (page-token) form shape, the regression
this design fixes (a 32-card and a 50-card render), the undeclared-candidate
count line (moved inside the poll-swapped ``#shortlist-cards`` div, since it
must update on every 3s poll same as the cards), and a one-line consequence
hint on each card.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from frontend import api_client, csrf
from frontend import app as frontend_app_module
from frontend.app import app
from src.settings import Settings
from tests.unit.test_frontend_csrf_write_route_enforcement import _page_token
from tests.unit.test_frontend_resume_detail import _extract_form_token


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture(autouse=True)
def _no_ranking_state(monkeypatch: Any) -> None:
    """Every job in this file has a settled shortlist — no ranking/awaiting_llm
    state — so the poll banners never interfere with the card assertions."""
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={"job_id": None, "state": None, "reason": None, "at": None}
        ),
    )
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))


def _entry(
    job_id: Any,
    resume_id: Any,
    *,
    rank: int = 1,
    work_authorization: str = "unknown",
    metrics_invalidated: bool = False,
    display_label: str = "Candidate A",
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "job_id": str(job_id),
        "resume_id": str(resume_id),
        "rank": rank,
        "score_final": 0.75,
        "score_breakdown": {
            "skill": 0.8,
            "experience": 0.7,
            "education": 0.6,
            "seniority": 0.5,
            "vector": 0.65,
        },
        "evidence": None,
        "blinded": True,
        "display_label": display_label,
        "work_authorization": work_authorization,
        "metrics_invalidated": metrics_invalidated,
    }


def _three_entries(job_id: Any) -> list[dict[str, Any]]:
    """Three cards, two with no declaration and one already declared eligible —
    the fixture the "N of M" count test below needs."""
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    return [
        _entry(
            job_id,
            r1,
            rank=1,
            work_authorization="unknown",
            display_label="Candidate A",
        ),
        _entry(
            job_id,
            r2,
            rank=2,
            work_authorization="eligible",
            display_label="Candidate B",
        ),
        _entry(
            job_id,
            r3,
            rank=3,
            work_authorization="unknown",
            display_label="Candidate C",
        ),
    ]


def _many_entries(job_id: Any, n: int) -> list[dict[str, Any]]:
    return [
        _entry(job_id, uuid4(), rank=i + 1, display_label=f"Candidate {i}")
        for i in range(n)
    ]


# ── the new route's URL + per-card form extraction ───────────────────────

_CSRF_INPUT_RE = re.compile(
    r'<input[^>]*name="csrf_token"[^>]*value="([^"]*)"'
    r'|<input[^>]*value="([^"]*)"[^>]*name="csrf_token"'
)


def _new_route(job_id: Any, resume_id: Any) -> str:
    return f"/jobs/{job_id}/shortlist/{resume_id}/work-authorization"


def _card_form_html(body: str, job_id: Any, resume_id: Any) -> str:
    """The specific card's work-authorization form, disambiguated by BOTH
    job and résumé id (the new route path is unique per card, so no
    additional scoping trick is needed)."""
    fragment = re.escape(_new_route(job_id, resume_id))
    pattern = re.compile(
        r'<form[^>]*action="[^"]*' + fragment + r'[^"]*"[^>]*>(.*?)</form>',
        re.DOTALL,
    )
    match = pattern.search(body)
    assert match is not None, (
        f"no work-authorization form posting to {_new_route(job_id, resume_id)} "
        "— the declaration cannot be made from this shortlist card at all"
    )
    return match.group(1)


def _card_token(body: str, job_id: Any, resume_id: Any) -> str:
    form = _card_form_html(body, job_id, resume_id)
    token_match = _CSRF_INPUT_RE.search(form)
    assert token_match is not None, "the card's work-authorization form has no token"
    return token_match.group(1) or token_match.group(2) or ""


def _hashed_key(resume_id: Any, action: str = "reveal") -> str:
    """Test-local reimplementation of ``csrf._session_key_for`` — asserts the
    real observable session-storage contract without importing the SUT's own
    private helper (mirrors ``test_frontend_csrf.py``'s ``_hashed_key``)."""
    seed = str(resume_id) if action == "reveal" else f"{action}:{resume_id}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def _session_mapping(client: Any) -> dict[str, str]:
    with client.session_transaction() as session:
        stored = session.get(csrf.SESSION_KEY)
        return dict(stored) if isinstance(stored, dict) else {}


def _session_page_token(client: Any) -> str | None:
    with client.session_transaction() as session:
        stored = session.get(csrf.PAGE_SESSION_KEY)
        return stored if isinstance(stored, str) else None


# ── balanced-tag helper: locate the poll-swapped #shortlist-cards span ────


_DIV_TOKEN_RE = re.compile(r"<div\b|</div>")


def _shortlist_cards_div(html: str) -> str:
    """The exact span of ``<div id="shortlist-cards" ...>...</div>`` — the
    same div both Generate and the 3s poll swap via htmx — found by balanced
    tag matching (mirrors the balanced if/elif/endif matcher already used in
    ``test_resume_detail_withdraw_control_is_outside_the_reveal_identity_
    block``), since nested ``<div>``s inside the cards make a naive
    "first </div> after" search wrong."""
    marker = html.index('id="shortlist-cards"')
    open_tag = html.rfind("<div", 0, marker)
    assert open_tag != -1
    depth = 0
    for m in _DIV_TOKEN_RE.finditer(html, open_tag):
        if m.group(0) == "<div":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return html[open_tag : m.end()]
    raise AssertionError("unbalanced <div> for #shortlist-cards")


_COUNT_RE = re.compile(
    r"(\d+) of (\d+) candidates? (has|have) no work-authorization declaration"
)


# ---------------------------------------------------- the control is RENDERED


def test_each_card_posts_to_the_new_shortlist_scoped_route(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries = _three_entries(job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    for entry in entries:
        form = _card_form_html(body, job_id, entry["resume_id"])
        # The new design carries job_id/resume_id in the URL, not as hidden
        # fields — unlike the old per-card one-shot design this replaces.
        assert 'name="context"' not in form
        assert 'name="job_id"' not in form


def test_each_cards_work_auth_form_carries_the_shared_page_token(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries = _three_entries(job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))
    token = _page_token(client)

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    for entry in entries:
        card_token = _card_token(body, job_id, entry["resume_id"])
        assert card_token, "a card's work-authorization token is empty"
        assert card_token == token, (
            "the card's token is not this session's page token — the new "
            "design shares ONE reusable token across every card"
        )


def test_the_three_radios_render_in_order_with_the_cards_own_value_preselected(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    entries = [_entry(job_id, resume_id, work_authorization="not_eligible")]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    form = _card_form_html(body, job_id, resume_id)

    radios = re.findall(r'<input type="radio" name="status" value="([a-z_]+)"', form)
    assert radios == ["unknown", "eligible", "not_eligible"]

    checked = re.search(
        r'<input type="radio" name="status" value="([a-z_]+)"\s*\n?\s*checked', form
    )
    assert checked is not None and checked.group(1) == "not_eligible"


def test_non_writer_session_does_not_see_the_work_auth_form_on_cards(
    monkeypatch: Any, client: Any
) -> None:
    _enable_cas_session(monkeypatch, "hiring_manager")
    job_id = uuid4()
    resume_id = uuid4()
    entries = [_entry(job_id, resume_id)]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    resp = client.get(
        f"/jobs/{job_id}/shortlist", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert f'action="{_new_route(job_id, resume_id)}"' not in body


# --------------------------------------------------------- the end-to-end act


def test_declaring_from_a_shortlist_card_saves_and_returns_to_that_shortlist(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    entries = [_entry(job_id, resume_id)]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    token = _card_token(body, job_id, resume_id)

    spy = MagicMock()
    monkeypatch.setattr(api_client, "set_work_authorization", spy)
    resp = client.post(
        _new_route(job_id, resume_id),
        data={"csrf_token": token, "status": "not_eligible"},
        follow_redirects=False,
    )
    assert resp.status_code == 302, (
        f"expected the redirect a successful save produces, got "
        f"{resp.status_code} — POST /jobs/<id>/shortlist/<id>/"
        "work-authorization does not exist yet"
    )
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}/shortlist")
    spy.assert_called_once()
    assert spy.call_args.args[0] == resume_id
    assert spy.call_args.kwargs.get("status") == "not_eligible"
    assert spy.call_args.kwargs.get("note") is None


def test_declaring_without_a_token_is_rejected_before_the_backend(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    with patch("frontend.app.api_client") as mock_api:
        mock_api.BackendUnavailable = Exception
        mock_api.NotFound = Exception
        mock_api.BadRequest = Exception
        resp = client.post(
            _new_route(job_id, resume_id), data={"status": "not_eligible"}
        )
    assert resp.status_code == 403
    assert not mock_api.method_calls


def test_declaring_cross_origin_is_rejected_even_with_a_valid_token(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    token = _page_token(client)
    with patch("frontend.app.api_client") as mock_api:
        mock_api.BackendUnavailable = Exception
        mock_api.NotFound = Exception
        mock_api.BadRequest = Exception
        resp = client.post(
            _new_route(job_id, resume_id),
            data={"csrf_token": token, "status": "not_eligible"},
            headers={"Origin": "http://evil.example"},
        )
    assert resp.status_code == 403
    assert not mock_api.method_calls


# --------------------------------- the regression pin for the measured defect


def test_at_32_cards_every_cards_reveal_and_withdraw_token_is_still_live(
    monkeypatch: Any, client: Any
) -> None:
    """32 cards x (reveal + withdraw) = 64 slots = exactly the cap — the
    pre-diff behaviour, and it must not regress. Under the OLD (rejected)
    3-slot-per-card design this same render was already past the cap (96
    slots) and every reveal token was dead; the new design removes the third
    slot entirely, so this boundary case is restored."""
    job_id = uuid4()
    entries = _many_entries(job_id, 32)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    mapping = _session_mapping(client)

    for entry in entries:
        resume_id = entry["resume_id"]
        reveal_token = _extract_form_token(body, f"/resumes/{resume_id}/reveal")
        withdraw_token = _extract_form_token(body, f"/resumes/{resume_id}/withdraw")
        assert mapping.get(_hashed_key(resume_id, "reveal")) == reveal_token, (
            f"résumé {resume_id}'s reveal token is not the live session token "
            "at 32 cards — a card rendered near the start of the page would "
            "403 on the very first click"
        )
        assert mapping.get(_hashed_key(resume_id, "withdraw")) == withdraw_token


def test_at_50_cards_every_cards_work_auth_token_is_the_same_valid_page_token(
    monkeypatch: Any, client: Any
) -> None:
    """50 cards (the ``match_coarse_k`` cap) — the exact render size that
    killed every reveal token and 36 withdraw tokens under the rejected
    3-slot design. The work-authorization control does not touch that budget
    at all under the new design: it is ONE reusable page token, identical on
    every one of the 50 cards, never minted per-résumé.

    Deliberately NOT asserting reveal/withdraw tokens here — 2 slots x 50
    cards = 100, over the 64 cap on its own, which is a pre-existing
    property of the reveal/withdraw design this branch does not touch and is
    not this branch's regression to pin or fix."""
    job_id = uuid4()
    entries = _many_entries(job_id, 50)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))
    page_token = _page_token(client)

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    tokens = {_card_token(body, job_id, entry["resume_id"]) for entry in entries}
    assert tokens == {page_token}, (
        "not every card carried this session's page token — a per-card mint "
        "would reproduce the exact defect this design change fixes"
    )


# --------------------------------------------- the "N of M undeclared" count


def test_shortlist_header_count_is_inside_the_poll_swapped_div_on_the_full_page(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries = _three_entries(job_id)  # 2 of 3 are "unknown"
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    segment = " ".join(_shortlist_cards_div(body).split())

    match = _COUNT_RE.search(segment)
    assert match is not None, (
        "no undeclared-candidate count found inside #shortlist-cards — since "
        "that div is what Generate/the 3s poll swap, a count rendered "
        "OUTSIDE it would go stale the moment the cards refresh"
    )
    assert match.group(1) == "2"
    assert match.group(2) == "3"
    assert match.group(3) == "have"


def test_shortlist_header_count_is_present_in_the_poll_fragment_too(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries = _three_entries(job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    segment = " ".join(body.split())

    match = _COUNT_RE.search(segment)
    assert match is not None, (
        "the poll-fragment response (what htmx actually swaps in every 3s) "
        "carries no undeclared-candidate count"
    )
    assert match.group(1) == "2"
    assert match.group(2) == "3"


def test_the_undeclared_count_uses_singular_grammar_for_exactly_one(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    entries = [
        _entry(job_id, r1, work_authorization="unknown"),
        _entry(job_id, r2, work_authorization="eligible"),
        _entry(job_id, r3, work_authorization="not_eligible"),
    ]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = " ".join(
        client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True).split()
    )
    match = _COUNT_RE.search(body)
    assert match is not None
    assert match.group(1) == "1"
    assert match.group(2) == "3"
    assert match.group(3) == "has", (
        f"singular subject took the plural verb {match.group(3)!r} — "
        '"1 of 3 candidates has", not "have"'
    )


def test_the_undeclared_count_is_absent_when_there_are_no_entries(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    assert _COUNT_RE.search(body) is None


# ------------------------------------------------------- the consequence hint


def test_the_ranked_last_consequence_hint_renders_once_per_writer_card(
    monkeypatch: Any, client: Any
) -> None:
    """A one-line hint under each card's fieldset — NOT the pre-existing
    ``voided`` banner (which only appears once a candidate is ALREADY marked
    not eligible); every entry here is non-voided so that banner never
    renders, isolating this assertion to the new hint."""
    job_id = uuid4()
    entries = [
        _entry(job_id, uuid4(), rank=1, display_label="Candidate A"),
        _entry(job_id, uuid4(), rank=2, display_label="Candidate B"),
    ]
    for entry in entries:
        assert entry["metrics_invalidated"] is False

    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))
    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True).lower()

    assert body.count("ranked last") == len(entries), (
        "expected exactly one consequence hint per writer card, found "
        f"{body.count('ranked last')} for {len(entries)} cards"
    )


# ----------------------------------------------- hidden for a non-writer role


def _authenticated(role: str) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": "jordan",
        "cas_enabled": True,
        "role": role,
    }


def _enable_cas_session(monkeypatch: Any, role: str) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(role))
    )
