"""RED — every page must render the shape the API actually returns.

**Found in production, 2026-09-03, on the jobs list:**

    jinja2.exceptions.UndefinedError: 'str object' has no attribute 'strftime'
    index.html:201  {{ job.updated_at.strftime('%Y-%m-%d') if job.updated_at ... }}

The Updated column had landed one commit earlier with tests, a green
`verify.sh all`, and green CI. Every one of those tests asserted on the
template's *source text* — `assert "job.external_url" in tpl` — which cannot
see a type error, or rendered a hand-written dict in which somebody had
helpfully typed a `datetime`. The frontend never sees a `datetime`: it reads
JSON over HTTP, so every timestamp arrives as a **string**.

This is precisely the seam `CLAUDE.md` says the suite does not cross —
"~6,000 tests and not one crosses the browser→Flask→API seam: every frontend
test mocks `api_client`" — and `smoke.sh`, which does cross it, FAILS rather
than runs while CAS is on, which it now is.

**The fix that generalises is not "remember to use strings in fixtures."** It
is to stop hand-writing the fixture at all: build the real DTO, serialise it
the way FastAPI does (`model_dump(mode="json")`), and render the page with
that. Then a field whose type changes, or a template that grows a call only a
`datetime` supports, fails here — without anyone having to remember.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import uuid4

import pytest

from frontend import api_client

_TS = dt.datetime(2026, 9, 3, 8, 30, tzinfo=dt.UTC)


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _list_row(**over: Any) -> dict[str, Any]:
    """One jobs-list row, exactly as ``GET /jobs`` puts it on the wire.

    Built from ``JobListItem`` and dumped in JSON mode — NOT hand-written, so
    it cannot drift from the DTO and cannot accidentally carry a richer Python
    type than the frontend can ever receive.
    """
    from src.schemas.jobs import JobListItem

    base: dict[str, Any] = {
        "id": uuid4(),
        "title": "Multimedia Specialist",
        "department": "School of Medicine",
        "location": "Burnaby",
        "status": "open",
        "created_at": _TS,
        "updated_at": _TS,
        "parsed_at": _TS,
        "source": "taleo",
        "external_url": "https://tre.tbe.taleo.net/req?rid=7124",
        "resume_count": 12,
    }
    base.update(over)
    return JobListItem(**base).model_dump(mode="json")


def _detail(**over: Any) -> dict[str, Any]:
    """One job-detail body, exactly as ``GET /jobs/{id}`` puts it on the wire."""
    from src.schemas.jobs import JobOut

    base: dict[str, Any] = {
        "id": uuid4(),
        "title": "Multimedia Specialist",
        "department": "School of Medicine",
        "location": "Burnaby",
        "employment_type": None,
        "seniority": None,
        "min_years": None,
        "description_raw": "A detailed job description of the role. " * 3,
        "description_parsed": None,
        "status": "draft",
        "retention_days": 180,
        "shortlist_top_percent": 100,
        "blind_review": True,
        "failure_reason": None,
        "created_by": "asalah",
        "created_at": _TS,
        "updated_at": _TS,
        "parsed_at": _TS,
        "closed_at": None,
        "source": "taleo",
        "external_id": "7124",
        "external_url": "https://tre.tbe.taleo.net/req?rid=7124",
        "external_last_seen_at": _TS,
        "title_provisional": True,
    }
    base.update(over)
    return JobOut(**base).model_dump(mode="json")


# ── the jobs list ───────────────────────────────────────────────────────────


def test_the_jobs_list_renders_a_real_api_row(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression. This 500'd in production with 'str object' has no
    attribute 'strftime'."""
    monkeypatch.setattr(api_client, "list_jobs", lambda **kw: [_list_row()])
    resp = client.get("/")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]


def test_the_updated_column_shows_a_date_and_not_a_timestamp(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The column exists to answer "was this touched recently", scanned down a
    40-row list. A full ISO timestamp is noise; the crash must not be fixed by
    dumping the raw string into the cell."""
    monkeypatch.setattr(api_client, "list_jobs", lambda **kw: [_list_row()])
    html = client.get("/").get_data(as_text=True)
    assert "2026-09-03" in html
    assert "08:30" not in html
    assert "T08" not in html


def test_a_row_with_no_timestamps_still_renders(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``updated_at`` and ``parsed_at`` are nullable on the DTO, and a job
    created before the column existed reads null."""
    monkeypatch.setattr(
        api_client,
        "list_jobs",
        lambda **kw: [_list_row(updated_at=None, parsed_at=None)],
    )
    resp = client.get("/")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]


def test_an_empty_list_still_renders(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_client, "list_jobs", lambda **kw: [])
    assert client.get("/").status_code == 200


# ── the job detail page ─────────────────────────────────────────────────────


def test_the_job_detail_page_renders_a_real_api_body(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same guard, other page. It grew an edit form and a provenance note in
    the same batch of work that broke the list; nothing had rendered it from a
    DTO-derived body either."""
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _detail())
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    resp = client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]


def test_the_job_detail_page_renders_a_bare_job(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual, unparsed job with every optional field null — the shape a
    freshly created requisition actually has."""
    monkeypatch.setattr(
        api_client,
        "get_job",
        lambda jid, **kw: _detail(
            department=None,
            location=None,
            parsed_at=None,
            source="manual",
            external_id=None,
            external_url=None,
            external_last_seen_at=None,
            title_provisional=False,
        ),
    )
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    resp = client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:400]


# ── the filter itself ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        # Copied verbatim off the running API, not invented: this is what
        # `JobListItem.model_dump(mode="json")` emits for a real row —
        # microseconds AND a `Z`, the two things a naive parse trips on.
        ("2026-09-03T23:10:55.264135Z", "2026-09-03"),
        ("2026-09-03T08:30:00+00:00", "2026-09-03"),
        ("2026-09-03T08:30:00Z", "2026-09-03"),
        ("2026-09-03", "2026-09-03"),
        (_TS, "2026-09-03"),  # a datetime, for any server-side caller
        (None, "—"),
        ("", "—"),
    ],
)
def test_the_day_filter_accepts_every_shape_a_timestamp_arrives_in(
    value: Any, expected: str
) -> None:
    from frontend.app import _day

    assert _day(value) == expected


def test_the_day_filter_does_not_raise_on_junk() -> None:
    """A template filter that can raise is a 500 on a whole page. Anything
    unparseable is shown as-is rather than crashing the render — the cell is
    informational, and no date is worth losing the jobs list over."""
    from frontend.app import _day

    assert _day("not a date") == "not a date"
    assert _day(12345) == "12345"
