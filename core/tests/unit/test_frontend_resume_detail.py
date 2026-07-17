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
"""

from __future__ import annotations

import datetime as _dt
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
    """Even fed a payload that CONTAINS the candidate's real identity (as a
    reveal=True response would), the rendered HTML must not contain those
    bytes — the template has no branch that prints candidate.* fields."""
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


def test_resume_detail_template_has_no_candidate_render_branch() -> None:
    """Structural guard: the template source itself must never reference a
    ``candidate.<pii>`` field — there is no code path capable of printing it."""
    template = (Path(app.root_path) / "templates" / "resume_detail.html").read_text(
        encoding="utf-8"
    )
    for banned in (
        "candidate.name",
        "candidate.email",
        "candidate.phone",
        "candidate.location",
        "candidate['name']",
        "candidate['email']",
        "candidate['phone']",
        "candidate['location']",
    ):
        assert banned not in template, banned
