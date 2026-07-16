"""Unit tests for ``src.services.shortlist_service.persist_shortlist`` /
``persist_reverse_match`` — the Phase 4d write-only persistence contract.
All I/O is mocked: a bare ``MagicMock`` connection whose ``execute`` is an
``AsyncMock``, decoding the SQL args it was called with (no real Postgres —
see ``test_shortlist_persistence_pg.py`` for the same contract proven against
a real Postgres, incl. the ``UNIQUE`` constraints these DELETE-first calls
must satisfy).

``src.services.shortlist_service`` does not exist yet — RED half of the TDD
cycle; this whole file is expected to fail at collection
(``ModuleNotFoundError``).

The core asymmetry this file pins (verified against ``src/models/ddl.py``):

* ``shortlist_entries`` has NO ``score_structured``/``score_evidence``
  columns and ``evidence JSONB NOT NULL`` -> ``persist_shortlist`` FOLDS
  ``score_structured``/``score_evidence`` into the ``score_breakdown`` jsonb
  dict and coerces ``evidence=None`` to the JSON literal ``{}``.
* ``reverse_match_entries`` HAS dedicated ``score_structured``/
  ``score_evidence`` columns and ``evidence JSONB`` (nullable) ->
  ``persist_reverse_match`` passes those as their OWN SQL args (never folded
  into the breakdown dict) and passes ``evidence=None`` through as SQL NULL,
  never coerced to ``{}``.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.pipeline.matching.orchestrator import (
    JobMatchResult,
    JobMatchResultEntry,
    ShortlistResult,
    ShortlistResultEntry,
)
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    MatchWeights,
    PipelineMeta,
    ScoreBreakdown,
    SkillContribution,
)


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
        motivation=0.1,
        implied_experience=False,
        skill_contributions=[
            SkillContribution(
                skill="python", score=1.0, years=5, is_must_have=True, reason=None
            ),
            SkillContribution(
                skill="rest-api-design",
                score=0.0,
                years=None,
                is_must_have=True,
                reason="missing",
            ),
        ],
    )


def _meta(
    git_sha: str | None = "sha-abc-123", weights: MatchWeights = DEFAULT_WEIGHTS
) -> PipelineMeta:
    return PipelineMeta(
        model_gen="test-gen",
        model_emb="test-emb",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=weights,
        git_sha=git_sha,
        generated_at=dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
        timings_ms={"stage1_ms": 10},
    )


def _json_args(call: Any) -> list[dict[str, Any]]:
    """Every argument in a mock call that decodes as a JSON object."""
    out: list[dict[str, Any]] = []
    for arg in list(call.args) + list(call.kwargs.values()):
        if not isinstance(arg, str):
            continue
        try:
            decoded = json.loads(arg)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(decoded, dict):
            out.append(decoded)
    return out


def _find_by_key(dicts: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for d in dicts:
        if key in d:
            return d
    return None


def _ints(call: Any) -> list[int]:
    return [
        a
        for a in list(call.args) + list(call.kwargs.values())
        if isinstance(a, int) and not isinstance(a, bool)
    ]


def _floats(call: Any) -> list[float]:
    return [
        a for a in list(call.args) + list(call.kwargs.values()) if isinstance(a, float)
    ]


def _flat(call: Any) -> list[Any]:
    return list(call.args) + list(call.kwargs.values())


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock()
    return conn


# ── persist_shortlist ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_shortlist_deletes_before_inserting_even_with_no_entries() -> (
    None
):
    from src.services.shortlist_service import persist_shortlist

    conn = _mock_conn()
    job_id = uuid4()
    result = ShortlistResult(job_id=job_id, entries=[], pipeline_meta=_meta())

    await persist_shortlist(conn, result)

    assert conn.execute.await_count == 1, (
        "an empty result must still issue the DELETE (clearing a stale prior "
        "run) even though there is nothing to insert"
    )
    first_call = conn.execute.await_args_list[0]
    query = first_call.args[0]
    assert "DELETE" in query.upper()
    assert "shortlist_entries" in query
    assert job_id in _flat(first_call)


@pytest.mark.asyncio
async def test_persist_shortlist_preserves_rank_order_1_to_n() -> None:
    from src.services.shortlist_service import persist_shortlist

    conn = _mock_conn()
    job_id = uuid4()
    entries = [
        ShortlistResultEntry(
            resume_id=uuid4(),
            rank=i,
            score_final=0.9 - 0.01 * i,
            score_structured=0.8,
            score_evidence=0.7,
            breakdown=_breakdown(),
            evidence=None,
        )
        for i in range(1, 4)
    ]
    result = ShortlistResult(job_id=job_id, entries=entries, pipeline_meta=_meta())

    await persist_shortlist(conn, result)

    insert_calls = conn.execute.await_args_list[1:]
    assert len(insert_calls) == 3
    for i, call in enumerate(insert_calls, start=1):
        assert i in _ints(call), f"expected rank={i} among the INSERT's int args"


@pytest.mark.asyncio
async def test_persist_shortlist_folds_structured_and_evidence_into_breakdown() -> None:
    from src.services.shortlist_service import persist_shortlist

    conn = _mock_conn()
    job_id = uuid4()
    breakdown = _breakdown()
    entry = ShortlistResultEntry(
        resume_id=uuid4(),
        rank=1,
        score_final=0.9,
        score_structured=0.77,
        score_evidence=0.66,
        breakdown=breakdown,
        evidence=None,
    )
    result = ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())

    await persist_shortlist(conn, result)

    insert_call = conn.execute.await_args_list[1]
    dicts = _json_args(insert_call)
    breakdown_json = _find_by_key(dicts, "skill_contributions")
    assert breakdown_json is not None, "score_breakdown jsonb must be present"
    assert breakdown_json["score_structured"] == pytest.approx(0.77)
    assert breakdown_json["score_evidence"] == pytest.approx(0.66)
    # full breakdown preserved verbatim alongside the folded-in fields
    assert breakdown_json["skill"] == pytest.approx(breakdown.skill)
    assert breakdown_json["experience"] == pytest.approx(breakdown.experience)
    assert breakdown_json["skill_contributions"] == [
        c.model_dump() for c in breakdown.skill_contributions
    ]


@pytest.mark.asyncio
async def test_persist_shortlist_none_evidence_persists_as_empty_json_object() -> None:
    from src.services.shortlist_service import persist_shortlist

    conn = _mock_conn()
    job_id = uuid4()
    entry = ShortlistResultEntry(
        resume_id=uuid4(),
        rank=1,
        score_final=0.5,
        score_structured=0.5,
        score_evidence=0.0,
        breakdown=_breakdown(),
        evidence=None,
    )
    result = ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=_meta())

    await persist_shortlist(conn, result)

    insert_call = conn.execute.await_args_list[1]
    args = _flat(insert_call)
    assert None not in args, (
        "shortlist_entries.evidence is NOT NULL — evidence=None must never be "
        "passed through as SQL NULL"
    )
    dicts = _json_args(insert_call)
    breakdown_key = "skill_contributions"
    meta_key = "weights"
    remaining = [d for d in dicts if breakdown_key not in d and meta_key not in d]
    assert {} in remaining, (
        "evidence=None must serialize to the literal JSON object {} "
        "(a valid, empty EvidenceObject shape)"
    )


@pytest.mark.asyncio
async def test_persist_shortlist_pipeline_meta_carries_full_weights_and_git_sha() -> (
    None
):
    from src.services.shortlist_service import persist_shortlist

    conn = _mock_conn()
    job_id = uuid4()
    custom_weights = MatchWeights(
        skill=0.5, experience=0.15, education=0.10, seniority=0.15, vector=0.10
    )
    meta = _meta(git_sha="sha-xyz-999", weights=custom_weights)
    entry = ShortlistResultEntry(
        resume_id=uuid4(),
        rank=1,
        score_final=0.5,
        score_structured=0.5,
        score_evidence=0.5,
        breakdown=_breakdown(),
        evidence=None,
    )
    result = ShortlistResult(job_id=job_id, entries=[entry], pipeline_meta=meta)

    await persist_shortlist(conn, result)

    insert_call = conn.execute.await_args_list[1]
    dicts = _json_args(insert_call)
    meta_json = _find_by_key(dicts, "weights")
    assert meta_json is not None, "pipeline_meta jsonb must be present"
    assert meta_json["git_sha"] == "sha-xyz-999"
    assert meta_json["weights"]["skill"] == pytest.approx(0.5)
    assert meta_json["weights"]["experience"] == pytest.approx(0.15)
    assert meta_json["model_gen"] == "test-gen"


# ── persist_reverse_match ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_reverse_match_deletes_before_inserting_with_no_entries() -> None:
    from src.services.shortlist_service import persist_reverse_match

    conn = _mock_conn()
    resume_id = uuid4()
    result = JobMatchResult(resume_id=resume_id, entries=[], pipeline_meta=_meta())

    await persist_reverse_match(conn, result)

    assert conn.execute.await_count == 1
    first_call = conn.execute.await_args_list[0]
    query = first_call.args[0]
    assert "DELETE" in query.upper()
    assert "reverse_match_entries" in query
    assert resume_id in _flat(first_call)


@pytest.mark.asyncio
async def test_persist_reverse_match_preserves_rank_order_1_to_n() -> None:
    from src.services.shortlist_service import persist_reverse_match

    conn = _mock_conn()
    resume_id = uuid4()
    entries = [
        JobMatchResultEntry(
            job_id=uuid4(),
            title=f"Job {i}",
            rank=i,
            score_final=0.9 - 0.01 * i,
            score_structured=0.8,
            score_evidence=0.7,
            breakdown=_breakdown(),
            evidence=None,
            requirement_count=5,
            must_have_count=3,
        )
        for i in range(1, 4)
    ]
    result = JobMatchResult(resume_id=resume_id, entries=entries, pipeline_meta=_meta())

    await persist_reverse_match(conn, result)

    insert_calls = conn.execute.await_args_list[1:]
    assert len(insert_calls) == 3
    for i, call in enumerate(insert_calls, start=1):
        assert i in _ints(call)


@pytest.mark.asyncio
async def test_persist_reverse_match_keeps_structured_and_evidence_dedicated() -> None:
    from src.services.shortlist_service import persist_reverse_match

    conn = _mock_conn()
    resume_id = uuid4()
    entry = JobMatchResultEntry(
        job_id=uuid4(),
        title="Eng",
        rank=1,
        score_final=0.9,
        score_structured=0.81,
        score_evidence=0.42,
        breakdown=_breakdown(),
        evidence=None,
        requirement_count=5,
        must_have_count=3,
    )
    result = JobMatchResult(resume_id=resume_id, entries=[entry], pipeline_meta=_meta())

    await persist_reverse_match(conn, result)

    insert_call = conn.execute.await_args_list[1]
    floats = _floats(insert_call)
    assert 0.81 in floats, "score_structured must be its own dedicated SQL arg"
    assert 0.42 in floats, "score_evidence must be its own dedicated SQL arg"

    dicts = _json_args(insert_call)
    breakdown_json = _find_by_key(dicts, "skill_contributions")
    assert breakdown_json is not None
    assert "score_structured" not in breakdown_json, (
        "reverse_match_entries has dedicated score_structured/score_evidence "
        "columns — unlike shortlist_entries, they must NOT be folded into "
        "the breakdown jsonb"
    )
    assert "score_evidence" not in breakdown_json


@pytest.mark.asyncio
async def test_persist_reverse_match_none_evidence_passes_through_as_sql_null() -> None:
    from src.services.shortlist_service import persist_reverse_match

    conn = _mock_conn()
    resume_id = uuid4()
    entry = JobMatchResultEntry(
        job_id=uuid4(),
        title="Eng",
        rank=1,
        score_final=0.5,
        score_structured=0.5,
        score_evidence=0.5,
        breakdown=_breakdown(),
        evidence=None,
        requirement_count=5,
        must_have_count=3,
    )
    result = JobMatchResult(resume_id=resume_id, entries=[entry], pipeline_meta=_meta())

    await persist_reverse_match(conn, result)

    insert_call = conn.execute.await_args_list[1]
    args = _flat(insert_call)
    assert None in args, (
        "reverse_match_entries.evidence IS nullable — evidence=None must pass "
        "through as SQL NULL"
    )
    dicts = _json_args(insert_call)
    assert {} not in dicts, (
        "evidence=None must NOT be coerced to the empty-object literal on the "
        "reverse-match path (that coercion is the shortlist-only asymmetry)"
    )


@pytest.mark.asyncio
async def test_persist_reverse_match_requirement_and_must_have_counts_verbatim() -> (
    None
):
    from src.services.shortlist_service import persist_reverse_match

    conn = _mock_conn()
    resume_id = uuid4()
    entry = JobMatchResultEntry(
        job_id=uuid4(),
        title="Eng",
        rank=1,
        score_final=0.5,
        score_structured=0.5,
        score_evidence=0.5,
        breakdown=_breakdown(),
        evidence=None,
        requirement_count=17,
        must_have_count=9,
    )
    result = JobMatchResult(resume_id=resume_id, entries=[entry], pipeline_meta=_meta())

    await persist_reverse_match(conn, result)

    insert_call = conn.execute.await_args_list[1]
    ints = _ints(insert_call)
    assert 17 in ints
    assert 9 in ints


@pytest.mark.asyncio
async def test_persist_reverse_match_pipeline_meta_carries_weights_and_git_sha() -> (
    None
):
    from src.services.shortlist_service import persist_reverse_match

    conn = _mock_conn()
    resume_id = uuid4()
    custom_weights = MatchWeights(structured=0.7, evidence=0.2, motivation=0.1)
    meta = _meta(git_sha="reverse-sha-42", weights=custom_weights)
    entry = JobMatchResultEntry(
        job_id=uuid4(),
        title="Eng",
        rank=1,
        score_final=0.5,
        score_structured=0.5,
        score_evidence=0.5,
        breakdown=_breakdown(),
        evidence=None,
        requirement_count=5,
        must_have_count=3,
    )
    result = JobMatchResult(resume_id=resume_id, entries=[entry], pipeline_meta=meta)

    await persist_reverse_match(conn, result)

    insert_call = conn.execute.await_args_list[1]
    dicts = _json_args(insert_call)
    meta_json = _find_by_key(dicts, "weights")
    assert meta_json is not None
    assert meta_json["git_sha"] == "reverse-sha-42"
    assert meta_json["weights"]["structured"] == pytest.approx(0.7)
