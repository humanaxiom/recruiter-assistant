"""Job write-back — the worker's half of the job lifecycle.

Phase 3 ports ONLY the two worker-side mutations from hris
``apps/api/src/api/services/job_service.py``; the CRUD / status state machine
lands with the routes in Phase 6.

Two deliberate cuts from the hris source:

* **No ``refine_title`` branch.** hris had a second UPDATE that also wrote
  ``title = $4, title_autofilled = FALSE`` so a bulk-ingest job whose title was
  derived from a filename could be corrected once by the LLM. Bulk ingest is
  cut, and ``jobs.title_autofilled`` does not exist in ``src/models/ddl.py`` —
  porting the branch would be an ``UndefinedColumnError`` waiting to happen.
  ``record_parsed`` never touches the ``title`` column at all.
* **No ``'failed'`` status.** ``job_status`` is ('draft','open','closed',
  'archived') — there is no failed state for a job, so a parse failure only
  surfaces on ``failure_reason`` and the row stays in 'draft' for a retry.

``record_parsed`` carries an optimistic-concurrency guard: the UPDATE applies
only while the row is still ``status = 'draft'``. If a concurrent transition
moved it out from under us, 0 rows apply and we return ``False`` — the caller
(``parse_job``) turns that into ``"stale"`` and MUST NOT enqueue an outbox row,
because a projection event for a write that never landed would corrupt the
graph.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any
from uuid import UUID

from src.errors import NotFoundError
from src.schemas.jobs import JDExtracted, JobCreate, JobListItem, JobOut
from src.services import DbConn

logger = logging.getLogger(__name__)

_MAX_REASON_CHARS = 1000

_RECORD_PARSED_SQL = """
UPDATE jobs SET
    description_parsed = $2::jsonb,
    parsed_at = $3,
    failure_reason = NULL,
    updated_at = now()
WHERE id = $1 AND status = 'draft'
"""

_RECORD_FAILURE_SQL = """
UPDATE jobs SET
    failure_reason = $2,
    updated_at = now()
