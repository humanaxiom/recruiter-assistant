"""RED pin — SPONSOR 2026-09-02 §O2 follow-on, from the same user who asked for
the declaration control: "the current location (on shortlist only) is not
enough, because the declaration is a critical eval parameter" [sic — meaning
the OPPOSITE of what shipped: the control landed on the résumé-detail page
only, and the user wants it where the candidate is actually being evaluated —
the shortlist card].

``resume_work_authorization`` ALREADY handles ``context=shortlist`` +
``job_id`` and redirects back to the shortlist — but nothing has ever posted
that from a shortlist card. This is the exact defect class the withdrawal
REASON was: an ABSENCE, not a bug in existing code, so no mutation of
``app.py`` can make it appear. ``shortlist_cards.html`` today only ever prints
"Work authorization not recorded." for every session, writer or not — there is
no way to declare it from the screen where a recruiter is actually comparing
candidates.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.settings import Settings


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


_FORM_RE_TEMPLATE = r'<form[^>]*action="[^"]*{fragment}[^"]*"[^>]*>(.*?)</form>'

_CSRF_INPUT_RE = re.compile(
    r'<input[^>]*name="csrf_token"[^>]*value="([^"]*)"'
    r'|<input[^>]*value="([^"]*)"[^>]*name="csrf_token"'
)


def _work_auth_form_html(body: str, resume_id: Any) -> str:
    """The specific card's work-authorization form, disambiguated by résumé id
    (a shortlist page renders one such form PER CARD, all posting to the same
    route) — never a plain "/work-authorization" search, which would only ever
    find the first card."""
    fragment = re.escape(f"/resumes/{resume_id}/work-authorization")
    pattern = re.compile(_FORM_RE_TEMPLATE.format(fragment=fragment), re.DOTALL)
    match = pattern.search(body)
    assert match is not None, (
        f"no work-authorization form found for résumé {resume_id} — the "
        "declaration cannot be made from this shortlist card at all"
    )
    return match.group(1)


def _card_token(body: str, resume_id: Any, action_fragment: str) -> str:
    fragment = re.escape(f"/resumes/{resume_id}/{action_fragment}")
    pattern = re.compile(_FORM_RE_TEMPLATE.format(fragment=fragment), re.DOTALL)
    match = pattern.search(body)
    assert match is not None, f"no {action_fragment} form for résumé {resume_id}"
    token_match = _CSRF_INPUT_RE.search(match.group(1))
    assert token_match is not None, f"{action_fragment} form has no csrf_token input"
    return token_match.group(1) or token_match.group(2) or ""


# ------------------------------------------- the control exists, on the card


def test_each_card_has_a_work_authorization_form_targeting_its_own_resume(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries = _three_entries(job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    for entry in entries:
        form = _work_auth_form_html(body, entry["resume_id"])
        assert f'value="{job_id}"' in form or f'value="{entry["job_id"]}"' in form
        assert 'name="context"' in form and 'value="shortlist"' in form


def test_each_cards_work_auth_form_carries_a_nonempty_per_card_token(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries = _three_entries(job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    tokens = [_card_token(body, e["resume_id"], "work-authorization") for e in entries]
    for token in tokens:
        assert token, "a card's work-authorization token is empty"
    assert len(set(tokens)) == len(tokens), (
        "two cards share a work-authorization token slot — declaring one "
        "candidate's status would silently invalidate another's"
    )


def test_a_cards_work_auth_token_is_independent_of_its_own_reveal_and_withdraw_tokens(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    entries = [_entry(job_id, resume_id)]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)

    work_auth_token = _card_token(body, resume_id, "work-authorization")
    reveal_token = _card_token(body, resume_id, "reveal")
    withdraw_token = _card_token(body, resume_id, "withdraw")

    assert work_auth_token
    assert work_auth_token != reveal_token
    assert work_auth_token != withdraw_token


def test_the_three_radios_render_in_order_with_the_cards_own_value_preselected(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    entries = [_entry(job_id, resume_id, work_authorization="not_eligible")]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    form = _work_auth_form_html(body, resume_id)

    radios = re.findall(r'<input type="radio" name="status" value="([a-z_]+)"', form)
    assert radios == ["unknown", "eligible", "not_eligible"]

    checked = re.search(
        r'<input type="radio" name="status" value="([a-z_]+)"\s*\n?\s*checked', form
    )
    assert checked is not None and checked.group(1) == "not_eligible"


# ------------------------------------------- the poll fragment carries it too


def test_the_poll_fragment_also_carries_the_per_card_work_auth_form(
    monkeypatch: Any, client: Any
) -> None:
    """``shortlist_cards.html`` is rendered both by the full-page route and by
    the HTMX poll fragment (``GET /jobs/<id>/shortlist-cards``) — both call
    sites must mint and render the same per-card control, or the form works on
    first load and silently vanishes the moment htmx swaps in a fresh poll."""
    job_id = uuid4()
    entries = _three_entries(job_id)
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)

    for entry in entries:
        token = _card_token(body, entry["resume_id"], "work-authorization")
        assert token, (
            f"the poll fragment rendered no usable work-authorization token "
            f"for résumé {entry['resume_id']}"
        )


# --------------------------------------------------------- the end-to-end act


def test_declaring_from_a_shortlist_card_saves_and_returns_to_that_shortlist(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    resume_id = uuid4()
    entries = [_entry(job_id, resume_id)]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)
    token = _card_token(body, resume_id, "work-authorization")

    spy = MagicMock()
    monkeypatch.setattr(api_client, "set_work_authorization", spy)

    resp = client.post(
        f"/resumes/{resume_id}/work-authorization",
        data={
            "csrf_token": token,
            "status": "not_eligible",
            "context": "shortlist",
            "job_id": str(job_id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}/shortlist")
    spy.assert_called_once()
    assert spy.call_args.args[0] == resume_id
    assert spy.call_args.kwargs.get("status") == "not_eligible"


# --------------------------------------------- the "N of M undeclared" count


def test_shortlist_header_shows_the_undeclared_candidate_count(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries = _three_entries(job_id)  # 2 of 3 are "unknown"
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = " ".join(
        client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True).split()
    )

    match = re.search(
        r"(\d+) of (\d+) candidates? (?:has|have) no work-authorization "
        r"declaration",
        body,
    )
    assert match is not None, (
        "no undeclared-candidate count found on the shortlist page — a "
        "recruiter has no way to see how many candidates still need this "
        "recorded without opening every card"
    )
    assert match.group(1) == "2"
    assert match.group(2) == "3"


def test_the_undeclared_count_is_zero_when_everyone_is_declared(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    r1, r2 = uuid4(), uuid4()
    entries = [
        _entry(job_id, r1, work_authorization="eligible"),
        _entry(job_id, r2, work_authorization="not_eligible"),
    ]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    body = " ".join(
        client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True).split()
    )
    match = re.search(
        r"(\d+) of (\d+) candidates? (?:has|have) no work-authorization "
        r"declaration",
        body,
    )
    assert match is not None
    assert match.group(1) == "0"
    assert match.group(2) == "2"


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


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_non_writer_session_does_not_see_the_work_auth_form_on_cards(
    monkeypatch: Any, client: Any, role: str
) -> None:
    _enable_cas_session(monkeypatch, role)
    job_id = uuid4()
    resume_id = uuid4()
    entries = [_entry(job_id, resume_id)]
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=entries))

    resp = client.get(
        f"/jobs/{job_id}/shortlist", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert f'action="/resumes/{resume_id}/work-authorization"' not in body
