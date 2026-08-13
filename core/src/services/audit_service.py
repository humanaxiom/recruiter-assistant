"""FU-5 slice 8 (ADR-019 §6/§7) — the generalized, append-only ``audit_log``
writer that replaces ``reveal_service.record_reveal`` on the reveal path.

:func:`record_audit` is a PURE insert, mirroring ``reveal_service
.record_reveal``'s own discipline: one INSERT into ``audit_log``, no
read-back, no decryption, no PII ever touched here. Unlike ``record_reveal``
(which mints and returns a ``UUID`` because the old reveal route needed it
back), ``record_audit`` has no caller that needs the minted id — the row's
own ``id`` is DB-generated (``DEFAULT gen_random_uuid()``) and this function
returns ``None``.

**No defensive validation of the actor identity here.** The
``audit_log_actor_identity`` CHECK constraint (``src/models/ddl.py``) is the
single source of truth for "exactly one of ``actor_user_id`` /
``actor_service`` is set" — this function is a passthrough, same as
``record_reveal``. Callers (``src.api.routes.resumes``'s reveal handler
above all) are responsible for supplying a legal combination; a caller that
does not gets a real Postgres constraint violation, not a Python-side
exception masking it.

The old ``reveal_audit`` sink (``src.services.reveal_service``) is KEPT,
read-only, per ADR-019 §6's migration posture — this module does not replace
or delete it.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from src.schemas.audit import AuditLogItem
from src.services import DbConn

#: Rendered in place of a value the auditor viewer will not disclose. A marker,
#: never a removal: an auditor must be able to tell "no value was recorded" from
#: "a value was recorded and you are not being shown it" — materially different
#: facts in a compliance review, and a view that silently omits fields is worse
#: than useless because it looks complete.
WITHHELD = "<withheld>"

#: ALLOWLIST of ``details`` keys safe to disclose, keyed by the ``action`` that
#: writes them. Scoped by action, not by bare key name: ``old_role`` is safe
#: *because of what ``role_changed`` puts there*, and an allowlist keyed only by
#: name would let a future writer smuggle content through a familiar-looking
#: key.
#:
#: **Fail-closed.** Anything absent is WITHHELD, so a new ``record_audit``
#: caller inventing a new details key gets withholding by default until someone
#: classifies it. A blocklist would protect against today's two writers and
#: silently leak the third one added next year — the ROADMAP A7 shape.
#:
#: Deliberately NOT listed: ``withdraw_resume``'s ``reason``. It is operator-
#: typed prose about a specific, named candidate, and this viewer is exactly the
#: surface that would render it. Whether an auditor should be able to read it at
#: all is a product/privacy question recorded in ADR-036, not answered here.
_DISCLOSABLE_DETAIL_KEYS: dict[str, frozenset[str]] = {
    "role_changed": frozenset({"old_role", "new_role"}),
}


def redact_audit_details(action: str, details: Any) -> Any:
    """Apply the disclosure allowlist to one row's ``details``.

    Per-KEY, not per-row: withholding one value must not blind the auditor to
    the classified ones beside it. An empty or null value is passed through
    as-is — there is nothing to protect, and marking it withheld would assert
    that a value exists when none does.

    Never raises. ``details`` is ``jsonb``, so a legacy or hand-written row may
    hold a scalar or a list rather than an object; anything that is not a dict
    is withheld wholesale rather than inspected. An audit read must degrade,
    never 500.
    """
    if details is None:
        return None
    if not isinstance(details, dict):
        return WITHHELD
    allowed = _DISCLOSABLE_DETAIL_KEYS.get(action, frozenset())
    return {
        key: value if (key in allowed or not value) else WITHHELD
        for key, value in details.items()
    }


_INSERT_SQL = """
INSERT INTO audit_log (
    actor_kind, actor_user_id, actor_service, action, subject_type,
    subject_id, job_id, context, details
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
"""


async def record_audit(
    conn: DbConn,
    *,
    actor_kind: str,
    actor_user_id: UUID | None,
    actor_service: str | None,
    action: str,
    subject_type: str,
    subject_id: UUID,
    job_id: UUID | None = None,
    context: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Write one append-only ``audit_log`` row.

    Deliberately NOT wrapped in ``async with conn.transaction():`` — a bare
    ``conn.execute`` outside an explicit transaction block is a single
    autocommitted statement under asyncpg, so the row is durably committed
    the instant this call returns. This is load-bearing for ADR-016/ADR-019
    §7's ordering guarantee: callers that write the audit row BEFORE
    attempting a decrypt (see ``src.api.routes.resumes.reveal_resume``) get a
    row that survives a crash in the decrypt step, even though both steps
    share the same pooled connection.
    """
    await conn.execute(
        _INSERT_SQL,
        actor_kind,
        actor_user_id,
        actor_service,
        action,
        subject_type,
        subject_id,
        job_id,
        context,
        json.dumps(details) if details is not None else None,
    )


_LIST_SQL = """
SELECT a.id,
       a.actor_kind,
       a.actor_user_id,
       u.cas_username AS actor_username,
       a.actor_service,
       a.action,
       a.subject_type,
       a.subject_id,
       a.job_id,
       a.context,
       a.details,
       a.occurred_at
FROM audit_log a
LEFT JOIN users u ON u.id = a.actor_user_id
WHERE ($1::text IS NULL OR a.action = $1)
  AND ($2::text IS NULL OR a.subject_type = $2)
  AND ($3::uuid IS NULL OR a.job_id = $3)
ORDER BY a.occurred_at DESC, a.id DESC
LIMIT $4 OFFSET $5
"""


async def list_audit_log(
    conn: Any,
    *,
    limit: int = 100,
    offset: int = 0,
    action: str | None = None,
    subject_type: str | None = None,
    job_id: UUID | None = None,
) -> list[AuditLogItem]:
    """Read the LIVE ``audit_log``, newest first (Phase 1.4 / ADR-036).

    **The LEFT JOIN is load-bearing.** ``actor_kind='service'`` rows carry a
    NULL ``actor_user_id`` by CHECK constraint, so an INNER JOIN would silently
    hide every one of them — and those are precisely the events an auditor most
    needs, since an unattributable ``actor_service='api'`` write is the
    signature of the ADR-034 exploit. A viewer that quietly dropped them would
    be worse than no viewer, because it would look complete.

    **``a.id`` is a tiebreak, not decoration.** Rows written inside one
    statement share ``occurred_at`` to the microsecond, so ordering by the
    timestamp alone is not a total order: without the tiebreak the same row can
    appear on two pages while another appears on none.

    **Never joins ``resumes`` or ``jobs``**, mirroring
    ``/audit/reveals-legacy``'s own discipline — nothing decrypts, so no
    candidate PII can reach this path even by accident. ``details`` is passed
    through :func:`redact_audit_details` before it leaves this function, so the
    boundary holds for every caller rather than depending on each one
    remembering to apply it.

    Filters are NULL-guarded in a fixed-shape query rather than concatenated,
    so parameter positions stay stable and there is no dynamic SQL to review.
    """
    rows = await conn.fetch(_LIST_SQL, action, subject_type, job_id, limit, offset)
    items: list[AuditLogItem] = []
    for row in rows:
        raw = row["details"]
        # asyncpg hands back `jsonb` as `str`; the isinstance guard mirrors
        # `resume_service`'s established idiom for the same column type.
        decoded = json.loads(raw) if isinstance(raw, str) else raw
        items.append(
            AuditLogItem(
                id=row["id"],
                actor_kind=row["actor_kind"],
                actor_user_id=row["actor_user_id"],
                actor_username=row["actor_username"],
                actor_service=row["actor_service"],
                action=row["action"],
                subject_type=row["subject_type"],
                subject_id=row["subject_id"],
                job_id=row["job_id"],
                context=row["context"],
                details=redact_audit_details(row["action"], decoded),
                occurred_at=row["occurred_at"],
            )
        )
    return items


__all__ = [
    "record_audit",
    "list_audit_log",
    "redact_audit_details",
    "WITHHELD",
]
