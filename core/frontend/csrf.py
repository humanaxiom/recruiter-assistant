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

The fix is a session-bound, ONE-SHOT token, scoped PER RÉSUMÉ ID. It carries no
identity — this is not a login — it only proves the submitting page was
rendered by us, for this browser session. It lives in Flask's EXISTING signed
session (``app.secret_key`` from ``settings.flask_secret_key``), so it cannot be
forged or read cross-origin.

Tokens are keyed by résumé id because the FU-1 reveal button appears on EVERY
shortlist card, all posting to the SAME ``resume_reveal`` route: with a single
per-session token, minting one card's token invalidated every other card's, so
only the first reveal a recruiter clicked worked. Per-résumé scoping also means
a token minted for résumé A can never validate a reveal of résumé B.

:func:`same_origin` is layered ON TOP of the token as defense-in-depth, never
instead of it: it blocks only when a cross-origin ``Origin``/``Referer`` is
actually present, and stays silent when neither header exists.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlsplit

import flask

#: Flask-session key holding the ``resume_id -> token`` mapping.
SESSION_KEY = "_csrf_token"

#: Form field name rendered as a hidden input and read from ``request.form``.
FORM_FIELD = "csrf_token"

#: Upper bound on how many per-résumé tokens one session may hold at once.
#: A shortlist render mints one token per card and is structurally capped at
#: the stage-1 ``k=50`` oversample (ADR-012 SEC-3), so 64 clears a full
#: shortlist page with headroom for a couple of other open résumé tabs while
#: still bounding the signed session cookie well inside the ~4KB ceiling.
MAX_TOKENS_PER_SESSION = 64

#: Entropy of a minted token, in bytes (URL-safe base64 expands this ~1.3x).
_TOKEN_BYTES = 32


def _mapping() -> dict[str, str]:
    """Return the session's ``resume_id -> token`` mapping, coercing junk.

    A session written by the previous per-session design held a bare token
    string under ``SESSION_KEY``; anything that is not a ``str -> str`` mapping
    is discarded rather than raising, so an old cookie simply mints afresh.
    """
    stored = flask.session.get(SESSION_KEY)
    if not isinstance(stored, dict):
        return {}
    return {
        k: v for k, v in stored.items() if isinstance(k, str) and isinstance(v, str)
    }


def issue_token(resume_id: Any) -> str:
    """Mint a fresh token for ``resume_id``, store it and return it.

    Overwrites any previously-issued, unconsumed token for the SAME résumé,
    but leaves every other résumé's token untouched — the shortlist renders
    one reveal form per card, all posting to the same route, so each card needs
    its own independently-valid token. When adding a NEW résumé would push the
    mapping past :data:`MAX_TOKENS_PER_SESSION`, the oldest entry is evicted
    first (strict FIFO by issue order; re-issuing for an existing résumé keeps
    that résumé's original position rather than promoting it). Must be called
    inside an active Flask request context.
    """
    key = str(resume_id)
    mapping = _mapping()
    if key not in mapping:
        while len(mapping) >= MAX_TOKENS_PER_SESSION:
            oldest = next(iter(mapping))
            del mapping[oldest]
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    mapping[key] = token
    # Reassign rather than mutate in place so Flask marks the session dirty.
    flask.session[SESSION_KEY] = mapping
    return token


def verify_and_consume(resume_id: Any, submitted: str | None) -> bool:
    """Return ``True`` iff ``submitted`` matches ``resume_id``'s stored token.

    ``resume_id``'s entry is popped UNCONDITIONALLY — a wrong-token attempt
    burns that résumé's slot just as a correct one does, so the token is a
    strict one-shot per résumé and an attacker cannot probe it by replaying
    guesses against a live slot. Other résumés' entries are left intact, so a
    misdirected attempt (résumé A's token posted at résumé B) fails without
    burning A's rightful slot. Never raises: a missing, empty or non-ASCII
    submission is simply ``False``.
    """
    key = str(resume_id)
    mapping = _mapping()
    stored = mapping.pop(key, None)
    flask.session[SESSION_KEY] = mapping
    if not stored or not submitted:
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
    "MAX_TOKENS_PER_SESSION",
    "issue_token",
    "verify_and_consume",
    "same_origin",
]
