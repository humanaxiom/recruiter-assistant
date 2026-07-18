"""FU-1 — the append-only audit sink behind the AUDITED candidate-reveal action.

Revealing a candidate's identity is the de-anonymization action, so every reveal
must leave an audit trail. :func:`record_reveal` is a PURE insert: it writes one
``reveal_audit`` row and returns its id. No decryption happens here (that is
``resume_service.get_one``'s job) — this module never touches PII.

The id is app-minted (like the résumé-upload path) so no ``RETURNING`` round trip
is needed. ``actor`` is best-effort today (the optional ``X-Actor-Name`` header);
real per-user identity arrives with RBAC (FU-4).
"""

from __future__ import annotations

from uuid import UUID, uuid4

from src.services import DbConn

_INSERT_SQL = """
INSERT INTO reveal_audit (id, resume_id, job_id, actor, context)
VALUES ($1, $2, $3, $4, $5)
"""


async def record_reveal(
    conn: DbConn,
    *,
    resume_id: UUID,
    actor: str | None,
    job_id: UUID | None = None,
    context: str | None = None,
) -> UUID:
    """Write one append-only ``reveal_audit`` row and return its (app-minted) id.

    A pure insert — no ``set_pii_key`` / ``pgp_sym_decrypt``, no read-back. Only
    ``resume_id`` is mandatory; ``job_id`` / ``actor`` / ``context`` are
    nullable.
    """
    audit_id = uuid4()
    await conn.execute(_INSERT_SQL, audit_id, resume_id, job_id, actor, context)
    return audit_id
