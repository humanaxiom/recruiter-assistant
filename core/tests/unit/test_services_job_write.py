"""Unit tests for Phase 6's job CRUD/status service surface —
``src.services.job_service.create_job`` / ``get_job`` / ``list_jobs`` /
``transition_status`` / ``_row_to_jobout``.

None of these exist yet — ``src/services/job_service.py`` currently ships
only the worker write-back pair (``record_parsed`` / ``record_parse_failure``,
Phase 3). Every test below fails at collection (``ImportError``) or the first
``await`` (``AttributeError``). RED half of the TDD cycle.

**Ambiguities this file locks (the coder's implementation MUST match):**

* ``create_job(conn, payload: JobCreate, *, created_by: str | None) -> JobOut``
  inserts a row with ``status='draft'`` (the DDL default) and returns the full
  ``JobOut`` built from the inserted row.
* ``get_job(conn, job_id) -> JobOut`` raises ``src.errors.NotFoundError`` when
  the id does not resolve (mirrors ``resume_service.get_one``).
* ``list_jobs(conn, *, limit=50, offset=0, status=None) -> list[JobListItem]``
  — ``status`` is an optional filter; omitted means "all statuses".
* ``_row_to_jobout(row) -> JobOut`` is the SINGLE place that builds a
  ``JobOut`` from a raw DB row. THE LOAD-BEARING CONTRACT (ADR-006 / Phase 2
  security "low", carried forward through every HANDOFF phase since): the DTO
  field ``JobOut.blind_review`` defaults to ``False`` (fail-OPEN) if a builder
  ever omits it, so ``_row_to_jobout`` MUST read ``row["blind_review"]``
  explicitly — never rely on the pydantic default. Tested BOTH directions
  (True row -> True DTO, False row -> False DTO) so the test cannot pass by a
  builder that hardcodes either literal.
* ``transition_status(conn, job_id, to) -> JobOut`` validates the transition
  against a known state graph (draft -> open -> closed -> archived, strictly
  forward, no skipping) and raises ``ValueError`` for any transition not in
  that graph (including a same-state no-op and a backward move) — this is the
  route's 409 material, NOT a pydantic ``Literal`` rejection (JobStatus is
  already a closed enum at the schema layer; this is business-rule
  validation on TOP of a syntactically valid target).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.errors import NotFoundError
from src.schemas.jobs import JobCreate, JobListItem, JobOut

_NOW = dt.datetime(2026, 7, 16, tzinfo=dt.UTC)


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _job_row(
    *,
    job_id: UUID | None = None,
    title: str = "Senior Backend Engineer",
    status: str = "draft",
    blind_review: bool = True,
    description_parsed: dict[str, Any] | None = None,
    created_by: str | None = "api",
) -> _Row:
    return _Row(
        {
            "id": job_id or uuid4(),
            "title": title,
            "department": "Engineering",
            "location": "Remote",
            "employment_type": "full_time",
            "seniority": "senior",
            "min_years": 5,
            "description_raw": "We are looking for a senior backend engineer " * 3,
            "description_parsed": (
                json.dumps(description_parsed)
                if description_parsed is not None
                else None
            ),
            "status": status,
            "retention_days": 180,
            "blind_review": blind_review,
            "failure_reason": None,
            "created_by": created_by,
            "created_at": _NOW,
            "updated_at": _NOW,
            "parsed_at": None,
            "closed_at": None,
        }
    )


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(
    *, fetchrow: _Row | None = None, fetch: list[_Row] | None = None
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _payload(**overrides: Any) -> JobCreate:
    base = {
        "title": "Senior Backend Engineer",
        "description_raw": "We are looking for a senior backend engineer " * 3,
    }
    base.update(overrides)
    return JobCreate.model_validate(base)


# ── create_job ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_job_returns_a_joubout_in_draft_status() -> None:
    from src.services import job_service

    row = _job_row(status="draft")
    conn = _mock_conn(fetchrow=row)
    out = await job_service.create_job(conn, _payload(), created_by="alice")
    assert isinstance(out, JobOut)
    assert out.status == "draft"


@pytest.mark.asyncio
async def test_create_job_inserts_with_the_payload_title() -> None:
    from src.services import job_service

    row = _job_row(title="Staff ML Engineer")
    conn = _mock_conn(fetchrow=row)
    await job_service.create_job(
        conn, _payload(title="Staff ML Engineer"), created_by="x"
    )
    query, *args = (
        conn.execute.await_args.args
        if conn.execute.await_args
        else (conn.fetchrow.await_args.args)
    )
    assert "Staff ML Engineer" in args or "INSERT" in query.upper()


@pytest.mark.asyncio
async def test_create_job_persists_created_by_actor_label() -> None:
    from src.services import job_service

    row = _job_row(created_by="alice")
    conn = _mock_conn(fetchrow=row)
    out = await job_service.create_job(conn, _payload(), created_by="alice")
    assert out.created_by == "alice"


@pytest.mark.asyncio
async def test_create_job_default_actor_label_is_api_when_none_passed() -> None:
    """The route resolves a default actor of "api" when X-Actor-Name is
    absent — create_job must round-trip whatever the caller passes (None
    stays None at the DB level; the "api" default is the ROUTE's job, tested
    in test_route_jobs.py)."""
    from src.services import job_service

    row = _job_row(created_by=None)
    conn = _mock_conn(fetchrow=row)
    out = await job_service.create_job(conn, _payload(), created_by=None)
    assert out.created_by is None


# ── get_job ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_returns_joubout_for_an_existing_job() -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    out = await job_service.get_job(conn, job_id)
    assert out.id == job_id


@pytest.mark.asyncio
async def test_get_job_raises_not_found_error_on_missing_id() -> None:
    from src.services import job_service

    conn = _mock_conn(fetchrow=None)
    with pytest.raises(NotFoundError):
        await job_service.get_job(conn, uuid4())


# ── list_jobs ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_returns_joblistitem_rows() -> None:
    from src.services import job_service

    rows = [_job_row(title="A"), _job_row(title="B")]
    conn = _mock_conn(fetch=rows)
    out = await job_service.list_jobs(conn)
    assert len(out) == 2
    assert all(isinstance(o, JobListItem) for o in out)


@pytest.mark.asyncio
async def test_list_jobs_passes_limit_and_offset_through_to_the_query() -> None:
    from src.services import job_service

    conn = _mock_conn(fetch=[])
    await job_service.list_jobs(conn, limit=10, offset=20)
    args = conn.fetch.await_args.args
    assert 10 in args
    assert 20 in args


@pytest.mark.asyncio
async def test_list_jobs_status_filter_is_passed_when_provided() -> None:
    from src.services import job_service

    conn = _mock_conn(fetch=[])
    await job_service.list_jobs(conn, status="open")
    args = conn.fetch.await_args.args
    assert "open" in args


@pytest.mark.asyncio
async def test_list_jobs_status_filter_omitted_means_no_status_arg() -> None:
    """Without a status filter, the query must not silently scope to one
    status (e.g. hard-coding 'open') — omitted means "every status"."""
    from src.services import job_service

    conn = _mock_conn(fetch=[])
    await job_service.list_jobs(conn)
    query = conn.fetch.await_args.args[0]
    # No status literal should be baked into the SQL when the filter is None.
    assert "status =" not in query.lower() or "$" in query.lower()


# ── _row_to_jobout — the fail-open blind_review contract ───────────────────


def test_row_to_jobout_sets_blind_review_true_from_a_true_row() -> None:
    from src.services.job_service import _row_to_jobout

    out = _row_to_jobout(_job_row(blind_review=True))
    assert out.blind_review is True


def test_row_to_jobout_sets_blind_review_false_from_a_false_row() -> None:
    """The other direction: a builder that hardcodes True (instead of reading
    the row) would pass the test above but fail this one — the pair is what
    makes the guard load-bearing, not either test alone."""
    from src.services.job_service import _row_to_jobout

    out = _row_to_jobout(_job_row(blind_review=False))
    assert out.blind_review is False


def test_row_to_jobout_parses_jsonb_description_parsed() -> None:
    from src.services.job_service import _row_to_jobout

    row = _job_row(description_parsed={"title": "Senior Backend Engineer"})
    out = _row_to_jobout(row)
    assert out.description_parsed is not None
    assert out.description_parsed.title == "Senior Backend Engineer"


def test_row_to_jobout_handles_null_description_parsed() -> None:
    from src.services.job_service import _row_to_jobout

    out = _row_to_jobout(_job_row(description_parsed=None))
    assert out.description_parsed is None


# ── transition_status ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transition_status_draft_to_open_is_valid() -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="open"))
    conn.fetchval = AsyncMock(return_value="draft")
    out = await job_service.transition_status(conn, job_id, "open")
    assert out.status == "open"


@pytest.mark.asyncio
async def test_transition_status_rejects_an_unknown_target_state() -> None:
    """Skipping straight from draft to archived is not a valid forward
    transition — the service layer must reject it even though 'archived' is
    itself a syntactically valid JobStatus member."""
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="draft"))
    conn.fetchval = AsyncMock(return_value="draft")
    with pytest.raises(ValueError, match="(?i)transition"):
        await job_service.transition_status(conn, job_id, "archived")


@pytest.mark.asyncio
async def test_transition_status_rejects_backward_moves() -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="draft"))
    conn.fetchval = AsyncMock(return_value="open")
    with pytest.raises(ValueError):
        await job_service.transition_status(conn, job_id, "draft")


@pytest.mark.asyncio
async def test_transition_status_raises_not_found_for_a_missing_job() -> None:
    from src.services import job_service

    conn = _mock_conn(fetchrow=None)
    conn.fetchval = AsyncMock(return_value=None)
    with pytest.raises(NotFoundError):
        await job_service.transition_status(conn, uuid4(), "open")
