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

from src.services import DbConn

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


__all__ = ["record_audit"]
