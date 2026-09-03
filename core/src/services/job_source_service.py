"""Upsert jobs discovered from an external ATS (ADR-046).

Ported from hris ``apps/api/src/api/services/job_source_service.py``. This is
the write half of the Taleo source; the fetching half is
``src.pipeline.sources.taleo`` and runs only when ``TALEO_ENABLED`` is true.

**Nothing here reaches the network.** It takes already-parsed records and puts
them in Postgres, which is why it is testable against a real database with no
egress at all.

DEVIATIONS from the hris source, all consequences of this repo's schema:

* **No seeded system user.** hris types ``jobs.created_by`` as a UUID FK to
  ``users`` and therefore has to seed a ``SYSTEM_USER_ID`` row in a migration
  just to own synced jobs. Here ``created_by`` is a nullable TEXT actor label
  (Phase 0 DDL), so the sync simply writes :data:`SYNC_ACTOR` and the whole
  seeded-user apparatus disappears.
* **No ``approval_required_2nd_review``.** That column does not exist here —
  the 2nd-review workflow was cut in Phase 2.
* **``description_sha256`` is written.** This repo dedups bulk-JD uploads on
  that hash (``job_service._JOB_BY_SHA_SQL``); a synced job that left it NULL
  would be invisible to that check, so the same JD could be created twice by
  two different routes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from src.campus import canonicalise_location
from src.services import DbConn, audit_service

logger = logging.getLogger(__name__)

#: The ``created_by`` label on every job this sync creates. A TEXT actor, not a
#: user id — see the module docstring. Audit trails for a synced job resolve to
#: this until a recruiter takes an action on it.
SYNC_ACTOR = "taleo-sync"

#: The only ``jobs.source`` value this module writes. ``manual`` (the column
#: default) is what every human-created job carries.
TALEO_SOURCE = "taleo"

#: ``audit_log.subject_type`` for a sync run. The subject of "the Taleo sync
#: ran" is the SOURCE, not any individual job — and it has to be *something*,
#: because ``audit_log.subject_id`` is NOT NULL and a run that changed nothing
#: must still leave a row. That is the case the audit exists for: zero rows is
#: how "the template changed and we now parse nothing" looks exactly like "a
#: quiet day with no new postings".
SOURCE_SUBJECT_TYPE = "job_source"

#: A stable identity for the Taleo source itself, derived rather than invented:
#: UUID5 over a fixed URN, so it is the same value in every database, is
#: reproducible from the string, and cannot collide with a real ``jobs.id``
#: (which is v4). Preferred over widening ``audit_log.subject_id`` to nullable
#: — that table is append-only and security-relevant, and one feature is not a
#: reason to relax a NOT NULL every reader currently relies on.
TALEO_SOURCE_ID = uuid5(NAMESPACE_URL, "urn:recruiter-assistant:job-source:taleo")


@dataclass(frozen=True)
class ExternalJobUpsert:
    """One requisition, flattened into exactly what the upsert writes."""

    external_id: str
    external_url: str
    title: str
    description_raw: str
    department: str | None = None
    location: str | None = None
    employment_type: str | None = None
    blind_review: bool = True


@dataclass(frozen=True)
class UpsertResult:
    job_id: UUID
    was_inserted: bool
    #: True when this run CHANGED the JD text (or created the row). The caller
    #: re-enqueues ``parse_job`` on exactly these — re-parsing an unchanged JD
    #: would burn an LLM call and could change extracted requirements
    #: underneath a shortlist someone is reading (ROADMAP §5).
    description_changed: bool


_UPSERT_SQL = """
-- The CTE snapshots the PRE-upsert description so the outer SELECT can tell
-- whether the JD text actually changed. Postgres forbids referencing EXCLUDED
-- in a RETURNING clause (it is visible only inside ON CONFLICT DO UPDATE SET),
-- so: snapshot, upsert, then compare.
WITH existing AS (
    SELECT id, description_raw AS old_desc
      FROM jobs
     WHERE source = $8 AND external_id = $9
),
upserted AS (
    INSERT INTO jobs (
        title, department, location, employment_type,
        description_raw, description_sha256, retention_days,
        blind_review, status, created_by,
        source, external_id, external_url, external_last_seen_at
    ) VALUES (
        $1, $2, $3, $4,
        $5, $6, 180,
        $7, 'draft', $11,
        $8, $9, $10, now()
    )
    -- blind_review is set on INSERT ONLY — deliberately absent from the SET
    -- below, so a re-sync never undoes a recruiter's later decision to
    -- un-blind a requisition. The same reasoning applies to every field a
    -- human can edit that the ATS does not own.
    ON CONFLICT (source, external_id) WHERE external_id IS NOT NULL
    DO UPDATE SET
        title           = EXCLUDED.title,
        -- COALESCE, not a bare assignment (2026-09-03). The ATS owns these
        -- fields for a job it owns, so a value it SUPPLIES still wins over a
        -- local edit — but a listing that carries no department or campus must
        -- not wipe one that a recruiter filled in by hand. Before this, a
        -- posting whose accordion omitted the location silently blanked the
        -- column on every nightly sync, which makes the UI's override field a
        -- promise the product does not keep.
        department      = COALESCE(EXCLUDED.department, jobs.department),
        location        = COALESCE(EXCLUDED.location, jobs.location),
        employment_type = COALESCE(EXCLUDED.employment_type,
                                   jobs.employment_type),
        description_raw = EXCLUDED.description_raw,
        description_sha256 = EXCLUDED.description_sha256,
        external_url    = EXCLUDED.external_url,
        external_last_seen_at = now(),
        -- A CHANGED JD invalidates its own extraction. Clearing all three
        -- fields together is what makes the row look exactly like a freshly
        -- created one to the parse path; clearing only `description_parsed`
        -- would leave a `parsed_at` timestamp asserting the parse happened.
        description_parsed = CASE
            WHEN jobs.description_raw IS DISTINCT FROM EXCLUDED.description_raw
            THEN NULL ELSE jobs.description_parsed END,
        parsed_at = CASE
            WHEN jobs.description_raw IS DISTINCT FROM EXCLUDED.description_raw
            THEN NULL ELSE jobs.parsed_at END,
        failure_reason = CASE
            WHEN jobs.description_raw IS DISTINCT FROM EXCLUDED.description_raw
            THEN NULL ELSE jobs.failure_reason END,
        -- A posting that comes BACK upstream reopens as draft. Note this is
        -- `archived -> draft`, which the job_service state machine does not
        -- list (it is strictly forward). That is deliberate and lives here
        -- rather than there: it is a system-initiated correction of an
        -- archive the sweep performed, not a human transition.
        status = CASE
            WHEN jobs.status = 'archived' THEN 'draft'::job_status
            ELSE jobs.status END,
        updated_at = now()
    RETURNING id, (xmax = 0) AS was_inserted, description_raw AS new_desc
)
SELECT
    upserted.id,
    upserted.was_inserted,
    -- True for a fresh insert (old_desc is NULL through the LEFT JOIN) and
    -- for an update whose JD text genuinely differs.
    (upserted.new_desc IS DISTINCT FROM existing.old_desc) AS desc_changed
