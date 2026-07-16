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


# ── missing / not-parsed ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_job_row_returns_missing_and_orchestrator_not_called() -> None:
    from src.worker.matching_tasks import shortlist_job

    conn = _make_conn(None)
    ctx = _make_ctx(conn)

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
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

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
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

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
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

    empty_result = ShortlistResult(job_id=job_id, entries=[])

    with (
        patch("src.worker.matching_tasks.get_settings", return_value=Settings()),
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
