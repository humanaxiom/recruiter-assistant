"""Shortlist generate/list/get + export routes (Phase 6).

The review-workflow decision/stage routes (``/shortlist/{id}/decision`` /
``/shortlist/{id}/stage``) from hris are CUT and must never exist — there is
deliberately no route defined for them here, so hitting them is FastAPI's own
unmatched-route 404 (proving the route table itself has no entry), never a
401/403.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Query, Response, status

from src.api.deps import (
    Role,
    get_arq,
    log_auditor_read,
    require_role,
    require_session_role,
    resolve_user,
    scoped_user_id_or_403,
)
from src.models.pool import Db
from src.schemas.auth import User
from src.schemas.matching import ShortlistEntry, ShortlistStatusResponse
from src.services import shortlist_service
from src.services.pii import set_pii_key

router = APIRouter()

# FU-4 (RBAC), per route: generating a shortlist enqueues real ranking work, so
# it is a write; listing/reading/exporting are blind reads open to all four
# roles (the export can no longer un-blind at all — see below).
_SHORTLIST_WRITERS: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER)
_SHORTLIST_READERS: tuple[Role, ...] = tuple(Role)

ExportFormat = Literal["csv", "evidence-csv", "json"]


@router.post(
    "/jobs/{job_id}/shortlist",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[
        Depends(require_role(*_SHORTLIST_WRITERS)),
        Depends(require_session_role(*_SHORTLIST_WRITERS)),
    ],
)
async def generate_shortlist(
    job_id: UUID,
    db: Db,
    arq: Annotated[ArqRedis, Depends(get_arq)],
) -> dict[str, str]:
    """``fix/regenerate-shortlist-no-feedback`` — ``shortlist_state`` is set to
    ``'ranking'`` BEFORE the job is handed to the queue, so the state is
    already true the instant this returns and the frontend's very first poll
    sees it, rather than racing the worker to set it. The worker
    clears/overwrites it on every terminal path (see ``matching_tasks.py``).

    ``fix/zero-requirements-rank-guard`` — the zero-requirements refusal runs
    FIRST, before any ranking-state write, so a refused job never flips to
    ``'ranking'`` and never enqueues.

    ITEM 1 (A DROPPED REGENERATE IS REMEMBERED) — AFTER the requirements
    guard, ATOMICALLY check-and-set via
    ``shortlist_service.request_shortlist_rerun_if_ranking``: ONE ``UPDATE
    ... WHERE shortlist_state = 'ranking' RETURNING true`` statement, not a
    separate read (``get_shortlist_state``) followed by a conditional write —
    that older shape was a TOCTOU race (a concurrent worker could flip the
    state between the read and the write). ``True`` means a run was
    genuinely in flight at the moment of the write: a second worker run
    would just duplicate work already happening, so this does NOT enqueue —
    it answers ``queued_after_current`` so the caller knows the request was
    heard rather than silently ignored. ``False`` covers every other case,
    including a nonexistent job (the ``UPDATE`` simply matches zero rows,
    diagnosing a missing job is the worker's job, not this guard's, matching
    the pre-existing "a nonexistent job still 202s" contract) and
    ``'awaiting_llm'``/no state at all (no run genuinely in flight) — all of
    which enqueue exactly as before."""
    await shortlist_service.assert_job_has_requirements(db, job_id)
    if await shortlist_service.request_shortlist_rerun_if_ranking(db, job_id):
        return {"job_id": str(job_id), "status": "queued_after_current"}
    await shortlist_service.set_shortlist_ranking(db, job_id)
    await arq.enqueue_job("shortlist_job", str(job_id))
    return {"job_id": str(job_id), "status": "enqueued"}


# Declared BEFORE /jobs/{job_id}/shortlist's own path shape is unambiguous
# (export is a THIRD path segment) so no ordering hazard exists, but kept
# adjacent for readability.
@router.get("/jobs/{job_id}/shortlist/export")
async def export_shortlist(
    job_id: UUID,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_SHORTLIST_READERS))],
    user: Annotated[User | None, Depends(resolve_user)],
    format: Annotated[ExportFormat, Query()] = "csv",
) -> Response:
    """``pii_service.set_pii_key`` runs inside an open ``db.transaction()``
    BEFORE ``shortlist_service.export_rows`` — its own raw ``pgp_sym_decrypt``
    fails loud without it.

    FU-4/D3: exports are BLIND-ONLY. The former ``reveal`` query parameter was
    an UNAUDITED BULK de-anonymization across a whole shortlist (never recorded
    in ADR-016), so it is gone — ``reveal=False`` is hardcoded below and the
    ``-anon`` filename suffix is now unconditional. If a bulk reveal export
    ever becomes a real need it gets its own audited POST route.

    FU-6 slice 7 (ADR-020 §3/§4) — row-scoped for a hiring_manager SESSION
    (not the key role — see ``scoped_user_id_or_403``'s docstring for why),
    unscoped (``user_id=None``) for admin/recruiter/auditor. An unassigned
    or nonexistent job both surface as a 200 + empty export (never 404) —
    this is a by-``job_id`` LIST subresource, matching ``list_shortlist``'s
    own ADR-020 §5 behaviour.

    FU-6 slice 8 (ADR-020 §6) — a deliberate BULK read, logged for a real
    auditor session exactly like the single-subject reads (``log_auditor_read``),
    AFTER the service call resolves and before the response returns. This
    route never 404s (see above), so there is no 404-writes-nothing case to
    special-case here.
    """
    user_id = await scoped_user_id_or_403(user, role)
    async with db.transaction():
        await set_pii_key(db)
        rows = await shortlist_service.export_rows(
            db, job_id=job_id, reveal=False, user_id=user_id
        )
    await log_auditor_read(
        db,
        user,
        action="read_shortlist_export",
        subject_type="job",
        subject_id=job_id,
        job_id=job_id,
    )

    if format == "csv":
        content = shortlist_service.shortlist_csv(rows)
        filename = f"shortlist-{job_id}-anon.csv"
        media_type = "text/csv"
    elif format == "evidence-csv":
        content = shortlist_service.shortlist_evidence_csv(rows)
        filename = f"shortlist-{job_id}-evidence-anon.csv"
        media_type = "text/csv"
    else:
        content = shortlist_service.shortlist_json(rows)
        filename = f"shortlist-{job_id}-anon.json"
        media_type = "application/json"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/jobs/{job_id}/shortlist")
async def list_shortlist(
    job_id: UUID,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_SHORTLIST_READERS))],
    user: Annotated[User | None, Depends(resolve_user)],
) -> list[ShortlistEntry]:
    """FU-6 slice 7 (ADR-020 §3/§4) — row-scoped for a hiring_manager SESSION
    (not the key role — see ``scoped_user_id_or_403``'s docstring for why),
    unscoped (``user_id=None``) for admin/recruiter/auditor. An unassigned
    or nonexistent job both surface as 200 + an empty list (never 404) —
    this is a LIST subresource, matching its pre-existing behaviour for a
    genuinely nonexistent ``job_id``."""
    user_id = await scoped_user_id_or_403(user, role)
    return await shortlist_service.list_for_job(db, job_id=job_id, user_id=user_id)


@router.get("/jobs/{job_id}/shortlist/status")
async def shortlist_status(
    job_id: UUID,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_SHORTLIST_READERS))],
    user: Annotated[User | None, Depends(resolve_user)],
) -> ShortlistStatusResponse:
    """FU-7 §2 (ADR-021 §2 / ADR-029) — the fail-closed ranking state the
    frontend poll consults to tell "awaiting AI" apart from "still generating".

    FU-6 slice 7 (ADR-020 §3/§5) — row-scoped exactly like ``list_shortlist``:
    the resolved ``user_id`` is forwarded into ``get_shortlist_state``. For a
    hiring_manager SESSION (not the key role — see ``scoped_user_id_or_403``)
    the read is scoped to their assigned jobs; a job they are not assigned to,
    and a genuinely nonexistent job, BOTH surface as 404 (never 403), so the
    404-vs-200 split cannot be used as a job-existence oracle and one manager
    cannot read another job's ranking state. admin/recruiter/auditor read
    unscoped (``user_id=None``). A hiring_manager KEY with no verifiable
    session 403s before the service is ever reached (``scoped_user_id_or_403``).
    An existing, assigned job with no ``awaiting_llm`` state returns
    ``state=null``."""
    user_id = await scoped_user_id_or_403(user, role)
    state = await shortlist_service.get_shortlist_state(db, job_id, user_id=user_id)
    if state is None:
        return ShortlistStatusResponse(job_id=job_id)
    return ShortlistStatusResponse(
        job_id=job_id,
        state=state.state,
        reason=state.reason,
        at=state.at,
        # ITEM 1: ``getattr`` with a default, not a bare attribute read — a
        # handful of pre-existing tests in this file mock ``get_shortlist_state``
        # with a stand-in ``_State`` object that predates this field and
        # carries no ``rerun_requested`` attribute at all; a bare read would
        # 500 those. ``ShortlistStateOut`` itself always carries the field
        # (with its own ``False`` default), so real callers are unaffected.
        rerun_requested=getattr(state, "rerun_requested", False),
    )


@router.get("/shortlist/{entry_id}")
async def get_shortlist_entry(
    entry_id: UUID,
    db: Db,
    role: Annotated[Role, Depends(require_role(*_SHORTLIST_READERS))],
    user: Annotated[User | None, Depends(resolve_user)],
) -> ShortlistEntry:
    """FU-6 slice 7 (ADR-020 §3/§5) — row-scoped for a hiring_manager
    SESSION; an unassigned or nonexistent entry both surface as 404 (never
    403) via ``shortlist_service.get_one``'s ``NotFoundError``.

    FU-6 slice 8 (ADR-020 §6) — a real auditor session's successful read is
    itself logged (``log_auditor_read``), AFTER the service call resolves
    (so a 404 writes no row) and before the response returns."""
    user_id = await scoped_user_id_or_403(user, role)
    entry = await shortlist_service.get_one(db, entry_id, user_id=user_id)
    await log_auditor_read(
        db,
        user,
        action="read_shortlist_entry",
        subject_type="shortlist_entry",
        subject_id=entry_id,
    )
    return entry


__all__ = ["router"]