WHERE id = $1 AND status = 'draft'
"""


async def record_parsed(
    conn: DbConn,
    job_id: UUID,
    extracted: JDExtracted,
    parsed_at: dt.datetime,
) -> bool:
    """Write the LLM extraction back onto the job row.

    Returns ``True`` when the UPDATE applied (the job was still 'draft'), and
    ``False`` when a concurrent transition already moved it — the worker treats
    ``False`` as "drop it on the floor, do not retry, do not enqueue".
    """
    result = await conn.execute(
        _RECORD_PARSED_SQL,
        job_id,
        json.dumps(extracted.model_dump()),
        parsed_at,
    )
    applied = result.endswith(" 1")
    if not applied:
        logger.info("job.record_parsed.stale job_id=%s", job_id)
    return applied


async def record_parse_failure(conn: DbConn, job_id: UUID, reason: str) -> None:
    """Surface a parse failure on the job row. Status stays 'draft'."""
    await conn.execute(_RECORD_FAILURE_SQL, job_id, reason[:_MAX_REASON_CHARS])
    logger.warning("job.parse_failed job_id=%s reason=%s", job_id, reason[:200])


# ── CRUD / status state machine (Phase 6) ───────────────────────────────────

_JOB_COLS = (
    "id, title, department, location, employment_type, seniority, min_years, "
    "description_raw, description_parsed, status, retention_days, "
    "blind_review, failure_reason, created_by, created_at, updated_at, "
    "parsed_at, closed_at"
)

_INSERT_JOB_SQL = f"""
INSERT INTO jobs (
    title, department, location, employment_type, seniority, min_years,
    description_raw, retention_days, blind_review, created_by
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING {_JOB_COLS}
"""

_GET_JOB_SQL = f"SELECT {_JOB_COLS} FROM jobs WHERE id = $1"

_LIST_JOBS_BASE_SQL = (
    "SELECT id, title, department, status, created_at, parsed_at FROM jobs"
)

_UPDATE_STATUS_SQL = f"""
UPDATE jobs SET status = $2, updated_at = now() WHERE id = $1
RETURNING {_JOB_COLS}
"""

# Strictly-forward state graph: draft -> open -> closed -> archived. No
# skipping, no same-state no-op, no backward move — anything not in this map
# (as the CURRENT status's single allowed next state) is rejected.
_FORWARD_TRANSITIONS: dict[str, str] = {
    "draft": "open",
    "open": "closed",
    "closed": "archived",
}


def _row_to_jobout(row: Any) -> JobOut:
    """THE single place that builds a ``JobOut`` from a raw DB row.

    ADR-006 / Phase 2 security "low": ``JobOut.blind_review`` defaults to
    ``False`` (fail-OPEN) if a builder ever omits it — this function MUST
    read ``row["blind_review"]`` explicitly, never rely on the pydantic
    default.
    """
    raw = dict(row)
    desc_parsed_raw = raw["description_parsed"]
    if isinstance(desc_parsed_raw, str):
        desc_parsed_raw = json.loads(desc_parsed_raw)
    description_parsed = (
        JDExtracted.model_validate(desc_parsed_raw)
        if desc_parsed_raw is not None
        else None
    )
    return JobOut(
        id=raw["id"],
        title=raw["title"],
        department=raw["department"],
        location=raw["location"],
        employment_type=raw["employment_type"],
        seniority=raw["seniority"],
        min_years=raw["min_years"],
        description_raw=raw["description_raw"],
        description_parsed=description_parsed,
        status=raw["status"],
        retention_days=raw["retention_days"],
        blind_review=raw["blind_review"],
        failure_reason=raw["failure_reason"],
        created_by=raw["created_by"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        parsed_at=raw["parsed_at"],
        closed_at=raw["closed_at"],
    )


async def create_job(
    conn: DbConn, payload: JobCreate, *, created_by: str | None
) -> JobOut:
    """Insert a new job row (status='draft', the DDL default) and return the
    full ``JobOut`` built from the inserted row."""
    row = await conn.fetchrow(
        _INSERT_JOB_SQL,
        payload.title,
        payload.department,
        payload.location,
        payload.employment_type,
        payload.seniority,
        payload.min_years,
        payload.description_raw,
        payload.retention_days,
        payload.blind_review,
        created_by,
    )
    return _row_to_jobout(row)


async def get_job(conn: DbConn, job_id: UUID) -> JobOut:
    """Fetch one job by id. Raises ``NotFoundError`` when it does not resolve."""
    row = await conn.fetchrow(_GET_JOB_SQL, job_id)
    if row is None:
        raise NotFoundError(f"job {job_id} not found", job_id=str(job_id))
    return _row_to_jobout(row)


async def list_jobs(
    conn: DbConn,
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> list[JobListItem]:
    """Light rows for list views. ``status`` omitted means "every status" —
    never silently scoped to one status when the filter is absent."""
    if status is not None:
        query = (
            f"{_LIST_JOBS_BASE_SQL} WHERE status = $1 "
            "ORDER BY created_at DESC LIMIT $2 OFFSET $3"
        )
        rows = await conn.fetch(query, status, limit, offset)
    else:
        query = f"{_LIST_JOBS_BASE_SQL} ORDER BY created_at DESC LIMIT $1 OFFSET $2"
        rows = await conn.fetch(query, limit, offset)
    # Explicit field-by-field projection (not `dict(r)`) — the real SQL only
    # selects these six columns, but a test double (or a future query that
    # widens its SELECT) may hand back a row carrying extra keys; JobListItem
    # is extra="forbid", so trusting the row's own key set would be fragile.
    return [
        JobListItem(
            id=r["id"],
            title=r["title"],
            department=r["department"],
            status=r["status"],
            created_at=r["created_at"],
            parsed_at=r["parsed_at"],
        )
        for r in rows
    ]


async def transition_status(conn: DbConn, job_id: UUID, to: str) -> JobOut:
    """Validate ``to`` against the strictly-forward state graph and apply it.

    Raises ``NotFoundError`` when the job doesn't exist, and ``ValueError``
    (the route's 409 material) for any transition not in the graph —
    including a same-state no-op and a backward move.
    """
    current = await conn.fetchval("SELECT status FROM jobs WHERE id = $1", job_id)
    if current is None:
        raise NotFoundError(f"job {job_id} not found", job_id=str(job_id))
    if _FORWARD_TRANSITIONS.get(current) != to:
        raise ValueError(f"invalid status transition: {current!r} -> {to!r}")
    row = await conn.fetchrow(_UPDATE_STATUS_SQL, job_id, to)
    return _row_to_jobout(row)
