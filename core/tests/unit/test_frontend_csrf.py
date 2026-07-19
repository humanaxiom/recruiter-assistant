"""Unit tests for ``frontend.csrf`` (FU-4/D4, amended).

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

**Amendment (this file) — tokens are scoped PER RÉSUMÉ ID, not per session.**
The shipped v1 design stored a single bare token in the Flask session. The
FU-1 reveal button appears on EVERY shortlist card, all posting to the SAME
``resume_reveal`` route: minting one card's token silently invalidated every
OTHER card's already-rendered token (only the most-recently-minted token was
ever valid, session-wide), so the first reveal a recruiter clicked worked and
every other card's reveal 403'd until a full page reload re-minted a fresh
single token. Reveal-from-shortlist is the primary FU-1 workflow, so this was
a functional regression, not a cosmetic one.

**The contract this file locks (the coder implements exactly this shape):**

* ``SESSION_KEY: str`` — the Flask session dict key under which the current
  mapping of ``resume_id -> token`` is stored (Flask's EXISTING signed
  session, already used for ``flash()`` — ``core/frontend/app.py`` sets
  ``app.secret_key`` from ``settings.flask_secret_key``). The stored value is
  a MAPPING (JSON-object-shaped, since the signed session is JSON-serialized
  — so keys are strings, e.g. ``str(resume_id)``), never a bare token string.
* ``FORM_FIELD: str`` — MUST equal ``"csrf_token"``. This is the form field
  name ``resume_detail.html``'s and ``shortlist_cards.html``'s reveal
  ``<form>``s render as a hidden input, and the field name
  ``frontend.app.resume_reveal`` reads from ``request.form``.
* ``MAX_TOKENS_PER_SESSION: int`` — MUST equal ``64``. Bounds the mapping so
  the signed session cookie cannot grow unboundedly across a long browsing
  session that visits many résumé/shortlist pages. Shortlist rows are
  structurally capped at the stage-1 ``k=50`` oversample (ADR-012 SEC-3), so a
  single shortlist render mints at most 50 tokens; 64 clears that with
  comfortable headroom while a rendered shortlist page still fits well inside
  the ~4KB cookie ceiling. When minting a token would push the mapping over
  this cap, the OLDEST entry (by insertion/issue order) is evicted first.
* ``issue_token(resume_id: Any) -> str`` — call inside an active Flask
  request context. Generates a token with ``secrets.token_urlsafe`` (imported
  as ``import secrets`` — NOT ``from secrets import token_urlsafe``, so tests
  can monkeypatch ``csrf.secrets.token_urlsafe``), stores it in the
  ``flask.session[SESSION_KEY]`` mapping under ``resume_id`` (overwriting any
  previously-issued, unconsumed token for that SAME ``resume_id`` — tokens for
  OTHER résumé ids are left untouched), evicting the oldest entry first if the
  mapping would exceed ``MAX_TOKENS_PER_SESSION``, and returns the new token.
  Never uses the ``random`` module.
* ``verify_and_consume(resume_id: Any, submitted: str | None) -> bool`` —
  call inside an active Flask request context. POPS ONLY ``resume_id``'s
  entry out of the ``flask.session[SESSION_KEY]`` mapping UNCONDITIONALLY (so
  the stored token is a strict one-shot PER RÉSUMÉ: this call invalidates
  that résumé's token whether or not ``submitted`` matches, but leaves every
  OTHER résumé's token untouched). Returns ``True`` iff a token was stored
  for ``resume_id`` AND ``submitted`` is truthy AND
  ``secrets.compare_digest(stored, submitted)`` is ``True`` (imported the
  same ``import secrets`` way, so tests can monkeypatch
  ``csrf.secrets.compare_digest``). Returns ``False`` in every other case
  (no stored token for this ``resume_id``, ``submitted`` is ``None``/empty, a
  mismatch, or — the new cross-résumé-confusion case — ``submitted`` is a
  token that was validly minted but for a DIFFERENT ``resume_id``) — never
  raises.
* ``same_origin(req: Any) -> bool`` — UNCHANGED by this amendment.
  Defense-in-depth, evaluated INDEPENDENTLY of the token. Compares the
  request's own origin (``req.host_url``) against an ``Origin`` header if
  present, else a ``Referer`` header if present. Returns ``False`` (blocks)
  iff a cross-origin ``Origin``/``Referer`` is present. Returns ``True``
  (does not block) when the header is absent entirely OR it matches — the
  token stays the PRIMARY control; this is a secondary layer, not a
  replacement.

``frontend.app`` wires this in as follows (asserted indirectly through
``test_frontend_resume_detail.py``'s route-level tests, not here):

* ``GET /resumes/<id>`` calls ``csrf.issue_token(resume_id)`` and passes the
  result to ``resume_detail.html`` as ``csrf_token``, rendered as a hidden
  ``<input name="csrf_token" value="...">`` inside THAT résumé's reveal
  ``<form>``.
* Each shortlist card in ``shortlist_cards.html`` similarly carries its OWN
  token, minted for its OWN ``entry.resume_id`` — so many cards on one
  render each get an independently valid, independently one-shot token.
* ``POST /resumes/<id>/reveal`` first checks ``csrf.same_origin(request)``
  (``abort(403)`` if it fails, WITHOUT ever calling
  ``csrf.verify_and_consume`` or ``api_client.reveal_resume``), then checks
  ``csrf.verify_and_consume(resume_id, request.form.get(csrf.FORM_FIELD))``
  (``abort(403)`` if it fails, WITHOUT ever calling
  ``api_client.reveal_resume``). Only if both pass does it call
  ``api_client.reveal_resume(...)``.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any
from uuid import uuid4

import flask

from frontend import csrf
from frontend.app import app

# ── module contract: constants ────────────────────────────────────────────


def test_module_exposes_session_key_and_form_field_constants() -> None:
    assert isinstance(csrf.SESSION_KEY, str) and csrf.SESSION_KEY
    assert isinstance(csrf.FORM_FIELD, str) and csrf.FORM_FIELD


def test_form_field_constant_is_csrf_token() -> None:
    """Pinned literally: ``resume_detail.html``, ``shortlist_cards.html`` and
    the route-level tests in ``test_frontend_resume_detail.py`` all hardcode
    the field name ``csrf_token``."""
    assert csrf.FORM_FIELD == "csrf_token"


def test_max_tokens_per_session_constant_is_sixty_four() -> None:
    """The chosen cap: shortlist rows are structurally capped at the stage-1
    ``k=50`` oversample (ADR-012 SEC-3, "bounded in practice by shortlist
    size"), so a single shortlist render mints at most 50 tokens into one
    session. 64 clears that with headroom for a couple of OTHER open résumé
    tabs in the same browser session, while still comfortably bounding the
    signed session cookie's size."""
    assert isinstance(csrf.MAX_TOKENS_PER_SESSION, int)
    assert csrf.MAX_TOKENS_PER_SESSION == 64


# ── issue_token ────────────────────────────────────────────────────────────


def test_issue_token_returns_a_string() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        assert isinstance(token, str)
        assert len(token) >= 16


def test_session_holds_a_mapping_keyed_by_resume_id_not_a_bare_token() -> None:
    """Pins the new contract: the session value under ``SESSION_KEY`` is a
    MAPPING of ``resume_id -> token``, not a single bare token string (the
    shape that caused the one-token-per-session regression this change
    fixes)."""
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        stored = flask.session[csrf.SESSION_KEY]
        assert isinstance(stored, dict)
        assert stored[str(resume_id)] == token


def test_issue_token_differs_across_repeated_calls_for_same_resume() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        first = csrf.issue_token(resume_id)
        second = csrf.issue_token(resume_id)
    assert first != second


def test_issue_token_for_one_resume_does_not_disturb_another_resumes_token() -> None:
    """The crux of this amendment: minting résumé B's token must not
    overwrite or invalidate résumé A's still-unconsumed token."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        csrf.issue_token(resume_b)
        assert csrf.verify_and_consume(resume_a, token_a) is True


def test_issue_token_uses_secrets_token_urlsafe(monkeypatch: Any) -> None:
    calls: list[int] = []
    real_token_urlsafe = secrets.token_urlsafe

    def spy(nbytes: int = 32) -> str:
        calls.append(nbytes)
        return real_token_urlsafe(nbytes)

    monkeypatch.setattr(csrf.secrets, "token_urlsafe", spy)
    with app.test_request_context():
        csrf.issue_token(uuid4())
    assert calls, "issue_token() must generate its token via secrets.token_urlsafe"
    assert calls[0] >= 16, "token entropy must be meaningful, not a token of length 1"


# ── verify_and_consume: basic one-shot behaviour, now scoped per résumé ────


def test_verify_and_consume_succeeds_with_the_matching_token_for_that_resume() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        assert csrf.verify_and_consume(resume_id, token) is True


def test_verify_and_consume_removes_only_that_resumes_entry_from_the_mapping() -> None:
    """One-shot, per résumé: after ANY call to ``verify_and_consume`` for a
    given ``resume_id``, that résumé's stored token must be gone — whether or
    not the call succeeded — but OTHER résumés' entries are untouched."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        token_b = csrf.issue_token(resume_b)
        csrf.verify_and_consume(resume_a, token_a)
        mapping = flask.session[csrf.SESSION_KEY]
        assert str(resume_a) not in mapping
        assert mapping[str(resume_b)] == token_b


def test_verify_and_consume_is_single_use_a_second_call_with_the_same_value_fails() -> (
    None
):
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        first = csrf.verify_and_consume(resume_id, token)
        second = csrf.verify_and_consume(resume_id, token)
    assert first is True
    assert second is False


def test_verify_and_consume_fails_on_wrong_token_and_still_consumes_the_slot() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_id)
        result = csrf.verify_and_consume(resume_id, "not-the-right-token")
        assert result is False
        assert str(resume_id) not in flask.session[csrf.SESSION_KEY]


