"""Unit tests for `fix/serve-behind-tls-proxy` — the app will be served at
https://sfuai.ca behind an nginx TLS-terminating reverse proxy, and Flask's
own ``request.host_url`` (which ``frontend.csrf.same_origin`` compares the
browser's ``Origin``/``Referer`` against, ~csrf.py line 304) is wrong behind
that proxy unless the forwarded headers are explicitly trusted.

Neither ``frontend.app._install_proxy_fix`` nor ``_configure_session_cookie``
exists yet — this whole file is RED at collection (``AttributeError``) until
the coder adds them, per the task's design:

* ``_install_proxy_fix(flask_app: Flask, settings) -> None`` — wraps
  ``flask_app.wsgi_app`` in ``werkzeug.middleware.proxy_fix.ProxyFix`` with
  ``x_for=x_proto=x_host=settings.proxy_hops, x_prefix=0`` when
  ``settings.trust_proxy_headers`` is True; leaves ``wsgi_app`` untouched
  otherwise.
* ``_configure_session_cookie(flask_app: Flask, settings) -> None`` — sets
  Flask's own signed-session-cookie ``SESSION_COOKIE_SECURE`` config key to
  ``settings.session_cookie_secure``.

**Why the spoof/honour tests use `test_client()`, not `test_request_context`:**
``ProxyFix`` is WSGI middleware wrapping ``app.wsgi_app`` — it only runs when
a request actually goes through the app's ``__call__``/WSGI entry point.
``test_request_context()`` builds a request context directly and never
invokes the wrapped ``wsgi_app``, so it cannot observe ProxyFix's effect
either way (a false negative dressed as a pass). ``test_client()`` drives the
real WSGI call chain.

Both fields' defaults (``trust_proxy_headers=False``, ``proxy_hops=1``) are
pinned in ``test_settings_cas.py``; this file pins only the install-time
wiring and its externally observable effect on ``same_origin``.
"""

from __future__ import annotations

import flask
import pytest
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from frontend import csrf
from frontend.app import _configure_session_cookie, _install_proxy_fix
from src.settings import Settings

# ── _install_proxy_fix ────────────────────────────────────────────────────


def test_install_proxy_fix_leaves_wsgi_app_untouched_when_trust_is_off() -> None:
    settings = Settings(trust_proxy_headers=False)
    flask_app = Flask(__name__)
    original_wsgi_app = flask_app.wsgi_app

    _install_proxy_fix(flask_app, settings)

    assert flask_app.wsgi_app is original_wsgi_app
    assert not isinstance(flask_app.wsgi_app, ProxyFix)


def test_install_proxy_fix_wraps_wsgi_app_in_proxy_fix_when_trust_is_on() -> None:
    settings = Settings(trust_proxy_headers=True, proxy_hops=1)
    flask_app = Flask(__name__)

    _install_proxy_fix(flask_app, settings)

    assert isinstance(flask_app.wsgi_app, ProxyFix)
    wrapped = flask_app.wsgi_app
    assert wrapped.x_for == 1
    assert wrapped.x_proto == 1
    assert wrapped.x_host == 1
    assert wrapped.x_prefix == 0


def test_install_proxy_fix_honours_configured_hop_count() -> None:
    settings = Settings(trust_proxy_headers=True, proxy_hops=2)
    flask_app = Flask(__name__)

    _install_proxy_fix(flask_app, settings)

    wrapped = flask_app.wsgi_app
    assert isinstance(wrapped, ProxyFix)
    assert wrapped.x_for == 2
    assert wrapped.x_proto == 2
    assert wrapped.x_host == 2
    assert wrapped.x_prefix == 0


# ── externally observable effect on same_origin, driven over real WSGI ────


def _probe_app() -> Flask:
    """A throwaway Flask app with one route that reports exactly what
    ``same_origin`` and ``host_url`` look like from INSIDE a real request —
    the same seam `frontend.csrf.same_origin` reads in production."""
    probe = Flask(__name__)

    @probe.route("/__probe")
    def _probe() -> str:  # pragma: no cover - exercised via test_client only
        return f"{csrf.same_origin(flask.request)} {flask.request.host_url}"

    return probe