FROM upserted
LEFT JOIN existing ON existing.id = upserted.id
"""


async def upsert_external_job(conn: DbConn, payload: ExternalJobUpsert) -> UpsertResult:
    """Insert or update one externally-sourced job. Idempotent.

    Keyed on ``(source, external_id)`` via the partial unique index, so the
    same feed produces the same rows however many times it is replayed.
    """
    row = await conn.fetchrow(
        _UPSERT_SQL,
        payload.title,
        payload.department,
        # Taleo is the third campus source, so it gets the same canonicalisation
        # as the JD parse and the UI: "Burnaby, BC" from a listing cell must not
        # sit beside "Burnaby" from a form in the same column.
        canonicalise_location(payload.location),
        payload.employment_type,
        payload.description_raw,
        hashlib.sha256(payload.description_raw.encode("utf-8")).hexdigest(),
        payload.blind_review,
        TALEO_SOURCE,
        payload.external_id,
        payload.external_url,
        SYNC_ACTOR,
    )
    if row is None:
        # Not reachable through the normal path: INSERT ... ON CONFLICT DO
        # UPDATE ... RETURNING always yields exactly one row. It becomes
        # reachable if the ON CONFLICT target ever stops matching the partial
        # unique index, in which case DO UPDATE never fires and the statement
        # can conflict away to nothing — a silent no-op that would look like a
        # working sync writing zero jobs. Fail loudly instead.
        raise RuntimeError(
            f"upsert returned no row for {TALEO_SOURCE}/{payload.external_id} — "
            "the ON CONFLICT target has probably drifted from the "
            "jobs_source_external_id_idx partial unique index"
        )
    return UpsertResult(
        job_id=row["id"],
        was_inserted=row["was_inserted"],
        description_changed=row["desc_changed"],
    )


_ARCHIVE_SQL = """
UPDATE jobs
   SET status = 'archived', updated_at = now()
 WHERE source = $1
   AND status <> 'archived'
   AND (external_last_seen_at IS NULL OR external_last_seen_at < $2)
