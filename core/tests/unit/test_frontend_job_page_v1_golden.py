"""S1 (job-page redesign, Option A) -- v1 must stay byte-for-byte unchanged.

Managers are testing the live job page. The redesign ships behind a
default-off flag, so with the flag unset the page must render exactly as it
does on ``main``. This module is the regression guard for that promise: it is
GREEN today and must stay green after the v2 shell lands. It is not a red test.

**Golden capture mechanism.** The expected HTML lives in
``tests/unit/goldens/job_page_v1/<state>.html``. When a golden file is missing
the test WRITES it from the current render and then FAILS with a message
containing "golden captured". A capture run therefore never passes, so a CI run
with a missing golden file is red rather than silently green. When the file
exists the test compares the render to it, byte for byte. The goldens were
captured from unmodified ``main`` before any v2 code existed.

The render is deterministic: fixed UUIDs, fixed timestamps, DTO-built fixtures
(``model_dump(mode="json")``), and the two CSRF token shapes (the hidden form
inputs and the ``hx-headers`` attribute on ``<body>``) are normalised, because
the CSRF page token is random per session.

To regenerate on purpose, delete the golden file, run the test once (it fails
with "golden captured"), review the diff, and commit it. Never edit a golden by
hand.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from src.schemas.jobs import JDExtracted, JobOut, Skill
from src.schemas.resumes import ResumeListItem
from src.settings import Settings

GOLDEN_DIR = Path(__file__).parent / "goldens" / "job_page_v1"

_JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
_TS = dt.datetime(2026, 9, 3, 8, 30, tzinfo=dt.UTC)
_STATES = (
    "writer_open_with_resumes",
    "writer_parsing_draft",
    "writer_draft_parse_failed",
    "hiring_manager_open",
)

_FORM_TOKEN_RE = re.compile(r'(name="csrf_token"\s+value=")[^"]*(")')
_HX_TOKEN_RE = re.compile(r'("X-CSRF-Token": ")[^"]*(")')


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _normalise(html: str) -> str:
    html = _FORM_TOKEN_RE.sub(r"\1<TOKEN>\2", html)
    return _HX_TOKEN_RE.sub(r"\1<TOKEN>\2", html)


def _job(**over: Any) -> dict[str, Any]:
    parsed = JDExtracted(
        title="Multimedia Specialist",
        required_skills=[Skill(name="Video editing"), Skill(name="Photography")],
    )
    base: dict[str, Any] = {
        "id": _JOB_ID,
        "title": "Multimedia Specialist",
        "department": "School of Medicine",
        "location": "Burnaby",
        "employment_type": None,
        "seniority": None,
        "min_years": 3,
        "description_raw": "A detailed job description of the role. " * 3,
        "description_parsed": parsed,
        "status": "open",
        "retention_days": 180,
        "shortlist_top_percent": 100,
        "blind_review": False,
        "failure_reason": None,
        "created_by": "asalah",
        "created_at": _TS,
        "updated_at": _TS,
        "parsed_at": _TS,
        "closed_at": None,
        "source": "manual",
    }
    base.update(over)
    return JobOut(**base).model_dump(mode="json")


def _resumes() -> list[dict[str, Any]]:
    rows = [
        ResumeListItem(
            id=UUID("22222222-2222-4222-8222-222222222222"),
            original_filename="ada.pdf",
            status="parsed",
            uploaded_at=_TS,
            parsed_at=_TS,
            candidate_name="Ada Lovelace",
        ),
        ResumeListItem(
            id=UUID("33333333-3333-4333-8333-333333333333"),
            original_filename="grace.pdf",
            status="parsing",
            uploaded_at=_TS,
            parsed_at=None,
        ),
    ]
    return [r.model_dump(mode="json") for r in rows]


def _stub_backend(
    monkeypatch: pytest.MonkeyPatch,
    job: dict[str, Any],
    resumes: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: job)
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: resumes)
    monkeypatch.setattr(api_client, "list_resume_splits", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "get_latest_resume_export", lambda jid, **kw: None)
    monkeypatch.setattr(api_client, "list_job_assignees", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_users", lambda **kw: [])
    monkeypatch.setattr(api_client, "list_shortlist", lambda jid, **kw: [])


def _use_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)


def _arrange(monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    """Install the backend stubs and session for one of the four states."""
    if state == "hiring_manager_open":
        _use_settings(monkeypatch, Settings(cas_enabled=True))
        monkeypatch.setattr(
            api_client,
            "get_cas_user",
            lambda **kw: {
                "authenticated": True,
                "username": "jordan",
                "cas_enabled": True,
                "role": "hiring_manager",
            },
        )
    else:
        _use_settings(monkeypatch, Settings(cas_enabled=False))
    if state == "writer_parsing_draft":
        job = _job(status="draft", parsed_at=None, description_parsed=None)
        resumes: list[dict[str, Any]] = []
    elif state == "writer_draft_parse_failed":
        job = _job(
            status="draft",
            parsed_at=None,
            description_parsed=None,
            failure_reason="The model returned no usable JSON.",
        )
        resumes = []
    else:
        job = _job()
        resumes = _resumes()
    _stub_backend(monkeypatch, job, resumes)


def _render(client: Any, url: str) -> str:
    resp = client.get(url, headers={"Cookie": "ra_session=tok-live"})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]
    return _normalise(resp.get_data(as_text=True))


def _compare_to_golden(state: str, html: str) -> None:
    path = GOLDEN_DIR / f"{state}.html"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(html.encode("utf-8"))
        pytest.fail(
            f"golden captured: wrote {path.name} from the current render. "
            "Review it, commit it, and re-run; a capture run never passes."
        )
    assert html.encode("utf-8") == path.read_bytes(), (
        f"the v1 job page changed for state {state!r}; with the flag off it "
        "must render byte for byte as before"
    )


@pytest.mark.parametrize("state", _STATES)
def test_the_v1_job_page_matches_its_golden(
    client: Any, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _arrange(monkeypatch, state)
    html = _render(client, f"/jobs/{_JOB_ID}")
    _compare_to_golden(state, html)


@pytest.mark.parametrize("state", _STATES)
def test_a_tab_query_with_the_flag_off_is_the_identical_v1_page(
    client: Any, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """``?tab=`` is ignored while the flag is off: same bytes as the bare URL."""
    _arrange(monkeypatch, state)
    plain = _render(client, f"/jobs/{_JOB_ID}")
    tabbed = _render(client, f"/jobs/{_JOB_ID}?tab=candidates")
    assert tabbed == plain
    golden = GOLDEN_DIR / f"{state}.html"
    if golden.exists():
        assert tabbed.encode("utf-8") == golden.read_bytes()


@pytest.mark.parametrize("state", _STATES)
def test_the_v1_page_carries_no_v2_marker(
    client: Any, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _arrange(monkeypatch, state)
    assert 'data-job-layout="v2"' not in _render(client, f"/jobs/{_JOB_ID}")


def test_the_normaliser_hides_token_values_only() -> None:
    raw = (
        '<body hx-headers=\'{"X-CSRF-Token": "abc123"}\'>'
        '<input type="hidden" name="csrf_token" value="abc123">'
        '<input type="hidden" name="to" value="open">'
    )
    out = _normalise(raw)
    assert "abc123" not in out
    assert 'name="to" value="open"' in out
