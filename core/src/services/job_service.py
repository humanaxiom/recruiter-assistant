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
from uuid import UUID

import asyncpg
from asyncpg import Record

from src.schemas.jobs import JDExtracted

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
    conn: asyncpg.Connection[Record],
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


async def record_parse_failure(
    conn: asyncpg.Connection[Record], job_id: UUID, reason: str
) -> None:
    """Surface a parse failure on the job row. Status stays 'draft'."""
    await conn.execute(_RECORD_FAILURE_SQL, job_id, reason[:_MAX_REASON_CHARS])
    logger.warning("job.parse_failed job_id=%s reason=%s", job_id, reason[:200])
