"""RED pin — the reveal-then-act defect reproduced against the live product,
2026-09-09 18:40: ``POST /resumes/<id>/reveal`` returns 200, then
``POST /resumes/<id>/work-authorization`` (the very next click on the SAME
now-revealed page) returns 403.

**Root cause.** ``resume_detail`` (GET) mints FOUR one-shot CSRF tokens —
``csrf_token`` (reveal), ``withdraw_csrf_token``, ``work_auth_csrf_token`` and
``document_csrf_token`` — and passes all four into ``resume_detail.html``.
``resume_reveal`` re-renders the SAME template but passes only ``resume``,
``current_year`` and ``revealed=True``. The other three template variables are
therefore Jinja-undefined, which renders as the empty string, so the
withdraw/reinstate, document and work-authorization forms on the just-revealed
page carry a hidden ``csrf_token`` value of ``""``. Every submit from that page
is then rejected by ``csrf.verify_and_consume`` — this is why every résumé on
the pilot box still reads ``work_authorization='unknown'`` and the audit log
has no ``set_work_authorization`` row: the ONE workflow a recruiter actually
uses — reveal a candidate, then act on what they see — is broken at exactly
the step after the reveal.

This is the same class HANDOFF.md lesson 6 names: a Jinja template renders a
field the data it was handed does not have, silently (an empty string, not a
crash), so nothing before the browser ever sees it.
"""

from __future__ import annotations

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


def _reveal(
    monkeypatch: Any, client: Any, resume_id: Any, *, withdrawn_at: str | None = None
) -> Any:
    """Mint the GET page's reveal token, then POST /reveal — mirroring the
    exact sequence a recruiter performs: open the résumé, click Reveal. Returns
    the Flask test response for the reveal POST (the just-revealed page)."""
    get_payload = _resume(resume_id, blinded=True, withdrawn_at=withdrawn_at)
    monkeypatch.setattr(api_client, "get_resume", MagicMock(return_value=get_payload))
    page = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    reveal_token = _extract_form_token(page, "/reveal")

    reveal_payload = _resume(
        resume_id, blinded=False, with_pii=True, withdrawn_at=withdrawn_at
    )
    monkeypatch.setattr(
        api_client, "reveal_resume", MagicMock(return_value=reveal_payload)
    )
    return client.post(
        f"/resumes/{resume_id}/reveal", data={"csrf_token": reveal_token}
    )


# ------------------------------------------------- every audited form is live


def test_every_audited_form_carries_a_nonempty_token_after_reveal(
    monkeypatch: Any, client: Any
) -> None:
    """THE structural pin. Immediately after a reveal, every audited form on
    the (now-revealed) page must carry a real, non-empty one-shot token — an
    empty one is indistinguishable from "no form at all" to a recruiter, and
    is rejected exactly the same way (403) the instant they click Save."""
    resume_id = uuid4()
    resp = _reveal(monkeypatch, client, resume_id)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    work_auth_token = _extract_form_token(body, "/work-authorization")
    withdraw_token = _extract_form_token(body, "/withdraw")
    document_token = _extract_form_token(body, "/document")

    assert work_auth_token, "the work-authorization form's token is empty post-reveal"
    assert withdraw_token, "the withdraw form's token is empty post-reveal"
    assert document_token, "the document-download form's token is empty post-reveal"
    assert work_auth_token != withdraw_token, (
        "the work-authorization and withdraw forms are sharing a token slot — "
        "using one would silently invalidate the other"
    )


def test_reinstate_form_carries_a_nonempty_token_after_reveal_of_a_withdrawn_resume(
    monkeypatch: Any, client: Any
) -> None:
    """Same defect, the withdrawn-candidate branch: the reinstate form (not
    the withdraw form) is the one rendered, and it is equally broken."""
    resume_id = uuid4()
    resp = _reveal(monkeypatch, client, resume_id, withdrawn_at="2026-07-20T00:00:00Z")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    reinstate_token = _extract_form_token(body, "/reinstate")
    assert reinstate_token, "the reinstate form's token is empty post-reveal"


# ---------------------------------------- the exact user-reported sequence


def test_setting_work_authorization_right_after_a_reveal_succeeds(
    monkeypatch: Any, client: Any
) -> None:
    """THE reproduction. Reveal, then — on that SAME rendered page, with no
    intervening reload — submit the work-authorization declaration using the
    token that page actually rendered. The live product 403s here; this must
    succeed (302 + the backend call made) once fixed."""
    resume_id = uuid4()
    reveal_resp = _reveal(monkeypatch, client, resume_id)
    assert reveal_resp.status_code == 200
    body = reveal_resp.get_data(as_text=True)
    work_auth_token = _extract_form_token(body, "/work-authorization")

    spy = MagicMock(return_value=_resume(resume_id, blinded=False, with_pii=True))
    monkeypatch.setattr(api_client, "set_work_authorization", spy)

    resp = client.post(
        f"/resumes/{resume_id}/work-authorization",
        data={"csrf_token": work_auth_token, "status": "eligible"},
    )
    assert resp.status_code == 302, (
        f"expected the redirect a successful save produces, got "
        f"{resp.status_code} — the reveal-then-save sequence is broken"
    )
    assert resp.headers["Location"].endswith(f"/resumes/{resume_id}")
    spy.assert_called_once()
    assert spy.call_args.args[0] == resume_id
    assert spy.call_args.kwargs.get("status") == "eligible"


def test_withdrawing_right_after_a_reveal_succeeds(
    monkeypatch: Any, client: Any
) -> None:
    """The same defect on the OTHER audited action that sits beside reveal on
    the page — withdraw. Reveal, then withdraw using the token the just-
    revealed page rendered."""
    resume_id = uuid4()
    reveal_resp = _reveal(monkeypatch, client, resume_id)
    assert reveal_resp.status_code == 200
    body = reveal_resp.get_data(as_text=True)
    withdraw_token = _extract_form_token(body, "/withdraw")

    spy = MagicMock(return_value=_resume(resume_id, blinded=False, with_pii=True))
    monkeypatch.setattr(api_client, "withdraw_resume", spy)

    resp = client.post(
        f"/resumes/{resume_id}/withdraw",
        data={"csrf_token": withdraw_token, "reason": "Accepted another offer"},
    )
    assert resp.status_code == 302, (
        f"expected the redirect a successful withdrawal produces, got "
        f"{resp.status_code} — the reveal-then-withdraw sequence is broken"
    )
    spy.assert_called_once()
    assert spy.call_args.args[0] == resume_id


def test_reinstating_right_after_a_reveal_of_a_withdrawn_resume_succeeds(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    reveal_resp = _reveal(
        monkeypatch, client, resume_id, withdrawn_at="2026-07-20T00:00:00Z"
    )
    assert reveal_resp.status_code == 200
    body = reveal_resp.get_data(as_text=True)
    reinstate_token = _extract_form_token(body, "/reinstate")

    spy = MagicMock(return_value=_resume(resume_id, blinded=False, with_pii=True))
    monkeypatch.setattr(api_client, "reinstate_resume", spy)

    resp = client.post(
        f"/resumes/{resume_id}/reinstate", data={"csrf_token": reinstate_token}
    )
    assert resp.status_code == 302, (
        f"expected the redirect a successful reinstatement produces, got "
        f"{resp.status_code} — the reveal-then-reinstate sequence is broken"
    )
    spy.assert_called_once()
