"""RED — the Taleo sync task (ADR-046), the only scheduled egress here.

All I/O mocked. What this pins is the task's *judgement*: when it refuses to
run, when it refuses to finish, and what it does with a partial failure.

**The disaster case has its own test, and it is a deviation from the hris
source.** There, a listing page that parses to zero rows logs a warning and
then falls through to the archive sweep — which archives every Taleo-sourced
job in the database. A 200 response carrying an error page, a template change
the parsers no longer recognise, or SFU moving the careers UI all produce
exactly that: zero rows, no exception.

You cannot distinguish "SFU took down every posting" from "our parser broke",
and only one of those readings is safe. So an empty listing ABORTS the run
before the sweep, and reports a failure rather than a quiet success.

**Nothing runs unless ``TALEO_ENABLED``** — pinned here as "no client is even
constructed", not merely "no rows change", because the point of the flag is
that a disabled deployment makes no outbound request at all.

``sync_taleo_jobs`` does not exist yet — RED half of the TDD cycle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _ctx(conn: MagicMock | None = None) -> dict[str, Any]:
    conn = conn or MagicMock(name="conn")
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=_acm())
    pool = MagicMock(name="pg_pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return {"pg_pool": pool, "arq": MagicMock(enqueue_job=AsyncMock())}


def _listing(rid: str = "7124") -> Any:
    from src.pipeline.sources.taleo import TaleoListingRow

    return TaleoListingRow(
        external_id=rid,
        title="Analyst",
        external_url=f"https://tre.tbe.taleo.net/req?rid={rid}",
        location="Burnaby",
        department="Neuroscience",
        employment_type="Full Time",
    )


def _requisition(rid: str = "7124") -> Any:
    from src.pipeline.sources.taleo import TaleoRequisition

    return TaleoRequisition(
        external_id=rid,
        title="Analyst",
        external_url=f"https://tre.tbe.taleo.net/req?rid={rid}",
        description_raw="A detailed job description of the role. " * 3,
        location="Burnaby",
        department="Neuroscience",
        employment_type="Full Time",
    )


# --------------------------------------------------------------- the flag


@pytest.mark.asyncio
async def test_a_disabled_deployment_makes_no_request_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``TALEO_ENABLED=false`` is the default, and the promise it makes is not
    "no rows change" — it is that a fresh checkout, a CI run and any airgapped
    deployment never egress. Asserting no CLIENT is constructed is what tests
    that promise; asserting no rows changed would pass even if the request
    were made and the parse failed."""
    from src.worker import taleo_sync_task

    built: list[Any] = []
    monkeypatch.setattr(
        taleo_sync_task,
        "TaleoClient",
        lambda *a, **k: built.append(1),  # noqa: ARG005
    )
    result = await taleo_sync_task.sync_taleo_jobs(_ctx())

    assert result["outcome"] == "skipped"
    assert built == [], "a disabled deployment constructed a Taleo client"


# ------------------------------------------------ the empty-listing abort


@pytest.mark.asyncio
async def test_an_empty_listing_aborts_before_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE disaster case, and the deviation from hris.

    Zero parsed rows is indistinguishable between "SFU took every posting
    down" and "the template changed and our parsers now match nothing" — a
    200 carrying an error page produces it with no exception. hris warns and
    sweeps anyway, which archives every Taleo job in the database.

    Only one reading is safe, so the run stops before the sweep.
    """
    from src.services import job_source_service
    from src.worker import taleo_sync_task

    _enable(monkeypatch, taleo_sync_task)
    _fake_client(monkeypatch, taleo_sync_task, listings=[])
    swept = MagicMock(side_effect=AssertionError("swept on an empty listing"))
    monkeypatch.setattr(job_source_service, "mark_missing_as_archived", swept)

    result = await taleo_sync_task.sync_taleo_jobs(_ctx())
    assert result["outcome"] == "failed"
    assert "empty" in result["reason"].lower()


@pytest.mark.asyncio
async def test_a_normal_run_does_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The abort above must not have disabled the sweep in general — a
    posting that genuinely disappears still has to be archived."""
    from src.worker import taleo_sync_task

    _enable(monkeypatch, taleo_sync_task)
    _fake_client(monkeypatch, taleo_sync_task, listings=[_listing()])
    swept = AsyncMock(return_value=[uuid4()])
    monkeypatch.setattr(
        taleo_sync_task.job_source_service, "mark_missing_as_archived", swept
    )
    monkeypatch.setattr(
        taleo_sync_task.job_source_service,
        "upsert_external_job",
        AsyncMock(return_value=_upsert_result(inserted=True)),
    )
    monkeypatch.setattr(
        taleo_sync_task.job_source_service, "write_sync_audit", AsyncMock()
    )

    result = await taleo_sync_task.sync_taleo_jobs(_ctx())
    assert result["outcome"] == "ok"
    swept.assert_awaited_once()


