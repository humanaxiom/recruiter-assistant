"""Unit tests for ``src.worker.matching_tasks.shortlist_job`` — all I/O
mocked (the 4c orchestrator is patched out entirely; this file pins CONTROL
FLOW, not scoring).

``src.worker.matching_tasks`` does not exist yet — RED half of the TDD cycle;
this whole file is expected to fail at collection (``ModuleNotFoundError``).

Pinned control flow, mirroring the precedent in ``test_worker_parse_job.py``
(missing row / not-parsed / happy path / race-adjacent edge cases):

* missing job row -> ``"missing"``, ``generate_shortlist`` NEVER called.
* row exists but ``description_parsed IS NULL`` (job not yet parsed) ->
  ``"not_parsed"``, ``generate_shortlist`` NEVER called.
* happy path -> ``persist_shortlist`` called EXACTLY ONCE with the
  orchestrator's ``ShortlistResult``, returns ``"persisted"``.
* a ZERO-candidate result (``entries == []``) still calls ``persist_shortlist``
  (a rerun that now yields nothing must still CLEAR a stale prior shortlist —
  the DELETE-first persistence contract lives in
  ``test_services_shortlist_persist.py``) and returns the DISTINCT status
  ``"empty"`` (not ``"persisted"``) so a caller/log line can tell "ranked, zero
  results" apart from "ranked, wrote N rows".

ADR-010 §1 residual / concurrency dedup (this slice):

* the advisory lock (``src.worker.job_lock.try_job_lock`` /
  ``release_job_lock``, imported into this module) is acquired FIRST, right
  after ``pool.acquire()`` — BEFORE the job row is even fetched. When it is
  already held (by a concurrent duplicate run on another connection),
  ``shortlist_job`` returns the new status ``"already_running"`` and touches
  NOTHING else: no row fetch, no orchestrator call, no persist, and it must
  NOT attempt to release a lock it never held.
* on every path that DID acquire the lock, it is released in a ``finally`` —
  proven both on the ordinary success path and when the orchestrator raises,
  so a later legitimate re-run is never left blocked by a crashed prior run.
  The real, only-provable-against-a-real-Postgres session-lock semantics
  (does a second connection actually get blocked; does release actually free
  it) live in ``tests/integration/test_job_lock_pg.py`` — mandatory, not
  optional, for this change.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.pipeline.matching.orchestrator import ShortlistResult, ShortlistResultEntry
from src.schemas.matching import DEFAULT_WEIGHTS, ScoreBreakdown
from src.settings import Settings


class _Row(dict[str, Any]):
    """A dict-like fake asyncpg Record: an absent key returns ``None``
    instead of raising ``KeyError``, so these tests don't have to know every
    column the real implementation selects."""

    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _flat_call_args(mock_call: Any) -> list[Any]:
    return list(mock_call.args) + list(mock_call.kwargs.values())


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_conn(fetchrow_result: Any) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    return conn


def _make_ctx(conn: MagicMock) -> dict[str, Any]:
    pool = MagicMock(name="pg_pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return {
        "pg_pool": pool,
        "neo4j": MagicMock(name="neo4j"),
        "llm": MagicMock(name="llm"),
        "embedder": MagicMock(name="embedder"),
    }


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.7,
        education=0.6,
        seniority=0.5,
        vector=0.4,
        structured=0.65,
    )


def _lock_patches(*, held: bool = True) -> tuple[Any, Any]:
    """Patch ``try_job_lock``/``release_job_lock`` as imported into
    ``matching_tasks`` — the lock is acquired by default (``held=True``) so
    existing control-flow tests written before this slice keep passing
    unchanged."""
    return (
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=held,
        ),
        patch("src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock),
    )


# ── missing / not-parsed ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_job_row_returns_missing_and_orchestrator_not_called() -> None:
    from src.worker.matching_tasks import shortlist_job

    conn = _make_conn(None)
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.generate_shortlist", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock
        ) as persist,
    ):
        result = await shortlist_job(ctx, str(uuid4()))

    assert result == "missing"
    generate.assert_not_called()
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_job_not_yet_parsed_returns_not_parsed_and_orchestrator_not_called() -> (
    None
):
    from src.worker.matching_tasks import shortlist_job

    conn = _make_conn(_Row({"description_parsed": None}))
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.generate_shortlist", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock
        ) as persist,
    ):
        result = await shortlist_job(ctx, str(uuid4()))

    assert result == "not_parsed"
    generate.assert_not_called()
    persist.assert_not_called()


# ── happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_persists_the_orchestrator_result_exactly_once() -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    conn = _make_conn(
        _Row({"description_parsed": {"required_skills": [{"name": "Python"}]}})
    )
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    fake_result = ShortlistResult(
        job_id=job_id,
        entries=[
            ShortlistResultEntry(
                resume_id=uuid4(),
                rank=1,
                score_final=0.9,
                score_structured=0.8,
                score_evidence=0.7,
                breakdown=_breakdown(),
                evidence=None,
            )
        ],
    )

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as generate,
        patch(
            "src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock
        ) as persist,
    ):
        result = await shortlist_job(ctx, str(job_id))

    assert result == "persisted"
    generate.assert_awaited_once()
    assert generate.await_args.args[0] == job_id or job_id in _flat_call_args(
        generate.await_args
    )
    persist.assert_awaited_once()
    assert any(
        arg is fake_result for arg in _flat_call_args(persist.await_args)
    ), "persist_shortlist must be called with the orchestrator's own result object"


@pytest.mark.asyncio
async def test_happy_path_passes_default_weights_when_settings_are_default() -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    conn = _make_conn(_Row({"description_parsed": {"required_skills": []}}))
    ctx = _make_ctx(conn)
    fake_result = ShortlistResult(job_id=job_id, entries=[])
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=fake_result,
        ) as generate,
        patch("src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock),
    ):
        await shortlist_job(ctx, str(job_id))

    assert generate.await_args.kwargs["weights"] == DEFAULT_WEIGHTS


# ── zero-candidate result: still persisted (clears stale prior run), but ───
#    a DISTINCT status from a non-empty write ───────────────────────────────


@pytest.mark.asyncio
async def test_zero_candidate_result_still_calls_persist_and_returns_empty() -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    conn = _make_conn(_Row({"description_parsed": {"required_skills": []}}))
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    empty_result = ShortlistResult(job_id=job_id, entries=[])

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=empty_result,
        ),
        patch(
            "src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock
        ) as persist,
    ):
        result = await shortlist_job(ctx, str(job_id))

    assert result == "empty"
    assert result != "persisted"
    persist.assert_awaited_once()
    assert any(
        arg is empty_result for arg in _flat_call_args(persist.await_args)
    ), "a zero-candidate rerun must still persist (clearing any stale prior shortlist)"


# ── advisory-lock dedup (ADR-010 §1 residual) ───────────────────────────────


@pytest.mark.asyncio
async def test_lock_already_held_returns_already_running_before_any_row_fetch() -> None:
    """When the advisory lock is already held by a concurrent duplicate run,
    ``shortlist_job`` must short-circuit BEFORE fetching the job row —
    proving the lock check runs first, not merely "somewhere before persist"."""
    from src.worker.matching_tasks import shortlist_job

    conn = _make_conn(_Row({"description_parsed": {"required_skills": []}}))
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock
        ) as release_lock,
        patch(
            "src.worker.matching_tasks.generate_shortlist", new_callable=AsyncMock
        ) as generate,
        patch(
            "src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock
        ) as persist,
    ):
        result = await shortlist_job(ctx, str(uuid4()))

    assert result == "already_running"
    conn.fetchrow.assert_not_called()
    generate.assert_not_called()
    persist.assert_not_called()
    release_lock.assert_not_called()


@pytest.mark.asyncio
async def test_lock_is_acquired_with_shortlist_kind_and_the_job_id() -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    conn = _make_conn(_Row({"description_parsed": {"required_skills": []}}))
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=True,
        ) as try_lock,
        patch("src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock),
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=ShortlistResult(job_id=job_id, entries=[]),
        ),
        patch("src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock),
    ):
        await shortlist_job(ctx, str(job_id))

    try_lock.assert_awaited_once_with(conn, "shortlist", job_id)


@pytest.mark.asyncio
async def test_normal_run_releases_the_lock_it_acquired() -> None:
    """A successful run must release the lock in a ``finally`` so a later
    legitimate re-run is not left blocked."""
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    conn = _make_conn(_Row({"description_parsed": {"required_skills": []}}))
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock
        ) as release_lock,
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=ShortlistResult(job_id=job_id, entries=[]),
        ),
        patch("src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock),
    ):
        result = await shortlist_job(ctx, str(job_id))

    assert result == "empty"
    release_lock.assert_awaited_once_with(conn, "shortlist", job_id)


@pytest.mark.asyncio
async def test_lock_is_released_even_when_the_orchestrator_raises() -> None:
    """The release must be in a ``finally`` — an exception mid-run must not
    leave the lock held forever."""
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    conn = _make_conn(_Row({"description_parsed": {"required_skills": []}}))
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock
        ) as release_lock,
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            await shortlist_job(ctx, str(job_id))

    release_lock.assert_awaited_once_with(conn, "shortlist", job_id)


@pytest.mark.asyncio
async def test_missing_job_row_still_releases_the_lock() -> None:
    """The "missing" early-return happens AFTER the lock is acquired (the
    row fetch is inside the locked section), so it must still release."""
    from src.worker.matching_tasks import shortlist_job

    conn = _make_conn(None)
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        patch(
            "src.worker.matching_tasks.try_job_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.matching_tasks.release_job_lock", new_callable=AsyncMock
        ) as release_lock,
    ):
        result = await shortlist_job(ctx, str(uuid4()))

    assert result == "missing"
    release_lock.assert_awaited_once()


# ── FU-configurable-shortlist-size slice B: shortlist_top_percent wiring ───
#
# Slice A (already GREEN on this branch) added ``jobs.shortlist_top_percent``
# (1-100, default 100) as a schema-only column. ``shortlist_job`` does not
# read it yet -- the job-status row fetch (``_JOB_META_SQL``) selects only
# ``description_parsed``, and ``generate_shortlist`` is called with no
# ``top_percent`` argument at all. Slice B requires ``shortlist_job`` to read
# the column off the SAME row fetch and forward it as ``top_percent`` into
# ``generate_shortlist`` -- these tests fail today because the forwarded
# kwarg is simply absent (``generate.await_args.kwargs`` has no
# ``"top_percent"`` key), even though the mock itself would silently accept
# any kwargs.


@pytest.mark.asyncio
async def test_shortlist_job_forwards_the_jobs_shortlist_top_percent_column() -> None:
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    conn = _make_conn(
        _Row(
            {
                "description_parsed": {"required_skills": []},
                "shortlist_top_percent": 30,
            }
        )
    )
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=ShortlistResult(job_id=job_id, entries=[]),
        ) as generate,
        patch("src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock),
    ):
        await shortlist_job(ctx, str(job_id))

    assert generate.await_args is not None
    assert generate.await_args.kwargs.get("top_percent") == 30, (
        "shortlist_job must read shortlist_top_percent off the job row and "
        "forward it as generate_shortlist(..., top_percent=<value>) -- got "
        f"kwargs={generate.await_args.kwargs!r}"
    )


@pytest.mark.asyncio
async def test_shortlist_job_forwards_a_non_default_shortlist_top_percent() -> None:
    """A second, DIFFERENT value from the test above -- a builder that
    hardcodes 30 (or any other single literal) instead of actually reading
    the row would pass the first test but fail this one."""
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    conn = _make_conn(
        _Row(
            {
                "description_parsed": {"required_skills": []},
                "shortlist_top_percent": 75,
            }
        )
    )
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=ShortlistResult(job_id=job_id, entries=[]),
        ) as generate,
        patch("src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock),
    ):
        await shortlist_job(ctx, str(job_id))

    assert generate.await_args.kwargs.get("top_percent") == 75


@pytest.mark.asyncio
async def test_shortlist_job_forwards_the_default_top_percent_of_100() -> None:
    """A job that never customised its shortlist size (the DDL default, 100)
    must forward exactly 100 -- the value that keeps generate_shortlist's
    cap a no-op, so the eval corpus (which never sets this column) sees
    byte-identical behaviour."""
    from src.worker.matching_tasks import shortlist_job

    job_id = uuid4()
    conn = _make_conn(
        _Row(
            {
                "description_parsed": {"required_skills": []},
                "shortlist_top_percent": 100,
            }
        )
    )
    ctx = _make_ctx(conn)
    lock_patch, release_patch = _lock_patches()

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
        lock_patch,
        release_patch,
        patch(
            "src.worker.matching_tasks.generate_shortlist",
            new_callable=AsyncMock,
            return_value=ShortlistResult(job_id=job_id, entries=[]),
        ) as generate,
        patch("src.worker.matching_tasks.persist_shortlist", new_callable=AsyncMock),
    ):
        await shortlist_job(ctx, str(job_id))

    assert generate.await_args.kwargs.get("top_percent") == 100
