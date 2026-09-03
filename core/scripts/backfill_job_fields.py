#!/usr/bin/env python3
"""Backfill title / department / campus onto jobs parsed BEFORE 2026-09-03.

**Why this exists, and why the code change alone was not enough.**
``record_parsed`` now writes the JD extraction onto the job row. That fires on
a parse. Every job on the pilot box was parsed weeks ago, so the fix applies to
none of them: 23 requisitions keep showing their own filenames, Department stays
empty on 20 of 26, and Location on all 26. This is the ROADMAP A7 shape exactly
— a correct fix, gated green, that had never touched a single row — and the
reason ``doctor.sh`` exists.

What it can and cannot recover, without spending an LLM call:

* **Title — recoverable for every parsed row.** ``description_parsed->>'title'``
  already holds the real title ("Multimedia Specialist"); it was written to
  JSONB and to no column. Nothing new is inferred here.
* **Campus — recoverable only where a value already exists**, and only to
  canonicalise it ("Burnaby, BC" -> "Burnaby"). ``description_parsed`` has no
  usable location on any pilot row, because ``jd_extract_v1`` asked for a
  free-text location and got null. **This script deliberately does NOT scan
  ``description_raw`` for a campus name.** Measured: exactly one of 26 JDs
  mentions a campus, and it says "…the Surrey and Vancouver campuses" —
  the role's scope, not its location. A scanner would have been wrong on the
  only row it ever fired for. Campus comes from the UI or from Taleo.
* **Department — not recoverable here.** The field did not exist in
  ``jd_extract_v1``, so no existing extraction contains one. It needs a
  re-parse under ``jd_extract_v2`` (``POST /jobs/{id}/reparse``, or
  ``--reparse-plan`` below to list the ids).

Dry-run by default, and it prints every proposed change:

    docker compose exec worker python scripts/backfill_job_fields.py
    docker compose exec worker python scripts/backfill_job_fields.py --apply

A title is only replaced when the row does not already look right — see
``--help`` on ``--titles``. Nothing here writes over a value a human set.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import asyncpg

from src.campus import canonicalise_location
from src.settings import get_settings

# Rows a title backfill may touch. ``title_provisional`` is the mechanism going
# forward, but it defaults FALSE, so every pre-existing row reads "chosen" —
# which is why this script cannot just trust the flag and has to compare the
# stored title against the extracted one.
_SELECT_SQL = """
SELECT id,
       title,
       title_provisional,
       department,
       location,
       description_parsed->>'title'    AS parsed_title,
       description_parsed->>'location' AS parsed_location,
       (description_parsed IS NOT NULL) AS is_parsed,
       -- Does the CURRENT title appear anywhere in the JD body? A filename
       -- stem never does; a title somebody typed almost always does. This is
       -- the only signal available for rows that predate ``title_provisional``
       -- (which defaults FALSE, so every legacy row claims to be "chosen").
       (strpos(description_raw, title) = 0) AS title_absent_from_jd
  FROM jobs
 ORDER BY created_at
"""

_UPDATE_SQL = """
UPDATE jobs
   SET title = $2,
       title_provisional = FALSE,
       location = $3,
       updated_at = now()
 WHERE id = $1
"""


def _title_fix(row: Any) -> str | None:
    """The title this row should have, or ``None`` to leave it alone.

    Replaced only when the stored title differs from the extracted one AND does
    not appear anywhere in the JD text. A filename stem never appears in the
    body of the document it names; a title somebody typed almost always does,
    and where it does not, leaving it alone is the safe direction — an
    un-improved title costs a reader nothing, a silently renamed requisition
    costs them trust.
    """
    parsed: str | None = row["parsed_title"]
    if not parsed:
        return None
    current: str = row["title"]
    if current.strip() == parsed.strip():
        return None
    if not row["title_absent_from_jd"]:
        return None
    return parsed.strip()


def _location_fix(row: Any) -> str | None:
    """A canonicalised campus for a row that already has some location, or
    ``None``. Never invents one — see the module docstring."""
    current: str | None = row["location"]
    if not current or not current.strip():
        return None
    canonical = canonicalise_location(current)
    return canonical if canonical != current else None


async def _run(*, apply: bool, titles: bool, reparse_plan: bool) -> int:
    settings = get_settings()
    conn = await asyncpg.connect(dsn=settings.postgres_dsn)
    try:
        rows = await conn.fetch(_SELECT_SQL)
        planned: list[tuple[Any, str, str | None]] = []
        needs_reparse: list[str] = []

        for row in rows:
            if not row["is_parsed"]:
                needs_reparse.append(str(row["id"]))
                continue
            if row["department"] is None or not str(row["department"]).strip():
                # Department can only come from a re-parse under v2.
                needs_reparse.append(str(row["id"]))
            new_title = _title_fix(row) if titles else None
            new_location = _location_fix(row)
            if new_title is None and new_location is None:
                continue
            planned.append((row["id"], new_title or row["title"], new_location))
            print(f"{row['id']}")
            if new_title:
                print(f"    title    {row['title']!r}")
                print(f"          -> {new_title!r}")
            if new_location:
                print(f"    campus   {row['location']!r} -> {new_location!r}")

        print(f"\n{len(rows)} jobs, {len(planned)} to change.")

        if reparse_plan:
            print(
                f"\n{len(needs_reparse)} job(s) still need a re-parse for "
                "department (jd_extract_v2). Job ids:"
            )
            for job_id in needs_reparse:
                print(f"  {job_id}")
            print(
                "\nRe-parse each with POST /jobs/<id>/reparse — it only applies "
                "to a job still in 'draft', and it costs one LLM call per job."
            )

        if not planned:
            return 0
        if not apply:
            print("\nDry run. Nothing was written. Re-run with --apply.")
            return 0

        async with conn.transaction():
            for job_id, title, location in planned:
                await conn.execute(_UPDATE_SQL, job_id, title, location)
        print(f"\nApplied {len(planned)} update(s).")
        return 0
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Backfill title/campus onto jobs whose extraction predates the "
            "2026-09-03 write-back. Dry run unless --apply."
        ),
    )
    ap.add_argument("--apply", action="store_true", help="actually write the changes")
    ap.add_argument(
        "--no-titles",
        dest="titles",
        action="store_false",
        help=(
            "leave titles alone; only canonicalise existing campus values. "
            "Use this if a recruiter has already been renaming requisitions "
            "by hand and you would rather not second-guess them"
        ),
    )
    ap.add_argument(
        "--reparse-plan",
        action="store_true",
        help=(
            "also list the job ids that still need a re-parse to acquire a "
            "department (this script cannot infer one)"
        ),
    )
    args = ap.parse_args(argv)
    return asyncio.run(
        _run(
            apply=bool(args.apply),
            titles=bool(args.titles),
            reparse_plan=bool(args.reparse_plan),
        )
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