# ------------------------------------------------------- partial failures


@pytest.mark.asyncio
async def test_one_unfetchable_requisition_does_not_kill_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """40 postings, one 404. Aborting would mean a single bad row on SFU's
    side stops the whole feed indefinitely, and the sweep never runs."""
    from src.worker import taleo_sync_task

    _enable(monkeypatch, taleo_sync_task)

    async def _fetch_req(listing: Any) -> Any:
        if listing.external_id == "bad":
            raise RuntimeError("404")
        return _requisition(listing.external_id)

    _fake_client(
        monkeypatch,
        taleo_sync_task,
        listings=[_listing("bad"), _listing("7124")],
        fetch_requisition=_fetch_req,
    )
    upsert = AsyncMock(return_value=_upsert_result(inserted=True))
    monkeypatch.setattr(
        taleo_sync_task.job_source_service, "upsert_external_job", upsert
    )
    monkeypatch.setattr(
        taleo_sync_task.job_source_service,
        "mark_missing_as_archived",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        taleo_sync_task.job_source_service, "write_sync_audit", AsyncMock()
    )

    result = await taleo_sync_task.sync_taleo_jobs(_ctx())
    assert result["outcome"] == "ok"
    assert result["inserted"] == 1
    assert result["fetch_failures"] == 1, (
        "a skipped requisition must be COUNTED and reported — a run that "
        "silently drops postings looks identical to a clean one"
    )


# ------------------------------------------------------- the re-parse enqueue


@pytest.mark.asyncio
async def test_only_new_or_changed_jobs_are_re_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-parsing an unchanged JD burns an LLM call per job per day AND can
    move extracted requirements underneath a shortlist someone is reading
    (ROADMAP §5)."""
    from src.worker import taleo_sync_task

    _enable(monkeypatch, taleo_sync_task)
    _fake_client(
        monkeypatch,
        taleo_sync_task,
        listings=[_listing("1"), _listing("2")],
    )
    results = [
        _upsert_result(inserted=True),
        _upsert_result(inserted=False, changed=False),
    ]
    monkeypatch.setattr(
        taleo_sync_task.job_source_service,
        "upsert_external_job",
        AsyncMock(side_effect=results),
    )
    monkeypatch.setattr(
        taleo_sync_task.job_source_service,
        "mark_missing_as_archived",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        taleo_sync_task.job_source_service, "write_sync_audit", AsyncMock()
    )

    ctx = _ctx()
    await taleo_sync_task.sync_taleo_jobs(ctx)
    assert ctx["arq"].enqueue_job.await_count == 1


@pytest.mark.asyncio
async def test_the_run_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.worker import taleo_sync_task

    _enable(monkeypatch, taleo_sync_task)
    _fake_client(monkeypatch, taleo_sync_task, listings=[_listing()])
    audit = AsyncMock()
    monkeypatch.setattr(
        taleo_sync_task.job_source_service,
        "upsert_external_job",
        AsyncMock(return_value=_upsert_result(inserted=True)),
    )
    monkeypatch.setattr(
        taleo_sync_task.job_source_service,
        "mark_missing_as_archived",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(taleo_sync_task.job_source_service, "write_sync_audit", audit)

    await taleo_sync_task.sync_taleo_jobs(_ctx(), triggered_by_str="admin:asalah")
    audit.assert_awaited_once()
    assert audit.await_args.kwargs["triggered_by"] == "admin:asalah"


# ----------------------------------------------------------------- helpers


def _upsert_result(*, inserted: bool, changed: bool = True) -> Any:
    from src.services.job_source_service import UpsertResult

    return UpsertResult(
        job_id=uuid4(), was_inserted=inserted, description_changed=changed
    )


def _enable(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    from src.settings import Settings

    monkeypatch.setattr(
        module, "get_settings", lambda: Settings(taleo_enabled=True)  # noqa: ARG005
    )


def _fake_client(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    *,
    listings: list[Any],
    fetch_requisition: Any = None,
) -> None:
    async def _fetch_listings() -> list[Any]:
        return listings

    async def _default_fetch(listing: Any) -> Any:
        return _requisition(listing.external_id)

    client = MagicMock()
    client.fetch_listings = _fetch_listings
    client.fetch_requisition = fetch_requisition or _default_fetch
    monkeypatch.setattr(module, "TaleoClient", lambda *a, **k: client)  # noqa: ARG005