_FORWARDED_HEADERS = {
    "X-Forwarded-Proto": "https",
    "X-Forwarded-Host": "sfuai.ca",
    "Origin": "https://sfuai.ca",
}


def test_spoofed_forwarded_headers_are_ignored_when_trust_is_off() -> None:
    """A malicious or misconfigured client can send X-Forwarded-* directly
    (there is no proxy between the test client and the app) — with trust
    OFF, Flask must not be fooled: host_url stays the raw WSGI origin and
    same_origin correctly reports a mismatch against the spoofed Origin."""
    settings = Settings(trust_proxy_headers=False)
    flask_app = _probe_app()
    _install_proxy_fix(flask_app, settings)
    client = flask_app.test_client()

    resp = client.get("/__probe", headers=_FORWARDED_HEADERS)

    body = resp.get_data(as_text=True)
    assert body.startswith("False"), body
    assert "http://localhost/" in body, body


def test_forwarded_headers_are_honoured_when_trust_is_on() -> None:
    """With trust ON and a matching Origin, the forwarded proto/host make
    Flask's own host_url agree with the browser's Origin — same_origin must
    now report True instead of the false-positive mismatch trust=False
    would have produced for this exact same real deployment traffic."""
    settings = Settings(trust_proxy_headers=True, proxy_hops=1)
    flask_app = _probe_app()
    _install_proxy_fix(flask_app, settings)
    client = flask_app.test_client()

    resp = client.get("/__probe", headers=_FORWARDED_HEADERS)

    assert resp.get_data(as_text=True) == "True https://sfuai.ca/"


def test_trusting_the_proxy_is_not_a_blanket_csrf_pass() -> None:
    """Trusting the proxy's own X-Forwarded-Host must not turn same_origin
    into a rubber stamp — a genuinely cross-site Origin is still rejected
    even though the forwarded host now correctly resolves to sfuai.ca."""
    settings = Settings(trust_proxy_headers=True, proxy_hops=1)
    flask_app = _probe_app()
    _install_proxy_fix(flask_app, settings)
    client = flask_app.test_client()

    resp = client.get(
        "/__probe",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "sfuai.ca",
            "Origin": "https://evil.example",
        },
    )

    assert resp.get_data(as_text=True).startswith("False")


def test_trust_on_with_no_forwarded_headers_falls_back_to_direct_origin() -> None:
    """With trust ON but no proxy in front of THIS particular request (e.g. a
    health check hitting the app directly), ProxyFix must not invent a
    forwarded origin — host_url stays the real WSGI origin and a matching
    direct-origin request is still same-origin."""
    settings = Settings(trust_proxy_headers=True, proxy_hops=1)
    flask_app = _probe_app()
    _install_proxy_fix(flask_app, settings)
    client = flask_app.test_client()

    resp = client.get("/__probe", headers={"Origin": "http://localhost"})

    assert resp.get_data(as_text=True) == "True http://localhost/"


# ── _configure_session_cookie ─────────────────────────────────────────────


@pytest.mark.parametrize("secure", [True, False])
def test_configure_session_cookie_matches_setting(secure: bool) -> None:
    settings = Settings(session_cookie_secure=secure)
    flask_app = Flask(__name__)

    _configure_session_cookie(flask_app, settings)

    assert flask_app.config["SESSION_COOKIE_SECURE"] is secure


def test_configure_session_cookie_is_applied_on_the_real_module_level_app() -> None:
    """The production `app` object (imported at module load) must actually
    have wired this through, not just the helper in isolation."""
    from frontend.app import _settings as real_settings
    from frontend.app import app as real_app

    assert (
        real_app.config["SESSION_COOKIE_SECURE"] is real_settings.session_cookie_secure
    )
