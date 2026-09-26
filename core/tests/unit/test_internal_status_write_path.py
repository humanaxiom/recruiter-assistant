"""RED — ``resume_service.set_internal_status`` (Sponsor Requirements PR2
slice 2).

Mirrors ``resume_service.set_work_authorization``
(``core/src/services/resume_service.py:1028``) exactly, and this file mirrors
``test_work_authorization_banding.py``'s own write-path section exactly for
the same reason: it is the SAME kind of act — an audited, reversible, human
declaration about a real person — extended to two booleans (SFU internal
APSA/CUPE employee status) instead of one three-state field.

Signature under test::

    async def set_internal_status(
        conn, resume_id, *, internal_apsa: bool, internal_cupe: bool,
        actor_kind, actor_user_id, actor_service,
    ) -> bool: ...

**Pinned here:**

* Idempotent — re-declaring the same ``(internal_apsa, internal_cupe)`` pair
  is a quiet no-op: no second audit row, no error.
* Audited on every APPLIED change, including a change that reverts BOTH
  flags back to ``False`` — undoing a previously-declared internal-employee
  status is exactly as much a decision as making one.
* ``NotFoundError`` for a résumé id that does not exist at all.
* **No outbox event — and the reason is not the obvious one.** It is NOT that
  the flag is inert with respect to ranking — it isn't: shortlist ordering
  DOES read ``internal_apsa``/``internal_cupe``. The reason is that what it
  feeds is a plain Postgres column read at rank/list time, exactly like
  ``work_authorization``'s band — never Neo4j's projected skill graph. There
  is therefore nothing for an outbox event to trigger re-projection OF: no
  graph node, no embedding, no derived skill edge depends on this column.
  Enqueueing here would trigger pointless worker fan-out and would wrongly
  imply to a future reader that this column feeds the graph.

All I/O is mocked here (matching ``test_work_authorization_banding.py``); the
real Postgres round trip (idempotent UPDATE guard, atomic audit write, and the
real DEFAULT/backfill) is covered by ``tests/integration/test_internal_status_pg.py``
and ``tests/unit/test_ddl.py``/``tests/integration/test_schema.py``.

``resume_service.set_internal_status`` does not exist yet — RED half of the
TDD cycle. Every test below either raises ``AttributeError`` at the
``monkeypatch``/call site or fails an assertion once a naive/incomplete
implementation lands.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.errors import NotFoundError


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(*, exists: bool = True, execute_result: str = "UPDATE 1") -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value=execute_result)
    conn.transaction = MagicMock(return_value=_acm())
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=uuid4() if exists else None)
    return conn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("internal_apsa", "internal_cupe"),
    [(True, False), (False, True), (True, True)],
)
async def test_declaring_internal_status_audits_the_applied_change(
    internal_apsa: bool, internal_cupe: bool
) -> None:
    from src.services import audit_service, resume_service

    conn = _mock_conn()
    recorded: list[dict[str, Any]] = []

    async def _record(_conn: Any, **kw: Any) -> None:
        recorded.append(kw)

    original = audit_service.record_audit
    audit_service.record_audit = _record  # type: ignore[assignment]
    try:
        applied = await resume_service.set_internal_status(
            conn,
            uuid4(),
            internal_apsa=internal_apsa,
            internal_cupe=internal_cupe,
            actor_kind="user",
            actor_user_id=None,
            actor_service=None,
        )
    finally:
        audit_service.record_audit = original  # type: ignore[assignment]

    assert applied is True
    assert [r["action"] for r in recorded] == ["set_internal_status"]
    assert recorded[0]["subject_type"] == "resume"
    assert recorded[0]["details"]["internal_apsa"] == internal_apsa
    assert recorded[0]["details"]["internal_cupe"] == internal_cupe


@pytest.mark.asyncio
async def test_reverting_both_flags_to_false_is_still_audited() -> None:
    """Undoing a previously-declared internal-employee status is exactly as
    much a decision as making one — the trail must record the correction,
    not just the original accusation-shaped declaration."""
    from src.services import audit_service, resume_service

    conn = _mock_conn(execute_result="UPDATE 1")
    recorded: list[dict[str, Any]] = []

    async def _record(_conn: Any, **kw: Any) -> None:
        recorded.append(kw)

    original = audit_service.record_audit
    audit_service.record_audit = _record  # type: ignore[assignment]
    try:
        applied = await resume_service.set_internal_status(
            conn,
            uuid4(),
            internal_apsa=False,
            internal_cupe=False,
            actor_kind="user",
            actor_user_id=None,
            actor_service=None,
        )
    finally:
        audit_service.record_audit = original  # type: ignore[assignment]

    assert applied is True
    assert len(recorded) == 1
    assert recorded[0]["details"]["internal_apsa"] is False
    assert recorded[0]["details"]["internal_cupe"] is False


@pytest.mark.asyncio
async def test_redeclaring_the_same_pair_is_a_quiet_no_op() -> None:
    """Idempotent, like ``set_work_authorization``. The guarded UPDATE matches
    zero rows, so NO second audit row is written — the trail records
    decisions, not a recruiter reloading a form."""
    from src.services import audit_service, resume_service

    conn = _mock_conn(execute_result="UPDATE 0")
    recorded: list[dict[str, Any]] = []

    async def _record(_conn: Any, **kw: Any) -> None:
        recorded.append(kw)

    original = audit_service.record_audit
    audit_service.record_audit = _record  # type: ignore[assignment]
    try:
        applied = await resume_service.set_internal_status(
            conn,
            uuid4(),
            internal_apsa=True,
            internal_cupe=False,
            actor_kind="user",
            actor_user_id=None,
            actor_service=None,
        )
    finally:
        audit_service.record_audit = original  # type: ignore[assignment]

    assert applied is False
    assert recorded == []


@pytest.mark.asyncio
async def test_declaring_on_a_missing_resume_raises_not_found() -> None:
    from src.services import resume_service

    conn = _mock_conn(exists=False)
    with pytest.raises(NotFoundError):
        await resume_service.set_internal_status(
            conn,
            uuid4(),
            internal_apsa=True,
            internal_cupe=False,
            actor_kind="user",
            actor_user_id=None,
            actor_service=None,
        )


@pytest.mark.asyncio
async def test_the_declaration_never_enqueues_a_reprojection() -> None:
    """See the module docstring for WHY: this column IS read at rank time,
    but only as a plain Postgres column — never via Neo4j's projected skill
    graph — so there is nothing for an outbox event to trigger re-projection
    of. An event here would be pure worker fan-out with no consumer that
    needs it, and would misleadingly suggest this field feeds the graph."""
    from src.services import audit_service, outbox_service, resume_service

    conn = _mock_conn()
    events: list[dict[str, Any]] = []

    async def _enqueue(_conn: Any, **kw: Any) -> None:
        events.append(kw)

    async def _record(_conn: Any, **kw: Any) -> None:
        return None

    orig_out, orig_audit = outbox_service.enqueue_outbox, audit_service.record_audit
    outbox_service.enqueue_outbox = _enqueue  # type: ignore[assignment]
    audit_service.record_audit = _record  # type: ignore[assignment]
    try:
        await resume_service.set_internal_status(
            conn,
            uuid4(),
            internal_apsa=True,
            internal_cupe=True,
            actor_kind="user",
            actor_user_id=None,
            actor_service=None,
        )
    finally:
        outbox_service.enqueue_outbox = orig_out  # type: ignore[assignment]
        audit_service.record_audit = orig_audit  # type: ignore[assignment]

    assert events == []


@pytest.mark.asyncio
async def test_declaring_mixed_flags_persists_both_independently() -> None:
    """The two flags are independent booleans, not a single combined state —
    a call that sets APSA True/CUPE False must not collapse to "any internal"
    or otherwise lose which bargaining unit applies."""
    from src.services import audit_service, resume_service

    conn = _mock_conn()
    recorded: list[dict[str, Any]] = []

    async def _record(_conn: Any, **kw: Any) -> None:
        recorded.append(kw)

    original = audit_service.record_audit
    audit_service.record_audit = _record  # type: ignore[assignment]
    try:
        applied = await resume_service.set_internal_status(
            conn,
            uuid4(),
            internal_apsa=True,
            internal_cupe=False,
            actor_kind="service",
            actor_user_id=None,
            actor_service="candidate-roster-import",
        )
    finally:
        audit_service.record_audit = original  # type: ignore[assignment]

    assert applied is True
    assert recorded[0]["details"] == {"internal_apsa": True, "internal_cupe": False}