def test_verify_and_consume_fails_when_no_token_was_ever_issued_for_that_resume() -> (
    None
):
    with app.test_request_context():
        assert csrf.verify_and_consume(uuid4(), "anything") is False


def test_verify_and_consume_fails_for_resume_with_no_token_if_others_have_one() -> None:
    resume_with_token, resume_without = uuid4(), uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_with_token)
        assert csrf.verify_and_consume(resume_without, "anything") is False


def test_verify_and_consume_fails_on_none_submitted() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_id)
        assert csrf.verify_and_consume(resume_id, None) is False


def test_verify_and_consume_fails_on_empty_string_submitted() -> None:
    resume_id = uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_id)
        assert csrf.verify_and_consume(resume_id, "") is False


def test_verify_and_consume_uses_secrets_compare_digest(monkeypatch: Any) -> None:
    calls: list[tuple[Any, Any]] = []
    real_compare_digest = secrets.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return bool(real_compare_digest(a, b))

    monkeypatch.setattr(csrf.secrets, "compare_digest", spy)
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        csrf.verify_and_consume(resume_id, token)
    assert calls, "verify_and_consume() must compare via secrets.compare_digest"


# ── verify_and_consume: cross-résumé confusion (new risk this change adds) ──


def test_token_minted_for_one_resume_does_not_validate_a_different_resume() -> None:
    """The new confusion risk this amendment introduces: with a per-résumé
    mapping, a token minted for résumé A must NOT validate a reveal of
    résumé B, even though both tokens live in the same session at once."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        assert csrf.verify_and_consume(resume_b, token_a) is False


def test_misdirected_verify_does_not_consume_the_original_resumes_slot() -> None:
    """A misdirected verify attempt (right token, wrong résumé id) must not
    burn the token's rightful résumé's slot — it simply doesn't match that
    lookup key at all, so résumé A's real token is still there afterwards."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        assert csrf.verify_and_consume(resume_b, token_a) is False
        # résumé A's own token is unaffected and still verifies correctly.
        assert csrf.verify_and_consume(resume_a, token_a) is True


