"""Persistence for the human decision record (``decision_records``).

Append-only: this module only ever INSERTs and SELECTs. A new decision for the
same (job, resume) supersedes the old one by being newer (``seq``); nothing is
updated or deleted, and rank/score on the shortlist are never touched.

The audit row is written in the SAME transaction as the INSERT, so there is
never a decision without its audit row or an audit row for a decision that did
not land. The audit ``details`` hold ids and the snapshot, never the rationale.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from src.api.deps import actor_fields_from_user
from src.schemas.auth import User
from src.schemas.decisions import DecisionBadge, DecisionIn, DecisionOut
from src.schemas.matching import ShortlistEntry
from src.services import DbConn, audit_service

_COLUMNS = (
    "id, job_id, resume_id, decision, rationale, decided_by, decided_by_role, "
    "decided_at, snap_rank, snap_score_final, snap_blind_review"
)

_LATEST_SQL = f"""
SELECT {_COLUMNS}
FROM decision_records
WHERE job_id = $1 AND resume_id = $2
ORDER BY seq DESC
LIMIT 1
"""

_INSERT_SQL = f"""INSERT INTO decision_records
    (job_id, resume_id, decision, rationale, decided_by, decided_by_user_id,
     decided_by_role, snap_rank, snap_score_final, snap_entry_id,
     snap_blind_review)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
RETURNING {_COLUMNS}
"""

_HISTORY_SQL = f"""
SELECT {_COLUMNS}
FROM decision_records
WHERE job_id = $1 AND resume_id = $2
ORDER BY seq DESC
"""

# No JOIN: the badge read must not be able to project candidate columns.
# Withdrawn candidates are excluded by a sub-select; their history is kept and
# reappears on reinstatement.
_LATEST_FOR_JOB_SQL = """
SELECT DISTINCT ON (resume_id) resume_id, decision, decided_by, decided_at
FROM decision_records d
WHERE job_id = $1
  AND EXISTS (
      SELECT 1 FROM resumes r
      WHERE r.id = d.resume_id AND r.withdrawn_at IS NULL
  )
ORDER BY resume_id, seq DESC
"""

_LATEST_FOR_JOB_SCOPED_SQL = """
SELECT DISTINCT ON (resume_id) resume_id, decision, decided_by, decided_at
FROM decision_records d
WHERE job_id = $1
  AND EXISTS (
      SELECT 1 FROM resumes r
      WHERE r.id = d.resume_id AND r.withdrawn_at IS NULL
  )
  AND EXISTS (
      SELECT 1 FROM job_assignees ja
      WHERE ja.job_id = d.job_id AND ja.user_id = $2
  )
ORDER BY resume_id, seq DESC
"""


def _out(row: Any) -> DecisionOut:
    return DecisionOut(
        id=row["id"],
        job_id=row["job_id"],
        resume_id=row["resume_id"],
        decision=row["decision"],
        rationale=row["rationale"],
        decided_by=row["decided_by"],
        decided_by_role=row["decided_by_role"],
        decided_at=row["decided_at"],
        snap_rank=row["snap_rank"],
        snap_score_final=row["snap_score_final"],
        snap_blind_review=row["snap_blind_review"],
    )


async def record(
    conn: DbConn, *, entry: ShortlistEntry, user: User, body: DecisionIn
) -> tuple[DecisionOut, bool]:
    """Record a decision. Returns ``(decision, created)``.

    ``created`` is False when the latest decision for this (job, resume) is an
    exact repeat by the same reviewer (same decision and rationale): nothing is
    written and nothing is audited."""
    actor_kind, actor_user_id, actor_service = actor_fields_from_user(user)
    async with conn.transaction():
        latest = await conn.fetchrow(_LATEST_SQL, entry.job_id, entry.resume_id)
        if (
            latest is not None
            and latest["decided_by"] == user.cas_username
            and latest["decision"] == body.decision
            and latest["rationale"] == body.rationale
        ):
            return _out(latest), False

        inserted = await conn.fetchrow(
            _INSERT_SQL,
            entry.job_id,
            entry.resume_id,
            body.decision,
            body.rationale,
            user.cas_username,
            actor_user_id,  # NULL for the dev sentinel (not a real users row)
            user.role,
            entry.rank,
            entry.score_final,
            entry.id,
            entry.blinded,
        )
        if inserted is None:  # pragma: no cover - INSERT ... RETURNING
            raise RuntimeError("decision insert returned no row")
        await audit_service.record_audit(
            conn,
            actor_kind=actor_kind,
            actor_user_id=actor_user_id,
            actor_service=actor_service,
            action="record_decision",
            subject_type="resume",
            subject_id=entry.resume_id,
            job_id=entry.job_id,
            details={
                "decision_id": str(inserted["id"]),
                "decision": inserted["decision"],
                "rank": entry.rank,
                "score_final": entry.score_final,
                "superseded_decision_id": (
                    str(latest["id"]) if latest is not None else None
                ),
                "blind_review": entry.blinded,
            },
        )
    return _out(inserted), True


async def history(conn: DbConn, job_id: UUID, resume_id: UUID) -> list[DecisionOut]:
    """Every decision for the pair, newest first."""
    rows = await conn.fetch(_HISTORY_SQL, job_id, resume_id)
    return [_out(r) for r in rows]


async def latest_for_job(
    conn: DbConn, job_id: UUID, user_id: UUID | None
) -> list[DecisionBadge]:
    """Latest decision per resume for a job (badges, no rationale). ``user_id``
    row-scopes a hiring manager to their assigned jobs; ``None`` is unscoped."""
    if user_id is None:
        rows = await conn.fetch(_LATEST_FOR_JOB_SQL, job_id)
    else:
        rows = await conn.fetch(_LATEST_FOR_JOB_SCOPED_SQL, job_id, user_id)
    return [
        DecisionBadge(
            resume_id=r["resume_id"],
            decision=r["decision"],
            decided_by=r["decided_by"],
            decided_at=r["decided_at"],
        )
        for r in rows
    ]
