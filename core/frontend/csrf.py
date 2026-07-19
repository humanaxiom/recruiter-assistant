"""Anti-forgery token for the Flask viewer's state-changing reveal form (FU-4/D4).

Classic CSRF does NOT apply to the FastAPI backend: it authenticates on the
``X-API-Key`` header and a cross-origin ``<form>`` cannot attach a custom
header, so a forged cross-site POST aimed straight at the backend is simply
rejected as unauthenticated. The real gap is the Flask hop — the browser
supplies no credential of its own for ``POST /resumes/<id>/reveal``; Flask
attaches its own server-held recruiter key on the OUTBOUND leg
(:func:`frontend.api_client.build_client`), so Flask itself cannot distinguish
a forged cross-site auto-submit from a genuine click. Left unguarded, a forged
POST would produce a real, attributable ``reveal_audit`` row.

The fix is a session-bound, ONE-SHOT token. It carries no identity — this is
not a login — it only proves the submitting page was rendered by us, for this
browser session. It lives in Flask's EXISTING signed session (``app.secret_key``
from ``settings.flask_secret_key``), so it cannot be forged or read
cross-origin.

:func:`same_origin` is layered ON TOP of the token as defense-in-depth, never
instead of it: it blocks only when a cross-origin ``Origin``/``Referer`` is
actually present, and stays silent when neither header exists.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlsplit

import flask

#: Flask-session key holding the current one-shot token.
SESSION_KEY = "_csrf_token"

#: Form field name rendered as a hidden input and read from ``request.form``.
FORM_FIELD = "csrf_token"

#: Entropy of a minted token, in bytes (URL-safe base64 expands this ~1.3x).
_TOKEN_BYTES = 32


def issue_token() -> str:
    """Mint a fresh token, store it in the session and return it.

    Overwrites any previously-issued, unconsumed token: only the most recently
    issued token for a session is ever valid. Must be called inside an active
    Flask request context.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    flask.session[SESSION_KEY] = token
    return token


def verify_and_consume(submitted: str | None) -> bool:
    """Return ``True`` iff ``submitted`` matches the session's stored token.

    The stored token is popped UNCONDITIONALLY — a wrong-token attempt burns
    the slot just as a correct one does, so the token is a strict one-shot and
    an attacker cannot probe it by replaying guesses against a live slot.
    Never raises: a missing, empty or non-ASCII submission is simply ``False``.
    """
    stored = flask.session.pop(SESSION_KEY, None)
    if not isinstance(stored, str) or not stored or not submitted:
        return False
    # Compare UTF-8 bytes: `compare_digest` raises on non-ASCII `str` inputs,
    # and `submitted` is attacker-controlled form data.
    return secrets.compare_digest(stored.encode("utf-8"), submitted.encode("utf-8"))


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def same_origin(req: Any) -> bool:
    """Defense-in-depth same-origin check, evaluated independently of the token.

    Prefers ``Origin`` over ``Referer`` when both are present (``Origin`` is the
    header browsers attach to cross-site form posts and cannot be spoofed by
    page content). Returns ``False`` only when a *cross*-origin header is
    present; an absent header is NOT a block — the token remains the primary
    control.
    """
    expected = _origin_of(req.host_url)
    declared = req.headers.get("Origin") or req.headers.get("Referer")
    if not declared:
        return True
    return _origin_of(declared) == expected


__all__ = [
    "SESSION_KEY",
    "FORM_FIELD",
    "issue_token",
    "verify_and_consume",
    "same_origin",
]
