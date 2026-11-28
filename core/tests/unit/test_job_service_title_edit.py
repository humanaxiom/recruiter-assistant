"""S6 -- a human title edit must clear ``title_provisional`` in the same UPDATE.

Why: ``record_parsed`` replaces the title whenever ``title_provisional`` is
still true (``job_service.py`` ``_RECORD_PARSED_SQL``). A recruiter who types a
title while the JD is still parsing would otherwise watch the parse overwrite
it. Clearing the flag must ride in the SAME statement as the title: a second
statement leaves a window in which the parse lands in between.

The connection is mocked (like ``test_services_job_write.py``), so these pin
the SQL text and its bind arguments; ``tests/integration/
test_job_title_edit_pg.py`` proves what a real row holds afterwards.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.schemas.jobs import JobUpdate

_NOW = dt.datetime(2026, 11, 26, tzinfo=dt.UTC)
_ACTOR: dict[str, Any] = {
    "actor_kind": "service",
    "actor_user_id": None,
    "actor_service": "test-harness",
}


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _row(job_id: UUID) -> _Row:
    return _Row(
        {
            "id": job_id,
            "title": "New Title",
            "department": "Engineering",
            "location": "Remote",
            "employment_type": "full_time",
            "seniority": "senior",
            "min_years": 5,
            "description_raw": "We are looking for a senior backend engineer " * 3,
            "description_parsed": None,
            "status": "draft",
            "retention_days": 180,
            "shortlist_top_percent": 100,
            "blind_review": True,
            "failure_reason": None,
            "created_by": "api",
            "created_at": _NOW,
            "updated_at": _NOW,
            "parsed_at": None,
            "closed_at": None,
        }
    )


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _conn(job_id: UUID) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=_row(job_id))
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


async def _update(body: dict[str, Any]) -> tuple[str, list[Any], MagicMock]:
    from src.services import job_service

    job_id = uuid4()
    conn = _conn(job_id)
    await job_service.update_job(conn, job_id, JobUpdate.model_validate(body), **_ACTOR)
    query, *args = conn.fetchrow.await_args.args
    return " ".join(str(query).split()), args, conn


def _set_clause(query: str) -> str:
    """Only the SET list: RETURNING names every column, title_provisional too."""
    match = re.search(r"\bSET\b(.*?)\bWHERE\b", query, re.IGNORECASE)
    assert match is not None, query
    return match.group(1)


def _sets_provisional_false(query: str) -> bool:
    pattern = r"title_provisional\s*=\s*false"
    return re.search(pattern, _set_clause(query), re.IGNORECASE) is not None


@pytest.mark.asyncio
async def test_a_title_edit_also_sets_title_provisional_false() -> None:
    query, _args, _conn_ = await _update({"title": "Research Analyst"})
    assert _sets_provisional_false(query), query


@pytest.mark.asyncio
async def test_the_flag_is_cleared_in_the_same_update_as_the_title() -> None:
    query, args, conn = await _update({"title": "Research Analyst"})
    assert "Research Analyst" in args
    assert query.upper().startswith("UPDATE JOBS SET")
    assert re.search(r"\btitle\s*=\s*\$2", query), query
    assert _sets_provisional_false(query)
    conn.fetchrow.assert_awaited_once()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_flag_is_a_literal_so_no_extra_bind_argument_appears() -> None:
    _query, args, _conn_ = await _update({"title": "Research Analyst"})
    assert len(args) == 2, args  # job_id and the title only


@pytest.mark.asyncio
async def test_a_title_edit_alongside_other_fields_still_clears_the_flag() -> None:
    query, args, _conn_ = await _update(
        {"title": "Research Analyst", "department": "Library", "min_years": 4}
    )
    assert _sets_provisional_false(query), query
    assert {"Research Analyst", "Library", 4} <= set(args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"department": "Library"},
        {"location": "Surrey"},
        {"min_years": 4},
        {"seniority": "mid"},
        {"employment_type": "intern"},
        {"blind_review": False},
        {"retention_days": 90},
        {"department": None, "min_years": None},
    ],
    ids=lambda b: "+".join(b),
)
async def test_an_update_without_a_title_does_not_touch_title_provisional(
    body: dict[str, Any],
) -> None:
    query, _args, _conn_ = await _update(body)
    assert "title_provisional" not in _set_clause(query).lower(), query


@pytest.mark.asyncio
async def test_an_empty_payload_issues_no_update_at_all() -> None:
    from src.services import job_service

    job_id = uuid4()
    conn = _conn(job_id)
    await job_service.update_job(conn, job_id, JobUpdate.model_validate({}), **_ACTOR)
    for call in conn.fetchrow.await_args_list:
        assert "update jobs" not in str(call.args[0]).lower()
