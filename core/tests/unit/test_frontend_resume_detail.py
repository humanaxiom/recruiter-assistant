"""Slice S6 — rebuilt résumé-detail view (``resume_detail.html``).

The backend ``GET /resumes/{id}`` (reveal=False) returns a ``ResumeOut`` whose
``parsed`` holds the redacted résumé. This slice renders ONLY the non-PII
structured fields (skills, experience, education, cover letter, source chunks)
plus an amber blind banner.

**Hard invariant (ADR-011/012/013) — the reason this app exists:** the
résumé-detail template has NO code branch capable of rendering
``candidate.name`` / ``candidate.email`` / ``candidate.phone`` /
``candidate.location``. This is proved structurally: even if
``api_client.get_resume`` is monkeypatched to return a payload that *contains* a
real name/email/phone (as a reveal=True response would), the rendered HTML must
not contain those bytes — because the template simply has no path that prints
them. ``get_resume`` is always called with ``reveal=False``.

**FU-4/D4 — CSRF on the reveal form.** ``POST /resumes/<id>/reveal`` now
requires a session-bound, one-shot anti-forgery token (``frontend.csrf``,
tested directly in ``test_frontend_csrf.py``): the preceding ``GET
/resumes/<id>`` mints a token and renders it into the reveal form as a hidden
``csrf_token`` input; the POST route rejects (403) a missing/wrong/replayed
token, or a request tagged with a cross-origin ``Origin`` header, WITHOUT ever
calling ``api_client.reveal_resume`` — no reveal audit row is implied by a
rejected forgery attempt. Every reveal-POST test below that predates FU-4 now
mints a real token via ``_mint_csrf_token`` before posting.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app

_REAL_NAME = "Zzyzxqrst Wibblesworth"
_REAL_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_REAL_PHONE = "604-555-0192"
_REAL_LOCATION = "Nowheresville, Yukon"

_CUR = _dt.date.today().year


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _parsed(*, with_pii: bool = False) -> dict[str, Any]:
    """A redacted ``ResumeParsed`` payload. With ``with_pii`` the identity
    fields are populated the way a reveal=True response would carry them —
    used by the structural byte-scan guard."""
    candidate: dict[str, Any]
    if with_pii:
        candidate = {
            "name": _REAL_NAME,
            "email": _REAL_EMAIL,
            "phone": _REAL_PHONE,
            "location": _REAL_LOCATION,
        }
    else:
        candidate = {"name": None, "email": None, "phone": None, "location": None}
    return {
        "candidate": candidate,
        "summary": "Seasoned backend engineer with a platform focus.",
        "total_years_experience": 8,
        "skills": [
            {
                "name": "Python",
                "years": 6,
                "last_used_year": _CUR,
                "evidence_chunk_ids": ["c_1"],
            },
            {"name": "Kubernetes", "years": 3, "last_used_year": _CUR - 4},
            {"name": "COBOL", "years": 2, "last_used_year": _CUR - 12},
            {"name": "Docker"},
        ],
        "experience": [
            {
                "company": "Employer A",
                "title": "Staff Engineer",
                "start": "2019",
                "end": "2024",
                "is_current": False,
                "bullets": [
                    {"text": "Led the platform rewrite.", "chunk_id": "c_5"},
                    {"text": "Mentored four engineers.", "chunk_id": None},
                ],
            }
        ],
        "education": [
            {
                "degree": "BSc",
                "institution": "Institution A",
                "field": "Computer Science",
                "year": 2014,
            }
        ],
        "chunks": [
            {"id": "c_1", "section": "skills", "page": 1, "text": "Expert in Python."}
        ],
        "cover_letter_chunks": [],
    }


def _resume(
    resume_id: Any,
    *,
    blinded: bool = True,
    with_pii: bool = False,
    cover_letter: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(resume_id),
        "job_id": str(uuid4()),
        "original_filename": "resume.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 1234,
        "sha256": "0" * 64,
        "candidate": _parsed(with_pii=with_pii)["candidate"],
        "candidate_email_hash": None,
        "parsed": _parsed(with_pii=with_pii),
        "status": "parsed",
        "uploaded_by": None,
        "uploaded_at": "2026-07-01T00:00:00Z",
        "parsed_at": "2026-07-01T00:05:00Z",
        "failure_reason": None,
        "consent_acknowledged": True,
        "blinded": blinded,
        "cover_letter_text": None,
        "cover_letter_parsed": None,
    }
    if cover_letter:
        payload["cover_letter_text"] = (
            "I am excited to bring my platform experience to your team."
        )
        payload["cover_letter_parsed"] = {
            "raw_text": ("I am excited to bring my platform experience to your team."),
            "themes": ["platform ownership", "mentorship"],
            "key_claims": ["scaled the API 10x"],
            "sentiment": "positive",
        }
    return payload


# ── FU-4/D4 CSRF helpers ─────────────────────────────────────────────────

_CSRF_INPUT_RE = re.compile(
    r'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"'
    r'|<input[^>]*value="([^"]+)"[^>]*name="csrf_token"'
)


def _extract_csrf_token(html: str) -> str:
    match = _CSRF_INPUT_RE.search(html)
    assert match is not None, "expected a hidden csrf_token input in the reveal form"
    return match.group(1) or match.group(2)


def _mint_csrf_token(monkeypatch: Any, client: Any, resume_id: Any) -> str:
    """GET the résumé-detail page (which must be blinded, so the reveal form —
    and its token — is rendered) and pull the freshly-minted ``csrf_token``
    hidden-input value out of the response body. Mints the token bound to
    whichever session ``client`` is carrying cookies for."""
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    return _extract_csrf_token(body)


# ── section rendering ────────────────────────────────────────────────────


def test_resume_detail_renders_all_sections(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    # summary
    assert "Seasoned backend engineer" in body
    # skills
    assert "Python" in body
    assert "Kubernetes" in body
    assert "Docker" in body
    # experience: title + (generic) employer + dates
    assert "Staff Engineer" in body
    assert "Employer A" in body
    assert "2019" in body
    # experience bullets + cited chunk id
    assert "Led the platform rewrite." in body
    assert "c_5" in body
    # education
    assert "BSc" in body
    assert "Institution A" in body
    assert "Computer Science" in body


def test_resume_detail_blind_banner_shows_when_blinded(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "blind-banner" in body
    assert "Identity hidden for blind review" in body


def test_resume_detail_blind_banner_absent_when_not_blinded(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=False)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "Identity hidden for blind review" not in body


# ── skill recency colour-coding ──────────────────────────────────────────


def test_resume_detail_colour_codes_skills_by_recency(
    monkeypatch: Any, client: Any
) -> None:
    """A skill used this year is green (current), one used four years ago is
    amber (aging), one used twelve years ago is zinc (stale)."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "chip-recency-current" in body
    assert "chip-recency-aging" in body
    assert "chip-recency-stale" in body
    # a small legend explaining the buckets
    assert "recency-legend" in body


