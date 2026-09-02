"""RED — the work-authorization write path and the shortlist band.

Slice C of the sponsor's 2026-09-02 set. The schema contracts are pinned in
``test_work_authorization_schema.py``; this file pins the two behaviours that
schema alone cannot express.

**1. Declaring it is an audited write, not a field edit.** A recruiter marking
a candidate "not eligible to work in Canada" is making an adverse decision on
a protected-adjacent ground. It follows the ``withdraw_resume`` precedent
exactly (ADR-026): writer roles only, session-role enforced, one ``audit_log``
row per APPLIED change, and idempotent — re-declaring the same state is a
quiet no-op rather than a second audit row, so the trail records *decisions*
rather than clicks.

**2. The band is a READ-TIME projection, never a persisted rank.** The
shortlist read joins ``resumes.work_authorization`` and sorts ineligible rows
last. Nothing is written to ``shortlist_entries``, and that is load-bearing: a
recruiter correcting a mis-set flag must see the list re-band on the next page
load, not after a multi-minute regenerate. It also means the declaration has
exactly one home, so no stale copy can disagree with it.

All I/O is mocked here, matching ``test_services_resume_withdraw.py``; the real
Postgres round trip (the idempotent UPDATE guard, the atomic audit write, and
the band's actual ordering against real rows) is covered by
``tests/integration/test_work_authorization_pg.py``.

``resume_service.set_work_authorization`` does not exist yet — RED half of the
TDD cycle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.errors import NotFoundError
from src.services import shortlist_service


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


# ------------------------------------------------------- the read-time band


def test_the_shortlist_read_projects_work_authorization() -> None:
    """Sponsor §O2 — the band cannot be applied to a column the read does not
    select. Both the plain and the blind read path need it, because blind
    review hides identity, not screening state: "Candidate C" must still be
    markable as ineligible or the band is unexplainable on a blind job."""
    for name, query in (
        ("_LIST_QUERY", shortlist_service._LIST_QUERY),
        ("_BLIND_LIST_QUERY", shortlist_service._BLIND_LIST_QUERY),
    ):
        assert (
            "work_authorization" in query
        ), f"{name} must project work_authorization — sponsor §O2"


def test_the_shortlist_read_orders_ineligible_candidates_last() -> None:
    """Sponsor answer 4: "Last but visible."

    Asserted against the SQL because the ordering IS the feature and it lives
    nowhere else. A band applied in Python after the fetch would be wrong the
    moment the list is capped by ``shortlist_top_percent`` — ineligible rows
    have to sink BEFORE the cap applies, not after it, or the cap silently
    evicts eligible candidates to make room for ineligible ones.
    """
    for name, query in (
        ("_LIST_QUERY", shortlist_service._LIST_QUERY),
        ("_BLIND_LIST_QUERY", shortlist_service._BLIND_LIST_QUERY),
    ):
        ordering = query[query.index("ORDER BY") :]
        assert "not_eligible" in ordering, (
            f"{name}: ineligible candidates must sort last IN THE QUERY, not "
            f"after the fetch. ORDER BY clause: {ordering!r}"
        )
        assert ordering.index("not_eligible") < ordering.index("rank"), (
            f"{name}: the eligibility band must be the PRIMARY sort key and "
            f"merit rank the secondary one. ORDER BY clause: {ordering!r}"
        )


def test_the_band_is_not_persisted_onto_the_shortlist_entry() -> None:
    """The declaration has exactly one home: ``resumes.work_authorization``.

    Fold it into the entry the way the composed sub-scores are folded and a
    recruiter's correction would not show until the job was regenerated —
    leaving two copies of a screening decision to disagree, with the stale one
    on screen. That is the ``score_breakdown`` staleness already recorded in
    ROADMAP §5, and it is not worth repeating on this field.
    """
    assert "work_authorization" not in shortlist_service._ENTRY_COLS


# ------------------------------------------------------------ the write path


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["eligible", "not_eligible", "unknown"])
async def test_declaring_work_authorization_audits_the_applied_change(
    state: str,
) -> None:
    """Every APPLIED declaration is audited, the correction back to
    ``unknown`` included — undoing an adverse decision is exactly as much a
    decision as making one, and a trail that records only the accusation is
    not a trail."""
    from src.services import audit_service, resume_service

    conn = _mock_conn()
    recorded: list[dict[str, Any]] = []

    async def _record(_conn: Any, **kw: Any) -> None:
        recorded.append(kw)

    original = audit_service.record_audit
    audit_service.record_audit = _record  # type: ignore[assignment]
    try:
        applied = await resume_service.set_work_authorization(
            conn,
            uuid4(),
            status=state,  # type: ignore[arg-type]
            note=None,
            actor_kind="user",
            actor_user_id=None,
            actor_service=None,
        )
    finally:
        audit_service.record_audit = original  # type: ignore[assignment]

    assert applied is True
    assert [r["action"] for r in recorded] == ["set_work_authorization"]
    assert recorded[0]["subject_type"] == "resume"
    assert recorded[0]["details"]["status"] == state


@pytest.mark.asyncio
async def test_redeclaring_the_same_state_is_a_quiet_no_op() -> None:
    """Idempotent, like ``withdraw_resume``. The guarded UPDATE matches zero
    rows, so no second audit row is written: the trail records decisions, not
    clicks. Without the guard, a recruiter reloading a form would manufacture
    audit rows for a decision nobody re-made."""
    from src.services import audit_service, resume_service

    conn = _mock_conn(execute_result="UPDATE 0")
    recorded: list[dict[str, Any]] = []

    async def _record(_conn: Any, **kw: Any) -> None:
        recorded.append(kw)

    original = audit_service.record_audit
    audit_service.record_audit = _record  # type: ignore[assignment]
    try:
        applied = await resume_service.set_work_authorization(
            conn,
            uuid4(),
            status="not_eligible",
            note=None,
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
        await resume_service.set_work_authorization(
            conn,
            uuid4(),
            status="eligible",
            note=None,
            actor_kind="user",
            actor_user_id=None,
            actor_service=None,
        )


@pytest.mark.asyncio
async def test_the_declaration_never_enqueues_a_reprojection() -> None:
    """Unlike a withdrawal, this changes NOTHING the graph knows about. It is
    a screening attribute, not a ranking input, and the band is applied at
    read time — so an outbox event here would trigger a pointless
    re-projection of every affected résumé and, worse, imply to a future
    reader that eligibility feeds the graph. It must not."""
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
        await resume_service.set_work_authorization(
            conn,
            uuid4(),
            status="not_eligible",
            note=None,
            actor_kind="user",
            actor_user_id=None,
            actor_service=None,
        )
    finally:
        outbox_service.enqueue_outbox = orig_out  # type: ignore[assignment]
        audit_service.record_audit = orig_audit  # type: ignore[assignment]

    assert events == []
