"""The outbox drainer — projects ``job.parsed``/``resume.parsed`` events into
Neo4j. Phase 4b of the ranking-engine build.

Ported behaviourally from hris's TWO ``project_to_graph`` definitions
(``apps/worker/src/worker/tasks.py`` and the shadowing overload in
``resume_tasks.py``) — deliberately unified into ONE drainer here instead of
reproducing hris's shadow-the-old-import trick, per ``docs/EXTRACTION_PLAN.md``
(4b row target file map).

Human-locked deviations:

* **Decision 2 — poison rows are capped, not retried forever.** hris's
  ``SELECT ... WHERE delivered_at IS NULL ORDER BY id LIMIT $1`` has no upper
  bound on ``delivery_attempts``. Here the SELECT itself excludes rows at/past
  ``settings.outbox_max_delivery_attempts`` — dead-lettered, never selected
  again, never deleted.
* **R3 (HIGH) — ``last_error`` and every log record are PII-free.** hris's
  ``f"{type(exc).__name__}: {exc}"`` echoes the exception's ``str()``, which
  for a Neo4j ``ClientError``/``CypherTypeError`` can include a Cypher
  parameter value — and N1 (ADR-007 §7) lets structured résumé fields ride the
  outbox unscrubbed. Only the exception's CLASS NAME is ever recorded.
* **R7 (MED) — ``FOR UPDATE SKIP LOCKED``** so two concurrent drainers each
  deliver a disjoint set instead of double-projecting the same rows.
* The whole batch (SELECT + per-row dispatch + per-row UPDATE) runs under ONE
  Postgres transaction — that is what makes ``FOR UPDATE SKIP LOCKED``
  meaningful; a per-row transaction would release each lock before a
  concurrent drainer's SELECT even ran.
* Batch size and the dead-letter cap are read from ``settings`` — never
  hard-coded (hris hard-codes both).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.settings import get_settings
from src.worker.resume_tasks import project_resume
from src.worker.tasks import _project_job

log = logging.getLogger(__name__)

_SELECT_SQL = (
    "SELECT id, aggregate, aggregate_id, event_type, payload "
    "FROM outbox "
    "WHERE delivered_at IS NULL AND delivery_attempts < $1 "
    "ORDER BY id ASC "
    "LIMIT $2 "
    "FOR UPDATE SKIP LOCKED"
)

_MARK_DELIVERED_SQL = "UPDATE outbox SET delivered_at = now() WHERE id = $1"

_MARK_FAILED_SQL = (
    "UPDATE outbox SET delivery_attempts = delivery_attempts + 1, "
    "last_error = $2 WHERE id = $1"
)


async def project_to_graph(ctx: dict[str, Any], batch: int | None = None) -> int:
    """Drain up to ``batch`` (or ``settings.outbox_drain_batch_size``)
    undelivered, non-dead-lettered outbox rows; project each into Neo4j.

    Returns the number of rows successfully delivered. A row whose projection
    raises does NOT block its batch-mates: ``delivery_attempts`` increments
    and the row is retried on the next drain, up to the dead-letter cap.
    """
    pool = ctx["pg_pool"]
    neo4j = ctx["neo4j"]
    llm = ctx["llm"]
    embedder = ctx["embedder"]

    settings = get_settings()
    limit = batch if batch is not None else settings.outbox_drain_batch_size
    cap = settings.outbox_max_delivery_attempts

    delivered = 0
    rows: list[Any] = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(_SELECT_SQL, cap, limit)
            for row in rows:
                raw_payload = row["payload"]
                payload = (
                    json.loads(raw_payload)
                    if isinstance(raw_payload, str)
                    else raw_payload
                )
                try:
                    if row["event_type"] == "job.parsed":
                        await _project_job(
                            neo4j,
                            row["aggregate_id"],
                            payload,
                            llm=llm,
                            embedder=embedder,
                        )
                    elif row["event_type"] == "resume.parsed":
                        await project_resume(
                            neo4j,
                            resume_id=row["aggregate_id"],
                            payload=payload,
                            llm=llm,
                            embedder=embedder,
                        )
                    else:
                        log.warning(
                            "outbox.unknown_event_type event_type=%s outbox_id=%s",
                            row["event_type"],
                            row["id"],
                        )
                    await conn.execute(_MARK_DELIVERED_SQL, row["id"])
                    delivered += 1
                except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
                    # R3: only the exception's CLASS NAME is ever recorded —
                    # never str(exc), which can echo a Cypher parameter value
                    # carrying N1's unscrubbed structured résumé fields.
                    log.error(
                        "outbox.project_failed outbox_id=%s event_type=%s "
                        "error_type=%s",
                        row["id"],
                        row["event_type"],
                        type(exc).__name__,
                    )
                    await conn.execute(_MARK_FAILED_SQL, row["id"], type(exc).__name__)

    if delivered:
        log.info("outbox.drained delivered=%d attempted=%d", delivered, len(rows))
    return delivered


__all__ = ["project_to_graph"]