def test_revealing_one_resume_does_not_invalidate_a_sibling_resumes_token() -> None:
    """One-shot is now per-résumé, not per-session: consuming résumé A's
    token via a full (successful) verify must not touch résumé B's token."""
    resume_a, resume_b = uuid4(), uuid4()
    with app.test_request_context():
        token_a = csrf.issue_token(resume_a)
        token_b = csrf.issue_token(resume_b)
        assert csrf.verify_and_consume(resume_a, token_a) is True
        assert csrf.verify_and_consume(resume_b, token_b) is True


def test_replaying_a_consumed_resumes_token_still_fails_after_amendment() -> None:
    """Even though one-shot is now per résumé rather than per session, replay
    protection for that SAME résumé is unchanged: a consumed token can never
    be reused."""
    resume_id = uuid4()
    with app.test_request_context():
        token = csrf.issue_token(resume_id)
        assert csrf.verify_and_consume(resume_id, token) is True
        assert csrf.verify_and_consume(resume_id, token) is False


# ── mapping size cap / eviction ─────────────────────────────────────────────


def test_issuing_tokens_beyond_the_cap_keeps_the_mapping_bounded() -> None:
    resume_ids = [uuid4() for _ in range(csrf.MAX_TOKENS_PER_SESSION + 5)]
    with app.test_request_context():
        for resume_id in resume_ids:
            csrf.issue_token(resume_id)
        mapping = flask.session[csrf.SESSION_KEY]
        assert len(mapping) <= csrf.MAX_TOKENS_PER_SESSION


