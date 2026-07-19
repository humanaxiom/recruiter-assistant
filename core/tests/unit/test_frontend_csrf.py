"""Unit tests for ``frontend.csrf`` (new module; does not exist yet). The
whole file fails at collection (``ModuleNotFoundError``) — RED half of the
TDD cycle, mirroring the established pattern in ``test_frontend_api_client.py``
for a not-yet-existent module.

**FU-4/D4 — the threat this module closes.** Classic CSRF does NOT apply to
the FastAPI backend: it authenticates on the ``X-API-Key`` header, and a
cross-origin ``<form>`` cannot attach a custom header — so a forged
cross-site POST straight at the backend is simply rejected as unauthenticated.
The real gap is the Flask hop: the browser supplies NO credential of its own
for ``POST /resumes/<id>/reveal`` (``core/frontend/app.py``) — Flask attaches
its own server-held recruiter key on the OUTBOUND leg to the backend
(``api_client.build_client``) — so Flask itself cannot distinguish a forged
cross-site auto-submit from a real click. This module is the fix: a
session-bound, one-shot anti-forgery token (NOT a login — it carries no
identity), plus an ``Origin``/``Referer`` same-origin check as
defense-in-depth layered ON TOP of the token, never instead of it.

**The contract this file locks (the coder implements exactly this shape):**

* ``SESSION_KEY: str`` — the Flask session dict key under which the current
  one-shot token is stored (Flask's EXISTING signed session, already used for
  ``flash()`` — ``core/frontend/app.py`` sets ``app.secret_key`` from
  ``settings.flask_secret_key``).
* ``FORM_FIELD: str`` — MUST equal ``"csrf_token"``. This is the form field
  name ``resume_detail.html``'s reveal ``<form>`` renders as a hidden input,
  and the field name ``frontend.app.resume_reveal`` reads from
  ``request.form``.
* ``issue_token() -> str`` — call inside an active Flask request context.
  Generates a token with ``secrets.token_urlsafe`` (imported as ``import
  secrets`` — NOT ``from secrets import token_urlsafe``, so tests can
  monkeypatch ``csrf.secrets.token_urlsafe``), stores it under
  ``flask.session[SESSION_KEY]`` (overwriting any previously-issued,
  unconsumed token — only the most-recently-issued token for a session is
  ever valid), and returns it. Never uses the ``random`` module.
* ``verify_and_consume(submitted: str | None) -> bool`` — call inside an
  active Flask request context. POPS ``SESSION_KEY`` out of
  ``flask.session`` UNCONDITIONALLY (so the stored token is a strict
  one-shot: this call invalidates it whether or not ``submitted`` matches).
  Returns ``True`` iff a token was stored AND ``submitted`` is truthy AND
  ``secrets.compare_digest(stored, submitted)`` is ``True`` (imported the
  same ``import secrets`` way, so tests can monkeypatch
  ``csrf.secrets.compare_digest``). Returns ``False`` in every other case
  (no stored token, ``submitted`` is ``None``/empty, or a mismatch) —
  never raises.
* ``same_origin(req: Any) -> bool`` — defense-in-depth, evaluated
  INDEPENDENTLY of the token. Compares the request's own origin
  (``req.host_url``) against an ``Origin`` header if present, else a
  ``Referer`` header if present. Returns ``False`` (blocks) iff a
  cross-origin ``Origin``/``Referer`` is present. Returns ``True`` (does not
  block) when the header is absent entirely OR it matches — the token stays
  the PRIMARY control; this is a secondary layer, not a replacement.

``frontend.app`` wires this in as follows (asserted indirectly through
``test_frontend_resume_detail.py``'s route-level tests, not here):

* ``GET /resumes/<id>`` calls ``csrf.issue_token()`` and passes it to
  ``resume_detail.html`` as ``csrf_token``, rendered as a hidden
  ``<input name="csrf_token" value="...">`` inside the reveal ``<form>``.
* ``POST /resumes/<id>/reveal`` first checks ``csrf.same_origin(request)``
  (``abort(403)`` if it fails, WITHOUT ever calling
  ``csrf.verify_and_consume`` or ``api_client.reveal_resume``), then checks
  ``csrf.verify_and_consume(request.form.get(csrf.FORM_FIELD))``
  (``abort(403)`` if it fails, WITHOUT ever calling
  ``api_client.reveal_resume``). Only if both pass does it call
  ``api_client.reveal_resume(...)``.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import flask

from frontend import csrf
from frontend.app import app

# ── module contract: constants ────────────────────────────────────────────


def test_module_exposes_session_key_and_form_field_constants() -> None:
    assert isinstance(csrf.SESSION_KEY, str) and csrf.SESSION_KEY
    assert isinstance(csrf.FORM_FIELD, str) and csrf.FORM_FIELD


def test_form_field_constant_is_csrf_token() -> None:
    """Pinned literally: ``resume_detail.html`` and the route-level tests in
    ``test_frontend_resume_detail.py`` both hardcode the field name
    ``csrf_token``."""
    assert csrf.FORM_FIELD == "csrf_token"


# ── issue_token ────────────────────────────────────────────────────────────


def test_issue_token_returns_a_string_and_stores_it_in_the_session() -> None:
    with app.test_request_context():
        token = csrf.issue_token()
        assert isinstance(token, str)
        assert len(token) >= 16
        assert flask.session[csrf.SESSION_KEY] == token


def test_issue_token_returns_different_tokens_on_repeated_calls() -> None:
    with app.test_request_context():
        first = csrf.issue_token()
        second = csrf.issue_token()
    assert first != second


def test_issue_token_uses_secrets_token_urlsafe(monkeypatch: Any) -> None:
    calls: list[int] = []
    real_token_urlsafe = secrets.token_urlsafe

    def spy(nbytes: int = 32) -> str:
        calls.append(nbytes)
        return real_token_urlsafe(nbytes)

    monkeypatch.setattr(csrf.secrets, "token_urlsafe", spy)
    with app.test_request_context():
        csrf.issue_token()
    assert calls, "issue_token() must generate its token via secrets.token_urlsafe"
    assert calls[0] >= 16, "token entropy must be meaningful, not a token of length 1"


# ── verify_and_consume ──────────────────────────────────────────────────────


def test_verify_and_consume_succeeds_with_the_matching_token() -> None:
    with app.test_request_context():
        token = csrf.issue_token()
        assert csrf.verify_and_consume(token) is True


def test_verify_and_consume_removes_the_token_from_the_session() -> None:
    """One-shot: after ANY call to verify_and_consume, the stored token must be
    gone from the session — whether or not the call succeeded."""
    with app.test_request_context():
        token = csrf.issue_token()
        csrf.verify_and_consume(token)
        assert csrf.SESSION_KEY not in flask.session


def test_verify_and_consume_is_single_use_a_second_call_with_the_same_value_fails() -> (
    None
):
    with app.test_request_context():
        token = csrf.issue_token()
        first = csrf.verify_and_consume(token)
        second = csrf.verify_and_consume(token)
    assert first is True
    assert second is False


def test_verify_and_consume_fails_on_a_wrong_token_and_still_consumes_it() -> None:
    with app.test_request_context():
        csrf.issue_token()
        result = csrf.verify_and_consume("not-the-right-token")
        assert result is False
        assert csrf.SESSION_KEY not in flask.session


def test_verify_and_consume_fails_when_no_token_was_ever_issued() -> None:
    with app.test_request_context():
        assert csrf.verify_and_consume("anything") is False


def test_verify_and_consume_fails_on_none_submitted() -> None:
    with app.test_request_context():
        csrf.issue_token()
        assert csrf.verify_and_consume(None) is False


def test_verify_and_consume_fails_on_empty_string_submitted() -> None:
    with app.test_request_context():
        csrf.issue_token()
        assert csrf.verify_and_consume("") is False


def test_verify_and_consume_uses_secrets_compare_digest(monkeypatch: Any) -> None:
    calls: list[tuple[Any, Any]] = []
    real_compare_digest = secrets.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return bool(real_compare_digest(a, b))

    monkeypatch.setattr(csrf.secrets, "compare_digest", spy)
    with app.test_request_context():
        token = csrf.issue_token()
        csrf.verify_and_consume(token)
    assert calls, "verify_and_consume() must compare via secrets.compare_digest"


# ── same_origin (defense-in-depth) ──────────────────────────────────────────


def test_same_origin_true_when_origin_header_matches_the_request_host() -> None:
    with app.test_request_context(headers={"Origin": "http://localhost"}):
        assert csrf.same_origin(flask.request) is True


def test_same_origin_false_when_origin_header_is_cross_site() -> None:
    with app.test_request_context(headers={"Origin": "http://evil.example"}):
        assert csrf.same_origin(flask.request) is False


def test_same_origin_true_when_referer_matches_and_origin_is_absent() -> None:
    with app.test_request_context(headers={"Referer": "http://localhost/resumes/x"}):
        assert csrf.same_origin(flask.request) is True


def test_same_origin_false_when_referer_is_cross_site_and_origin_is_absent() -> None:
    with app.test_request_context(
        headers={"Referer": "http://evil.example/attack.html"}
    ):
        assert csrf.same_origin(flask.request) is False


def test_same_origin_true_when_neither_header_is_present() -> None:
    """The token stays the PRIMARY control; a same-origin request that omits
    both headers (never actually happens for a genuine cross-site forged POST,
    which browsers always tag with `Origin`) is not blocked by this
    secondary layer alone."""
    with app.test_request_context():
        assert csrf.same_origin(flask.request) is True


def test_same_origin_prefers_origin_over_referer_when_both_present() -> None:
    with app.test_request_context(
        headers={
            "Origin": "http://evil.example",
            "Referer": "http://localhost/resumes/x",
        }
    ):
        assert csrf.same_origin(flask.request) is False


# ── structural guard: no `random` module, the right primitives are used ────


def test_csrf_module_never_imports_the_random_module() -> None:
    src = Path(csrf.__file__).read_text(encoding="utf-8")
    assert "import random" not in src
    assert "from random" not in src


def test_csrf_module_uses_token_urlsafe_and_compare_digest_by_name() -> None:
    src = Path(csrf.__file__).read_text(encoding="utf-8")
    assert "secrets.token_urlsafe(" in src
    assert "secrets.compare_digest(" in src