RETURNING id
"""


async def mark_missing_as_archived(
    conn: DbConn, *, run_started_at: dt.datetime
) -> list[UUID]:
    """Archive taleo-sourced jobs this run did not observe upstream.

    A posting that disappears from the careers site has been taken down, and
    the honest local state is ``archived``.

    **Archived, never deleted**, and the constraint is not squeamishness: a
    shortlist, its evidence and its audit trail all reference the job. Deleting
    it would cascade away a hiring decision's paper trail because a public web
    page changed.

    **Scoped to ``source = 'taleo'``.** A manually-created job has no
    ``external_last_seen_at`` and must never be swept — which is why the
    predicate leads with source rather than with the timestamp.

    ``run_started_at`` is the run's OWN start, not ``now()``: rows the run
    touched carry an ``external_last_seen_at`` later than it, so the comparison
    is "did this run see it", not "was it seen recently".
    """
    rows = await conn.fetch(_ARCHIVE_SQL, TALEO_SOURCE, run_started_at)
    ids = [r["id"] for r in rows]
    if ids:
        logger.info("taleo.sync.archived count=%d", len(ids))
    return ids


async def write_sync_audit(
    conn: DbConn,
    *,
    inserted: int,
    updated: int,
    archived: int,
    triggered_by: str | None = None,
) -> None:
    """One ``audit_log`` row per sync run.

    ADR-046's tamper-evidence obligation. Without it a sync that silently
    stopped working — a template change yielding zero rows, a firewall rule
    quietly removed — looks identical to a day with no new postings.
    """
    details: dict[str, Any] = {
        "inserted": inserted,
        "updated": updated,
        "archived": archived,
    }
    if triggered_by:
        details["triggered_by"] = triggered_by
    await audit_service.record_audit(
        conn,
        actor_kind="service",
        actor_user_id=None,
        actor_service=SYNC_ACTOR,
        action="taleo_sync",
        # The subject is the SOURCE, not any one job — a run that touched
        # nothing still has to leave a row, which is the whole point (see
        # below). ``audit_log.subject_id`` is NOT NULL, and rather than widen
        # a security-relevant append-only table or invent a sentinel, the
        # source gets a real, stable identity of its own.
        subject_type=SOURCE_SUBJECT_TYPE,
        subject_id=TALEO_SOURCE_ID,
        details=details,
    )


def build_description_raw(
    summary: str, structured: dict[str, str], pdf_url: str | None
) -> str:
    """Pad a Taleo posting's inline summary with its structured fields.

    Not cosmetic. ``JobCreate.description_raw`` has ``min_length=50``, and
    several real SIMOFRAS postings put the substance in a linked PDF and leave
    a sentence or two inline — so without this the sync would silently drop
    exactly the postings whose JD lives elsewhere. The structured fields also
    give the JD extractor real signal it would otherwise not see.

    Never returns empty: ``description_raw`` is NOT NULL, and one malformed
    posting must not fail the whole run.
    """
    parts: list[str] = []
    if summary:
        parts.append(summary.strip())
    if structured:
        rendered = "\n".join(f"- {k}: {v}" for k, v in structured.items())
        parts.append(f"\n\nStructured fields:\n{rendered}")
    if pdf_url:
        # ADR-046 alternative E: the PDF body is deliberately not fetched, so
        # this URL is the only route an operator has to the real JD.
        parts.append(f"\n\nFull job description (PDF): {pdf_url}")
    return "".join(parts) or "(no description provided)"


def normalise_employment_type(value: str | None) -> str | None:
    """Map a free-text Taleo string onto this repo's ``EmploymentType``.

    Conservative by design: an unrecognised string returns ``None`` (the column
    is nullable) rather than a plausible guess. A wrong-but-plausible bucket is
    worse than an empty one — it survives review unnoticed and feeds ranking.

    **Order matters, which is why this is not a dict lookup.** "Temporary Full
    Time" matches both "full+time" and "temporary"; it is a fixed-term post, so
    ``contract`` is right and ``full_time`` would misrepresent it to anyone
    filtering on permanence. Same for "Co-op Full Time" → ``intern``.
    """
    if not value:
        return None
    v = value.lower()
    if "contract" in v or "temporary" in v:
        return "contract"
    if "intern" in v or "co-op" in v or "coop" in v:
        return "intern"
    if "part" in v and "time" in v:
        return "part_time"
    if "full" in v and "time" in v:
        return "full_time"
    return None