def test_resume_detail_plain_chip_when_skill_has_no_recency(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    # Docker carries no last_used_year → plain chip, no recency class on it.
    assert "Docker" in body


# ── cover letter ─────────────────────────────────────────────────────────


def test_resume_detail_renders_cover_letter(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, cover_letter=True)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "platform ownership" in body  # theme
    assert "positive" in body  # sentiment
    assert "I am excited to bring my platform experience" in body  # text


def test_resume_detail_without_cover_letter_omits_section(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, cover_letter=False)),
    )
    resp = client.get(f"/resumes/{resume_id}")
    assert resp.status_code == 200


# ── source chunks ────────────────────────────────────────────────────────


def test_resume_detail_shows_source_chunks_details(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    assert "<details" in body
    assert "Expert in Python." in body


# ── redaction boundary ───────────────────────────────────────────────────


def test_resume_detail_calls_get_resume_with_reveal_false(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    spy = MagicMock(return_value=_resume(resume_id))
    monkeypatch.setattr(api_client, "get_resume", spy)
    client.get(f"/resumes/{resume_id}?reveal=true")
    spy.assert_called_once()
    _, kwargs = spy.call_args
    assert kwargs.get("reveal", False) is False


def test_resume_detail_structural_no_pii_byte_scan(
    monkeypatch: Any, client: Any
) -> None:
    """The DEFAULT (blind, non-reveal) GET must never print identity, even fed a
    payload that CONTAINS the candidate's real name/email/phone: the default
    render passes ``revealed=False`` so the identity block (the ONE place
    ``candidate.*`` is rendered) is never emitted. Identity is exposed only via
    the audited reveal POST — see ``test_resume_reveal_*`` below (FU-1)."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, with_pii=True)),
    )
    raw = client.get(f"/resumes/{resume_id}?reveal=true").get_data(as_text=True)
    assert _REAL_NAME not in raw
    assert _REAL_EMAIL not in raw
    assert _REAL_PHONE not in raw
    assert _REAL_LOCATION not in raw


def test_resume_detail_default_get_never_calls_reveal(
    monkeypatch: Any, client: Any
) -> None:
    """The blind default GET must never route through the audited reveal path —
    a plain view is not a reveal (and must not write an audit row)."""
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client, "get_resume", MagicMock(return_value=_resume(resume_id))
    )
    reveal_spy = MagicMock()
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    client.get(f"/resumes/{resume_id}")
    reveal_spy.assert_not_called()


def test_resume_reveal_shows_identity_and_audit_notice(
    monkeypatch: Any, client: Any
) -> None:
    """FU-1: POST /resumes/<id>/reveal renders the UN-blinded identity and the
    audit notice. This is the ONE render path that surfaces ``candidate.*``.
    FU-4/D4: requires a valid one-shot CSRF token, minted by the preceding
    GET."""
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    monkeypatch.setattr(
        api_client,
        "reveal_resume",
        MagicMock(return_value=_resume(resume_id, with_pii=True)),
    )
    raw = client.post(
        f"/resumes/{resume_id}/reveal", data={"csrf_token": token}
    ).get_data(as_text=True)
    assert _REAL_NAME in raw
    assert _REAL_EMAIL in raw
    assert _REAL_PHONE in raw
    assert "audit log" in raw.lower()


def test_resume_reveal_forwards_context_from_form(
    monkeypatch: Any, client: Any
) -> None:
    """A reveal triggered from the shortlist card posts ``context=shortlist``;
    the route forwards it so the audit row records the true origin. FU-4/D4:
    also requires a valid CSRF token alongside ``context``."""
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", spy)
    client.post(
        f"/resumes/{resume_id}/reveal",
        data={"context": "shortlist", "csrf_token": token},
    )
    spy.assert_called_once()
    assert spy.call_args.kwargs["context"] == "shortlist"


def test_resume_reveal_context_defaults_to_resume_detail(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", spy)
    client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    spy.assert_called_once()
    assert spy.call_args.kwargs["context"] == "resume_detail"


def test_resume_reveal_uses_audited_endpoint_not_raw_reveal(
    monkeypatch: Any, client: Any
) -> None:
    """The reveal must go through ``reveal_resume`` (POST, which audits), NOT a
    raw ``get_resume(reveal=True)`` that would skip the audit trail. FU-4/D4:
    the token-minting GET legitimately calls ``get_resume`` once — reset the
    spy before the assertion-relevant POST so that call doesn't get
    conflated with a (forbidden) reveal-time ``get_resume`` call."""
    resume_id = uuid4()
    get_spy = MagicMock(return_value=_resume(resume_id, with_pii=True, blinded=True))
    monkeypatch.setattr(api_client, "get_resume", get_spy)
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    token = _extract_csrf_token(body)
    get_spy.reset_mock()

    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    reveal_spy.assert_called_once()
    get_spy.assert_not_called()


def test_resume_detail_candidate_refs_only_inside_reveal_branch() -> None:
    """Structural guard (updated for FU-1): the template MAY reference
    ``candidate.*`` — but every such reference must sit inside the single
    ``{% if revealed %}`` block, so no non-reveal render can reach it."""
    template = (Path(app.root_path) / "templates" / "resume_detail.html").read_text(
        encoding="utf-8"
    )
    gate = template.find("{% if revealed %}")
    elif_marker = template.find("{% elif resume.blinded %}")
    assert gate != -1, "expected an `{% if revealed %}` gate in resume_detail.html"
    assert elif_marker > gate, "expected the reveal block to end at `{% elif %}`"
    # Every candidate.* reference must fall strictly inside the reveal-only span
    # (between the `revealed` gate and the `elif blinded` branch), so no
    # non-reveal render can reach it. Robust to the nested per-field ifs.
    banned = (
        "candidate.name",
        "candidate.email",
        "candidate.phone",
        "candidate.location",
        "candidate['name']",
        "candidate['email']",
        "candidate['phone']",
        "candidate['location']",
    )
    for token in banned:
        idx = template.find(token)
        while idx != -1:
            assert gate < idx < elif_marker, f"{token} outside the reveal block"
            idx = template.find(token, idx + 1)


# ── FU-4/D4 — CSRF on the reveal form ─────────────────────────────────────


def test_resume_detail_get_renders_a_csrf_token_hidden_input(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "get_resume",
        MagicMock(return_value=_resume(resume_id, blinded=True)),
    )
    body = client.get(f"/resumes/{resume_id}").get_data(as_text=True)
    token = _extract_csrf_token(body)
    assert len(token) >= 16


def test_resume_reveal_without_a_token_is_rejected_and_backend_never_called(
    monkeypatch: Any, client: Any
) -> None:
    """No token at all (e.g. a cross-site auto-submitting form, which supplies
    no session cookie's token) → 403, and CRITICALLY the audited reveal call
    never happens — no reveal_audit row is implied by a rejected forgery."""
    resume_id = uuid4()
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    resp = client.post(f"/resumes/{resume_id}/reveal")
    assert resp.status_code == 403
    reveal_spy.assert_not_called()


def test_resume_reveal_with_a_garbage_token_is_rejected_and_backend_never_called(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    _mint_csrf_token(monkeypatch, client, resume_id)  # a real token now exists…
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    # …but we submit a different, made-up value instead of it.
    resp = client.post(
        f"/resumes/{resume_id}/reveal", data={"csrf_token": "not-the-real-token"}
    )
    assert resp.status_code == 403
    reveal_spy.assert_not_called()


def test_resume_reveal_with_a_token_from_a_different_session_is_rejected(
    monkeypatch: Any, client: Any
) -> None:
    """A token minted for one browser session must not validate a POST arriving
    with a different session's cookie — the token is session-bound."""
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    other_client = app.test_client()
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    resp = other_client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    assert resp.status_code == 403
    reveal_spy.assert_not_called()


def test_resume_reveal_with_a_valid_token_succeeds(
    monkeypatch: Any, client: Any
) -> None:
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    resp = client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    assert resp.status_code == 200
    reveal_spy.assert_called_once()
    assert _REAL_NAME in resp.get_data(as_text=True)


def test_resume_reveal_token_is_single_use(monkeypatch: Any, client: Any) -> None:
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)

    first = client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    assert first.status_code == 200

    second = client.post(f"/resumes/{resume_id}/reveal", data={"csrf_token": token})
    assert second.status_code == 403
    reveal_spy.assert_called_once()  # still just the one successful call


def test_resume_reveal_cross_origin_request_is_rejected_even_with_a_valid_token(
    monkeypatch: Any, client: Any
) -> None:
    """The Origin/Referer same-origin check is layered ON TOP of the token, not
    instead of it: a cross-origin Origin header must reject the request even
    when the (correct, unconsumed) token is present."""
    resume_id = uuid4()
    token = _mint_csrf_token(monkeypatch, client, resume_id)
    reveal_spy = MagicMock(return_value=_resume(resume_id, with_pii=True))
    monkeypatch.setattr(api_client, "reveal_resume", reveal_spy)
    resp = client.post(
        f"/resumes/{resume_id}/reveal",
        data={"csrf_token": token},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    reveal_spy.assert_not_called()
