#!/usr/bin/env python3
"""Backfill ``jobs.blind_review`` onto rows created under the OLD default.

**Why this exists, and why the DDL/schema flip alone was not enough.** The
2026-09-09 sponsor decision reverses ``blind_review`` from default-ON back to
default-OFF (see ``src/models/ddl.py``'s ``ALTER TABLE jobs ALTER COLUMN
blind_review SET DEFAULT FALSE``, ``src/schemas/jobs.py``'s ``JobCreate``,
and ``src/services/job_source_service.py``'s ``ExternalJobUpsert``). None of
that touches a single EXISTING row: a default only ever governs an INSERT
that omits the column, so every job already on the pilot box that was
created under the old default keeps reading ``blind_review = TRUE`` forever.
This is the ROADMAP A7 shape exactly — a correct fix, gated green, that
never touched a single row — and the reason ``doctor.sh`` and this script
both exist.

**What it flips, and what it deliberately leaves alone.** Only jobs that are
currently ``blind_review = TRUE`` **and** were never toggled by a human (no
``blind_review_toggled`` row in ``audit_log`` for that job) are eligible. A
job a recruiter already flipped by hand — via the per-job PATCH toggle this
reversal does not touch — keeps their own choice, even if that choice
happens to currently read ``TRUE`` (toggled away and back). A job already
``FALSE`` is left alone regardless of audit history.

Dry-run by default, and it prints every proposed change:

    docker compose exec worker python scripts/backfill_blind_review_default.py
    docker compose exec worker python scripts/backfill_blind_review_default.py --apply

Each applied flip is written inside one transaction and paired with its own
``audit_log`` row (``action='blind_review_toggled'``, service actor
``blind-review-backfill``) — an unattributed write is not acceptable for a
security-relevant column, even one flipped by a script instead of a person.
"""

from __future__ import annotations

import argparse
import asyncio

import asyncpg

from src.services.audit_service import record_audit
from src.settings import get_settings

# Jobs currently ``TRUE`` with no prior human toggle recorded against them.
_SELECT_SQL = """
SELECT j.id
  FROM jobs j
 WHERE j.blind_review = TRUE
   AND NOT EXISTS (
       SELECT 1 FROM audit_log a
        WHERE a.action = 'blind_review_toggled'
          AND a.subject_type = 'job'
          AND a.subject_id = j.id
   )
 ORDER BY j.created_at
"""

_UPDATE_SQL = """
UPDATE jobs
   SET blind_review = FALSE,
       updated_at = now()
 WHERE id = $1
"""


async def _run(*, apply: bool) -> int:
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.postgres_dsn)
    try:
        rows = await conn.fetch(_SELECT_SQL)
        job_ids = [row["id"] for row in rows]

        for job_id in job_ids:
            print(f"{job_id}")
            print("    blind_review  True -> False")

        print(f"\n{len(job_ids)} job(s) to change.")

        if not job_ids:
            return 0
        if not apply:
            print("\nDry run. Nothing was written. Re-run with --apply.")
            return 0

        async with conn.transaction():
            for job_id in job_ids:
                await conn.execute(_UPDATE_SQL, job_id)
                await record_audit(
                    conn,
                    actor_kind="service",
                    actor_user_id=None,
                    actor_service="blind-review-backfill",
                    action="blind_review_toggled",
                    subject_type="job",
                    subject_id=job_id,
                    job_id=job_id,
                    details={
                        "old": True,
                        "new": False,
                        "reason": "default reversed 2026-09-09",
                    },
                )
        print(f"\nApplied {len(job_ids)} update(s).")
        return 0
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Backfill blind_review=FALSE onto jobs created under the old "
            "default-ON behaviour, reversed 2026-09-09. Dry run unless "
            "--apply."
        ),
    )
    ap.add_argument("--apply", action="store_true", help="actually write the changes")
    args = ap.parse_args(argv)
    return asyncio.run(_run(apply=bool(args.apply)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