def test_issuing_tokens_beyond_the_cap_evicts_the_oldest_first() -> None:
    resume_ids = [uuid4() for _ in range(csrf.MAX_TOKENS_PER_SESSION + 5)]
    with app.test_request_context():
        tokens = {resume_id: csrf.issue_token(resume_id) for resume_id in resume_ids}
        # The five oldest are gone...
        for evicted in resume_ids[:5]:
            assert csrf.verify_and_consume(evicted, tokens[evicted]) is False
        # ...but the most recently issued MAX_TOKENS_PER_SESSION are intact.
        for kept in resume_ids[5:]:
            assert csrf.verify_and_consume(kept, tokens[kept]) is True


def test_cap_eviction_does_not_break_a_freshly_issued_token() -> None:
    """Filling the mapping past its cap must not corrupt or block minting
    (and validating) a brand-new token afterwards."""
    resume_ids = [uuid4() for _ in range(csrf.MAX_TOKENS_PER_SESSION + 10)]
    fresh_resume_id = uuid4()
    with app.test_request_context():
        for resume_id in resume_ids:
            csrf.issue_token(resume_id)
        fresh_token = csrf.issue_token(fresh_resume_id)
        assert csrf.verify_and_consume(fresh_resume_id, fresh_token) is True


def test_re_issuing_a_token_for_the_same_resume_does_not_grow_the_mapping() -> None:
    """Re-minting résumé A's token twice (e.g. two GETs of the same résumé
    page in one session) must overwrite its existing slot, not add a second
    entry — otherwise a recruiter re-visiting the same handful of résumé
    pages could exhaust the cap and evict unrelated cards' tokens."""
    resume_id = uuid4()
    with app.test_request_context():
        csrf.issue_token(resume_id)
        csrf.issue_token(resume_id)
        mapping = flask.session[csrf.SESSION_KEY]
        assert len(mapping) == 1


# ── same_origin (defense-in-depth) — UNCHANGED by this amendment ───────────


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
