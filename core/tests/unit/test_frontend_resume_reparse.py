"""RED — the browser half of the résumé re-parse recovery path.

Mirrors ``test_frontend_job_reparse.py``'s shape for ``POST
/jobs/{id}/reparse``. Per the task instructions, every résumé context here is
built from a REAL ``ResumeOut`` via ``model_dump(mode="json")`` — never a
hand-written dict — matching ``test_templates_render_api_shaped_rows.py``'s
own rationale: the frontend only ever sees JSON over HTTP, so a hand-typed
fixture can carry a richer Python type (or a field name) the real API could
never send, and a test built on it can pass for the wrong reason.

None of ``api_client.reparse_resume``, the ``resume_reparse`` Flask route, or
the template's "Re-parse résumé" control exist yet — every test below fails
today (``AttributeError`` on the monkeypatch target, or a 404/missing text).
RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import _CSRF_HOOK_EXEMPT_ENDPOINTS, app
from src.schemas.resumes import CandidateInfo, ResumeOut, ResumeStatus
from src.settings import Settings

_NOW = dt.datetime(2026, 9, 19, 12, 0, tzinfo=dt.UTC)


@pytest.fixture
def client(csrf_client: Any) -> Any:
    """The shared CSRF-carrying browser client — a real browser presents a
    page token, so these do too rather than the guard being relaxed for them."""
    return csrf_client


def _resume_ctx(
    resume_id: UUID | None = None,
    *,
    status: ResumeStatus = "failed",
    degraded: bool = False,
    withdrawn_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """One résumé-detail body, exactly as ``GET /resumes/{id}`` puts it on the
    wire — built from the real ``ResumeOut``/``ResumeParsed`` and dumped in
    JSON mode, per the task instructions (never a hand-written dict)."""
    from src.schemas.resumes import ResumeParsed

    parsed = None
    parsed_at = None
    if status == "parsed":
        parsed = ResumeParsed(degraded=degraded)
        parsed_at = _NOW
    resume = ResumeOut(
        id=resume_id or uuid4(),
        job_id=uuid4(),
        original_filename="resume.pdf",
        mime_type="application/pdf",
        file_size_bytes=1234,
        sha256="a" * 64,
        candidate=CandidateInfo(name=None, email=None, phone=None, location=None),
        candidate_email_hash=None,
        parsed=parsed,
        status=status,
        uploaded_by="api",
        uploaded_at=_NOW,
        parsed_at=parsed_at,
        failure_reason=(
            "LLMUnavailableError: circuit breaker open" if status == "failed" else None
        ),
        consent_acknowledged=True,
        blinded=False,
        withdrawn_at=withdrawn_at,
    )
    return resume.model_dump(mode="json")


def _client_with(
    handler: Any,
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


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


# ── api_client.reparse_resume ────────────────────────────────────────────


def test_reparse_resume_posts_to_the_reparse_route() -> None:
    resume_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(202, json={"id": str(resume_id), "status": "queued"})

    api_client.reparse_resume(resume_id, client=_client_with(handler))
    assert captured["method"] == "POST"
    assert captured["path"] == f"/resumes/{resume_id}/reparse"


def test_reparse_resume_raises_conflict_on_409() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "résumé parsed cleanly"})

    with pytest.raises(api_client.Conflict) as exc:
        api_client.reparse_resume(uuid4(), client=_client_with(handler))
    assert exc.value.status_code == 409


# ── POST /resumes/<id>/reparse (the Flask BFF route) ─────────────────────


def test_reparse_route_redirects_to_the_resume_page(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    called: dict[str, Any] = {}

    def fake(rid: UUID, **_kw: Any) -> dict[str, str]:
        called["resume_id"] = rid
        return {"id": str(rid), "status": "queued"}

    monkeypatch.setattr(api_client, "reparse_resume", fake)
    resp = client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/resumes/{resume_id}")
    assert called["resume_id"] == resume_id


def test_reparse_route_calls_the_api_client_exactly_once(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    spy = MagicMock(return_value={"id": str(resume_id), "status": "queued"})
    monkeypatch.setattr(api_client, "reparse_resume", spy)
    client.post(f"/resumes/{resume_id}/reparse")
    spy.assert_called_once()


def test_reparse_route_surfaces_a_conflict_rather_than_500ing(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()

    def fake(rid: UUID, **_kw: Any) -> dict[str, str]:
        raise api_client.Conflict(
            "résumé parsed cleanly; there is nothing to recover",
            status_code=409,
            detail="résumé parsed cleanly; there is nothing to recover",
        )

    monkeypatch.setattr(api_client, "reparse_resume", fake)
    monkeypatch.setattr(
        api_client, "get_resume", lambda rid, **kw: _resume_ctx(rid, status="parsed")
    )
    resp = client.post(f"/resumes/{resume_id}/reparse")
    assert resp.status_code == 409
    assert "nothing to recover" in resp.get_data(as_text=True).lower()


def test_reparse_route_404s_when_the_backend_404s(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(rid: UUID, **_kw: Any) -> dict[str, str]:
        raise api_client.NotFound("no such resume")

    monkeypatch.setattr(api_client, "reparse_resume", fake)
    resp = client.post(f"/resumes/{uuid4()}/reparse")
    assert resp.status_code == 404


def test_reparse_route_rejects_a_request_with_no_page_token() -> None:
    """The ORDINARY ``_csrf_gate`` hook, not a one-shot slot — a plain client
    with no page token must still 403."""
    app.config.update(TESTING=True)
    plain_client = app.test_client()
    resp = plain_client.post(f"/resumes/{uuid4()}/reparse")
    assert resp.status_code == 403


def test_reparse_route_not_in_csrf_hook_exempt_endpoints() -> None:
    """No new one-shot slot: the shortlist_work_authorization docstring's
    64-token budget is the reason this must ride the ordinary reusable page
    token instead."""
    assert "resume_reparse" not in _CSRF_HOOK_EXEMPT_ENDPOINTS


# ── the control has to be visible to be a recovery path ──────────────────


def test_resume_detail_shows_reparse_control_for_a_failed_resume(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", lambda rid, **kw: _resume_ctx(rid, status="failed")
    )
    html = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert f"/resumes/{resume_id}/reparse" in html
    assert "Re-parse résumé" in html


def test_resume_detail_shows_reparse_control_for_a_degraded_parsed_resume(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        lambda rid, **kw: _resume_ctx(rid, status="parsed", degraded=True),
    )
    html = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert f"/resumes/{resume_id}/reparse" in html


def test_resume_detail_hides_reparse_control_for_a_clean_parse(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        lambda rid, **kw: _resume_ctx(rid, status="parsed", degraded=False),
    )
    html = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert f"/resumes/{resume_id}/reparse" not in html


def test_resume_detail_hides_reparse_control_for_a_withdrawn_resume(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        lambda rid, **kw: _resume_ctx(rid, status="failed", withdrawn_at=_NOW),
    )
    html = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert f"/resumes/{resume_id}/reparse" not in html


def test_resume_detail_shows_the_failure_reason(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", lambda rid, **kw: _resume_ctx(rid, status="failed")
    )
    html = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "circuit breaker open" in html


def test_resume_detail_hides_reparse_control_for_a_non_writer_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app.config.update(TESTING=True)
    plain_client = app.test_client()
    _enable_cas_session(monkeypatch, "hiring_manager")
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", lambda rid, **kw: _resume_ctx(rid, status="failed")
    )
    resp = plain_client.get(
        f"/resumes/{resume_id}", headers={"Cookie": "ra_session=tok-live"}
    )
    html = resp.get_data(as_text=True)
    assert f'action="/resumes/{resume_id}/reparse"' not in html


# ── no "re-upload" wording anywhere the feature touched ──────────────────


@pytest.mark.parametrize(
    "template_name",
    ["resume_detail.html", "resumes_table.html", "shortlist_list.html"],
)
def test_template_contains_no_re_upload_wording(template_name: str) -> None:
    from pathlib import Path

    text = (Path(app.root_path) / "templates" / template_name).read_text(
        encoding="utf-8"
    )
    assert "re-upload" not in text.lower()
