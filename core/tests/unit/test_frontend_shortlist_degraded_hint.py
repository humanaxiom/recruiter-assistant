"""RED pin — live demo, 2026-09-09 21:42: the shortlist page told the user
NOTHING when a résumé's skills parse degraded and was silently excluded from
ranking.

Every résumé on the job showed ``parsed`` in the table; the shortlist page
just said "No ranked candidates yet. Make sure the résumés below show
parsed" — advice the recruiter had already followed, with no way to learn
that one résumé (FU-7 §4 / ADR-030: a keyword-scan fallback,
``parsed->>'degraded' = true``) is by design never projected and never
ranked.

The fix surfaces the fact on the shortlist FULL page
(``shortlist_list.html``): when any résumé for the job is degraded, a hint
matching ``(\\d+) résumés? (has|have) a degraded parse and (is|are) not
ranked`` renders, singular/plural agreed; it is absent when none is degraded
and absent when there are no résumés at all. This is parse STATE, not PII —
``ResumeListItem.degraded`` already survives blind-review redaction (see
``test_resume_degraded_visibility_pg.py``), so the hint must render for every
session role, not just a writer.

Fixtures are built as real ``ResumeListItem`` DTOs,
``model_dump(mode="json")``'d — per HANDOFF lesson 6, never a hand-written
dict — mirroring ``test_frontend_shortlist.py``'s ``_dto_entry`` pattern.
Follows that file's ``list_shortlist``/``list_resumes``/status-mock style, and
``test_frontend_work_authorization_on_shortlist.py``'s
``_enable_cas_session``/role pattern for the "every role" assertion.

Every test below fails: the hint does not exist anywhere in
``shortlist_list.html`` yet, so ``_HINT_RE.search(body)`` is ``None`` even
when a degraded résumé is present. RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.schemas.resumes import ResumeListItem
from src.settings import Settings

_HINT_RE = re.compile(
    r"(\d+) résumés? (has|have) a degraded parse and (is|are) not ranked"
)


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


@pytest.fixture(autouse=True)
def _no_ranking_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every job in this file has a settled shortlist -- no ranking/
    awaiting_llm state -- so the poll banners never interfere with the hint
    assertions (mirrors ``test_frontend_work_authorization_on_shortlist.py``'s
    ``_no_ranking_state``)."""
    monkeypatch.setattr(
        api_client,
        "get_shortlist_status",
        MagicMock(
            return_value={"job_id": None, "state": None, "reason": None, "at": None}
        ),
    )
    monkeypatch.setattr(api_client, "list_shortlist", MagicMock(return_value=[]))


def _resume_item(*, degraded: bool, status: str = "parsed") -> dict[str, Any]:
    """A REAL ``ResumeListItem`` DTO, ``model_dump(mode="json")``'d -- per
    HANDOFF lesson 6, never a hand-written dict carrying a ``datetime``."""
    item = ResumeListItem(
        id=uuid4(),
        original_filename="resume.pdf",
        status=status,  # type: ignore[arg-type]
        uploaded_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
        parsed_at=dt.datetime(2026, 9, 1, 1, tzinfo=dt.UTC),
        candidate_name="Jane Smith",
        degraded=degraded,
    )
    return item.model_dump(mode="json")


def _get(client: Any, job_id: Any) -> str:
    return client.get(f"/jobs/{job_id}/shortlist").get_data(as_text=True)


def _authenticated(role: str) -> dict[str, Any]:
    return {
        "authenticated": True,
        "username": "jordan",
        "cas_enabled": True,
        "role": role,
    }


def _enable_cas_session(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client, "get_cas_user", MagicMock(return_value=_authenticated(role))
    )


# ── the hint appears when at least one résumé is degraded ──────────────────


def test_hint_present_singular_when_exactly_one_resume_is_degraded(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    resumes = [
        _resume_item(degraded=False),
        _resume_item(degraded=False),
        _resume_item(degraded=True),
    ]
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=resumes))

    body = _get(client, job_id)
    match = _HINT_RE.search(body)

    assert match is not None, "no degraded-parse hint rendered on the shortlist page"
    assert match.group(1) == "1"
    assert (
        match.group(2) == "has"
    ), f"singular subject took the plural verb {match.group(2)!r}"
    assert (
        match.group(3) == "is"
    ), f"singular subject took the plural verb {match.group(3)!r}"


def test_hint_present_plural_when_multiple_resumes_are_degraded(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    resumes = [
        _resume_item(degraded=True),
        _resume_item(degraded=True),
        _resume_item(degraded=False),
    ]
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=resumes))

    body = _get(client, job_id)
    match = _HINT_RE.search(body)

    assert match is not None
    assert match.group(1) == "2"
    assert (
        match.group(2) == "have"
    ), f"plural subject took the singular verb {match.group(2)!r}"
    assert (
        match.group(3) == "are"
    ), f"plural subject took the singular verb {match.group(3)!r}"


def test_hint_counts_only_degraded_resumes_not_the_whole_pool(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    """10 résumés, 1 degraded -- the exact live-demo shape. The count must be
    1, never 10 (the whole pool) and never 9 (the ranked remainder)."""
    job_id = uuid4()
    resumes = [_resume_item(degraded=False) for _ in range(9)] + [
        _resume_item(degraded=True)
    ]
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=resumes))

    body = _get(client, job_id)
    match = _HINT_RE.search(body)

    assert match is not None
    assert match.group(1) == "1"


def test_hint_counts_degraded_alongside_resumes_still_in_flight(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    """A job mid-batch -- some résumés still ``parsing``, one already parsed
    and degraded -- must still surface the hint for the one that IS parsed
    and degraded, without the in-flight rows affecting the count."""
    job_id = uuid4()
    resumes = [
        _resume_item(degraded=False, status="parsing"),
        _resume_item(degraded=False, status="parsing"),
        _resume_item(degraded=True),
    ]
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=resumes))

    body = _get(client, job_id)
    match = _HINT_RE.search(body)

    assert match is not None
    assert match.group(1) == "1"


# ── the hint is absent otherwise ────────────────────────────────────────


def test_hint_absent_when_no_resume_is_degraded(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    resumes = [_resume_item(degraded=False), _resume_item(degraded=False)]
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=resumes))

    body = _get(client, job_id)

    assert _HINT_RE.search(body) is None


def test_hint_absent_when_there_are_no_resumes_at_all(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))

    body = _get(client, job_id)

    assert _HINT_RE.search(body) is None


# ── visible for every role -- parse state, not PII ─────────────────────────


@pytest.mark.parametrize("role", ["recruiter", "admin", "hiring_manager", "auditor"])
def test_hint_renders_for_every_session_role(
    monkeypatch: pytest.MonkeyPatch, client: Any, role: str
) -> None:
    _enable_cas_session(monkeypatch, role)
    job_id = uuid4()
    resumes = [_resume_item(degraded=True)]
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=resumes))

    resp = client.get(
        f"/jobs/{job_id}/shortlist", headers={"Cookie": "ra_session=tok-live"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert _HINT_RE.search(body) is not None, (
        f"role {role!r} did not see the degraded-parse hint -- it is parse "
        "state, not PII, and must be visible regardless of role"
    )


def test_hint_renders_with_no_authenticated_session_at_all(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    """CAS disabled (``current_user is None``) is the default dev/test
    posture -- the hint must render there too, not only under an
    authenticated session."""
    job_id = uuid4()
    resumes = [_resume_item(degraded=True)]
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=resumes))

    body = _get(client, job_id)

    assert _HINT_RE.search(body) is not None
