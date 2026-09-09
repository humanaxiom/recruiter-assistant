"""RED pin — ranking must not silently drop candidates the graph has not seen.

**Found by `scripts/smoke.sh`, 2026-08-22, and invisible to every other gate.**
Three résumés uploaded through the real UI all parsed successfully, with 56, 48
and 33 skills extracted. The shortlist came back with **two** of them. The third
— the one with the MOST skills — was missing. Re-running the ranking minutes
later, with nothing else changed, produced all three.

The cause is a race between two sources of truth. `resumes.status` becomes
``parsed`` in Postgres the moment the LLM pipeline finishes, but ranking reads
Neo4j, and graph projection is an asynchronous cron (`project_to_graph`, every
5s). Between those two events a résumé is *parsed* and *unrankable*, and Stage 1
recall (``MATCH (r:Resume {job_id: $jid})``) simply does not see it.

**Why this is a product defect and not a test artefact.** The résumé table shows
``parsed``. A recruiter watching it click Generate the moment the last row turns
green gets a shortlist missing candidates — with no error, no warning, and no
way to tell which. On a 39-résumé pilot batch that is a candidate silently
excluded from consideration, which is the single worst thing this product can
do.

No unit or integration test could catch it: both either mock the graph or drive
projection synchronously, so the window does not exist for them.

**The fix defers rather than blocks**, and is BOUNDED. `project_to_graph` runs
every 5s, so a short retry resolves the ordinary case; but a résumé whose
projection genuinely failed would otherwise defer for ever, so past the ceiling
the run proceeds and records that it did. Ranking a subset is the status quo —
doing it silently is the bug.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from arq import Retry

from src.worker import matching_tasks


def _neo4j(projected: int) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.single = AsyncMock(return_value={"n": projected})
    session.run = AsyncMock(return_value=result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=ctx)
    return driver


def _conn(eligible: int) -> MagicMock:
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=eligible)
    return conn


async def test_ranking_defers_while_projection_is_behind() -> None:
    """The reported case: 3 parsed in Postgres, 2 visible in the graph."""
    with pytest.raises(Retry):
        await matching_tasks.ensure_projection_caught_up(
            _conn(3), _neo4j(2), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
        )


async def test_ranking_proceeds_once_the_graph_has_caught_up() -> None:
    await matching_tasks.ensure_projection_caught_up(
        _conn(3), _neo4j(3), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )


async def test_a_graph_ahead_of_postgres_never_defers() -> None:
    """Projection can legitimately hold nodes Postgres no longer counts — a
    withdrawn résumé keeps its node. Only a graph BEHIND the eligible set can
    drop a candidate from ranking, so only that direction may defer."""
    await matching_tasks.ensure_projection_caught_up(
        _conn(2), _neo4j(5), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )


async def test_the_defer_is_bounded_so_a_failed_projection_cannot_wedge_it() -> None:
    """A résumé whose projection genuinely failed is never coming. Deferring
    for ever would turn one broken row into a job that can never be ranked —
    strictly worse than the bug being fixed."""
    await matching_tasks.ensure_projection_caught_up(
        _conn(3), _neo4j(2), job_id=uuid4(), job_try=5, max_tries=5, defer_s=5
    )


async def test_nothing_eligible_never_defers() -> None:
    """An empty job ranks empty. Deferring would pin the UI on 'Generating…'
    for a job with no parsed résumés at all."""
    await matching_tasks.ensure_projection_caught_up(
        _conn(0), _neo4j(0), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )


async def test_an_unreachable_graph_does_not_defer() -> None:
    """Fail OPEN here, deliberately, and it is the one place in this repo that
    does. If Neo4j cannot be counted, the ranking that follows will fail on its
    own and report properly; turning an unreadable count into an infinite defer
    would replace a loud failure with a silent one."""
    driver = MagicMock()
    driver.session = MagicMock(side_effect=OSError("neo4j down"))
    await matching_tasks.ensure_projection_caught_up(
        _conn(3), driver, job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )


async def test_the_eligible_count_excludes_withdrawn_resumes() -> None:
    """A withdrawn résumé is not a candidate, so it must not hold up ranking
    for everyone else."""
    conn = _conn(3)
    await matching_tasks.ensure_projection_caught_up(
        conn, _neo4j(3), job_id=uuid4(), job_try=1, max_tries=5, defer_s=5
    )
    sql = str(conn.fetchval.await_args.args[0]).lower()
    assert "withdrawn_at is null" in sql
    assert "parsed" in sql


def test_the_helper_is_wired_into_shortlist_job() -> None:
    """A guard that exists but is never called is the ROADMAP A7 shape, and
    this repo has shipped that exact thing repeatedly."""
    import inspect

    src = inspect.getsource(matching_tasks.shortlist_job)
    assert "ensure_projection_caught_up" in src


# ── Live demo, 2026-09-09 21:42 — a degraded parse wedges ranking forever ───
#
# 10 résumés parsed for one job; one had its skills LLM pass fail (empty
# content), fell back to the keyword scan, and was persisted with
# ``parsed->>'degraded' = true``. By design (FU-7 §4 / ADR-030,
# ``resume_tasks.py``'s ``degraded_skip_projection``) a degraded parse is
# NEVER enqueued for projection — no Neo4j node, no ranking. But
# ``_ELIGIBLE_SQL`` counted it anyway (``status = 'parsed' AND withdrawn_at
# IS NULL`` says nothing about ``degraded``), so ``projected`` (9) could never
# catch ``eligible`` (10) — the run deferred every one of ``shortlist_max_tries``
# (20) x 45s (15 minutes, silently) before ranking the nine it could. Two
# fail-closed decisions, each correct alone, wedged the demo together.
#
# The fix: ``_ELIGIBLE_SQL`` excludes a degraded parse with the SAME
# expression ``resume_service`` already uses (``status_breakdown`` ~451,
# ``list_for_job`` ~799) — ``AND NOT COALESCE((parsed->>'degraded')::bool,
# false)`` — so a degraded résumé is never "eligible" for a graph it will
# never reach.


def test_the_eligible_sql_excludes_degraded_parses() -> None:
    """Pin the exact predicate, matching ``resume_service``'s own
    ``COALESCE((parsed->>'degraded')::bool, false)`` expression byte-for-byte
    (mirrors the existing ``lower()``-normalized style of the withdrawn-at
    pin above) so a future refactor of either side cannot silently drift the
    other out of sync."""
    sql = matching_tasks._ELIGIBLE_SQL.lower()
    assert "coalesce((parsed->>'degraded')::bool, false)" in sql
    assert "not coalesce((parsed->>'degraded')::bool, false)" in sql, (
        "the predicate must EXCLUDE a degraded parse, not merely reference "
        "the column — a bare COALESCE with no NOT still counts it as eligible"
    )


async def test_the_eligible_query_excludes_degraded_alongside_withdrawn() -> None:
    """Same style as ``test_the_eligible_count_excludes_withdrawn_resumes``
    above: assert on the ACTUAL SQL text handed to ``conn.fetchval``, not a
    hand-duplicated string, so the two tests cannot both pass against a query
    that dropped one predicate to satisfy the other."""
    conn = _conn(9)
    await matching_tasks.ensure_projection_caught_up(
        conn, _neo4j(9), job_id=uuid4(), job_try=1, max_tries=20, defer_s=45
    )
    sql = str(conn.fetchval.await_args.args[0]).lower()
    assert "withdrawn_at is null" in sql
    assert "not coalesce((parsed->>'degraded')::bool, false)" in sql


async def test_ranking_proceeds_when_the_degraded_parse_is_excluded_from_eligible() -> (
    None
):
    """The reported incident, reproduced at its own numbers: 10 résumés
    parsed, 1 degraded (never projected by design), 9 correctly projected.
    Once ``_ELIGIBLE_SQL`` excludes the degraded row, ``eligible`` reads 9 —
    not 10 — so a graph that has caught up on every résumé it was ever going
    to receive must NOT raise ``Retry``. Before the fix, ``_conn(9)`` here
    stood in for the OLD (defective) query still counting all 10; this pins
    what the query must now return for the same real data: 9, matching
    ``projected``."""
    await matching_tasks.ensure_projection_caught_up(
        _conn(9), _neo4j(9), job_id=uuid4(), job_try=1, max_tries=20, defer_s=45
    )


async def test_a_degraded_parse_deferred_under_the_old_query_would_never_catch_up() -> (
    None
):
    """Documents the FAILURE mode this fix removes: if ``eligible`` still
    counted the degraded row (10) against a graph that will only ever reach
    9, every retry up to the ceiling defers, and the run only proceeds once
    ``job_try`` reaches ``max_tries`` — the 15-minute silent wait from the
    incident. This is the behaviour the SQL-predicate tests above must make
    unreachable in the real query; it is pinned here via the same helpers,
    with a low ``job_try`` standing in for "not yet at the ceiling"."""
    with pytest.raises(Retry):
        await matching_tasks.ensure_projection_caught_up(
            _conn(10), _neo4j(9), job_id=uuid4(), job_try=1, max_tries=20, defer_s=45
        )
