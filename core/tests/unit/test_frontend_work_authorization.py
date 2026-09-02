"""The work-authorization declaration control, browser → Flask → API.

SPONSOR 2026-09-02 §O2. This file exists because of a specific, recorded
failure mode rather than for coverage: ROADMAP's "what the gates cannot see"
table names **an ABSENCE — a form input that was never rendered, which no
mutation of existing code can make appear.** The withdrawal REASON was exactly
that for months. The route read it, the backend accepted it (max_length=500),
no template ever rendered the input, and every withdrawal in the live database
recorded ``None`` — discovered only by running the product, after a decision
memo had already priced a feature that depended on it.

A screening column with no way for a recruiter to set it would be the same
defect on a field that decides whether a real person is ranked at all. So the
first test here asserts the INPUT EXISTS on the page, and the rest assert it
reaches the backend.

The three-radio shape is also pinned deliberately. A control that defaults to
or visually privileges "not eligible" would nudge a recruiter toward an adverse
finding on a ground adjacent to national origin and immigration status.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app
from tests.unit.test_frontend_resume_detail import _extract_form_token, _resume


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _page(
    monkeypatch: Any, client: Any, resume_id: Any, status: str = "unknown"
) -> str:
    payload = _resume(resume_id, blinded=True)
    payload["work_authorization"] = status
    monkeypatch.setattr(api_client, "get_resume", MagicMock(return_value=payload))
    return client.get(f"/resumes/{resume_id}").get_data(as_text=True)


def _token(monkeypatch: Any, client: Any, resume_id: Any) -> str:
    return _extract_form_token(
        _page(monkeypatch, client, resume_id), "/work-authorization"
    )


# ---------------------------------------------------- the control is RENDERED


def test_the_declaration_control_is_actually_on_the_page(
    monkeypatch: Any, client: Any
) -> None:
    """THE test this file exists for. A column a recruiter cannot set is a
    column that stays ``unknown`` forever — the withdrawal-reason defect,
    repeated on a field that decides whether someone is ranked."""
    body = _page(monkeypatch, client, uuid4())
    assert "/work-authorization" in body, (
        "no form posts to the work-authorization route — the declaration "
        "cannot be made from the product at all"
    )
    for value in ("unknown", "eligible", "not_eligible"):
        assert f'value="{value}"' in body, f"the {value!r} option is not rendered"


def test_all_three_options_are_offered_as_equal_choices(
    monkeypatch: Any, client: Any
) -> None:
    """Three radios, not a checkbox and not a two-way toggle. A boolean control
    would collapse "not recorded" into "not eligible" at the UI layer even
    though the schema keeps them apart."""
    body = _page(monkeypatch, client, uuid4())
    radios = re.findall(r'<input type="radio" name="status" value="([a-z_]+)"', body)
    assert sorted(radios) == ["eligible", "not_eligible", "unknown"]


def test_not_recorded_is_preselected_when_nothing_was_declared(
    monkeypatch: Any, client: Any
) -> None:
    """The default must be the neutral state. A form that opens pre-set to an
    adverse value turns a mis-click into an adverse finding."""
    body = _page(monkeypatch, client, uuid4(), status="unknown")
    checked = re.search(
        r'<input type="radio" name="status" value="([a-z_]+)"\s*\n?\s*checked', body
    )
    assert checked is not None and checked.group(1) == "unknown"


def test_the_current_declaration_is_preselected(monkeypatch: Any, client: Any) -> None:
    """Re-opening the page must show what was recorded, not reset to neutral —
    otherwise a recruiter saving an unrelated edit silently clears a real
    declaration."""
    body = _page(monkeypatch, client, uuid4(), status="not_eligible")
    checked = re.search(
        r'<input type="radio" name="status" value="([a-z_]+)"\s*\n?\s*checked', body
    )
    assert checked is not None and checked.group(1) == "not_eligible"


def test_the_page_says_the_declaration_is_never_inferred(
    monkeypatch: Any, client: Any
) -> None:
    """The one substantive claim the UI has to make. A recruiter who believes
    the system worked this out from the résumé will not check it."""
    # Whitespace-normalised: the copy wraps across lines in the template, and
    # a raw substring check would break on a purely cosmetic re-wrap while
    # saying nothing about whether the claim is still on the page.
    body = " ".join(_page(monkeypatch, client, uuid4()).split())
    assert "candidate declared" in body
    assert "never inferred from their" in body


# ------------------------------------------------------------- the round trip


def test_the_route_is_post_only(client: Any) -> None:
    """A GET that records an adverse decision is prefetchable and forgeable by
    a link — the same reasoning that made reveal and withdraw POST-only."""
    assert client.get(f"/resumes/{uuid4()}/work-authorization").status_code == 405


@pytest.mark.parametrize("status", ["eligible", "not_eligible", "unknown"])
def test_the_route_forwards_each_status_and_redirects_back(
    monkeypatch: Any, client: Any, status: str
) -> None:
    resume_id = uuid4()
    token = _token(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id, blinded=True))
    monkeypatch.setattr(api_client, "set_work_authorization", spy)
    resp = client.post(
        f"/resumes/{resume_id}/work-authorization",
        data={"csrf_token": token, "status": status},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/resumes/{resume_id}")
    spy.assert_called_once()
    assert spy.call_args.args[0] == resume_id
    assert spy.call_args.kwargs.get("status") == status


def test_the_route_forwards_the_note(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    token = _token(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id, blinded=True))
    monkeypatch.setattr(api_client, "set_work_authorization", spy)
    client.post(
        f"/resumes/{resume_id}/work-authorization",
        data={"csrf_token": token, "status": "eligible", "note": "PR card sighted"},
    )
    assert spy.call_args.kwargs.get("note") == "PR card sighted"


def test_the_route_rejects_a_missing_csrf_token(monkeypatch: Any, client: Any) -> None:
    """A forged cross-site submit must never manufacture a screening decision
    — or the audit row that says a named recruiter made one."""
    spy = MagicMock()
    monkeypatch.setattr(api_client, "set_work_authorization", spy)
    resp = client.post(
        f"/resumes/{uuid4()}/work-authorization", data={"status": "not_eligible"}
    )
    assert resp.status_code == 403
    spy.assert_not_called()


def test_the_route_rejects_a_garbage_csrf_token(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    _token(monkeypatch, client, resume_id)
    spy = MagicMock()
    monkeypatch.setattr(api_client, "set_work_authorization", spy)
    resp = client.post(
        f"/resumes/{resume_id}/work-authorization",
        data={"csrf_token": "not-the-real-token", "status": "not_eligible"},
    )
    assert resp.status_code == 403
    spy.assert_not_called()


def test_an_empty_status_is_rejected_before_the_backend(
    monkeypatch: Any, client: Any
) -> None:
    """``status`` has no default anywhere in the stack — setting this is a
    deliberate act, so a caller that names no value gets a 400 rather than
    falling into one."""
    resume_id = uuid4()
    token = _token(monkeypatch, client, resume_id)
    spy = MagicMock()
    monkeypatch.setattr(api_client, "set_work_authorization", spy)
    resp = client.post(
        f"/resumes/{resume_id}/work-authorization", data={"csrf_token": token}
    )
    assert resp.status_code == 400
    spy.assert_not_called()


def test_the_work_auth_token_does_not_share_withdraws_slot(
    monkeypatch: Any, client: Any
) -> None:
    """Both controls render on the page at once, so they need independent
    one-shot tokens. Sharing a slot would mean saving a declaration silently
    invalidated the withdraw button beside it — a control that looks fine and
    403s when used."""
    resume_id = uuid4()
    body = _page(monkeypatch, client, resume_id)
    assert _extract_form_token(body, "/work-authorization") != _extract_form_token(
        body, "/withdraw"
    )
