"""Unit tests — FU-7 §2 fail-closed ranking (ADR-021 §2 / ADR-029).

Pins the propagation contract at three levels of
``src.pipeline.matching.orchestrator``, all I/O mocked:

1. ``_stage3_per_candidate`` — currently catches ``LLMOutputInvalidError``,
   logs, and returns ``None`` (silently zeroing that candidate's evidence).
   Must instead RE-RAISE. ``LLMUnavailableError`` is already uncaught here
   and must keep propagating.
2. ``stage3_evidence._one`` — currently isolates ANY exception to ``None``
   per candidate. Must catch ``(LLMOutputInvalidError, LLMUnavailableError)``
   FIRST and re-raise (fail closed — one such failure withholds the WHOLE
   shortlist), while a genuinely unexpected ``Exception`` for one candidate
   still isolates to ``None`` (existing behaviour preserved).
3. ``generate_shortlist`` — wraps BOTH the stage-2 per-candidate loop (the
   embedder call for the seniority cosine can raise ``LLMUnavailableError``)
   and the ``stage3_evidence`` call: any Mode A/B failure anywhere in that
   path becomes a single typed ``RankingUnavailableError`` — the new
   exception ``src.pipeline.matching.orchestrator.RankingUnavailableError``
   does not exist yet, so importing it is itself part of the RED signal. A
   GENERIC per-candidate exception (already isolated inside stage3_evidence)
   must still produce a normal, non-raising shortlist.

Every test here white-box patches orchestrator module-level functions
(``load_job_view`` / ``stage1_coarse`` / ``_stage2_per_candidate`` /
``stage3_evidence`` / ``_stage3_per_candidate``) exactly like the existing
precedent in ``test_matching_context_settings_wiring.py`` and
``test_matching_orchestrator.py`` (``load_prompt`` patched to a fixed
``RenderedPrompt``) — no real Postgres/Neo4j/Ollama needed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from src.pipeline.llm import LLMOutputInvalidError, LLMUnavailableError
from src.pipeline.matching.orchestrator import (
    JobView,
    MatchingContext,
    Stage1Candidate,
    Stage2Candidate,
    generate_shortlist,
    stage3_evidence,
)
from src.prompts import RenderedPrompt
from src.schemas.matching import EvidenceObjectIngest, ScoreBreakdown

_STUBBED_PROMPT = RenderedPrompt(version="test-stub", system="system", user="user")


def _job(*, required_skills: tuple[str, ...] = ("Python",)) -> JobView:
    return JobView(
        id=uuid4(),
        title="Senior Backend Engineer",
        min_years=None,
        education_min_level=None,
        education_fields=(),
        required_skills=required_skills,
        nice_to_have_skills=(),
    )


def _breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        skill=0.8,
        experience=0.7,
        education=0.6,
        seniority=0.5,
        vector=0.4,
        structured=0.65,
    )


def _stage2_candidate(resume_id: UUID) -> Stage2Candidate:
    return Stage2Candidate(
        resume_id=resume_id, vec_score=0.9, structured=0.65, breakdown=_breakdown()
    )


def _ctx(*, llm: MagicMock | None = None, embedder: MagicMock | None = None) -> Any:
    return MatchingContext(
        db=MagicMock(name="db"),
        neo4j=MagicMock(name="neo4j"),
        llm=llm or MagicMock(name="llm"),
        embedder=embedder or MagicMock(name="embedder"),
        model_gen="test-gen",
        model_emb="test-emb",
    )


# ── 1. _stage3_per_candidate re-raises LLMOutputInvalidError ────────────────


@pytest.mark.asyncio
async def test_stage3_per_candidate_reraises_llm_output_invalid_error() -> None:
    from src.pipeline.matching.orchestrator import _stage3_per_candidate

    job = _job()
    candidate = _stage2_candidate(uuid4())
    parsed = {"chunks": [{"id": "c_001", "section": "summary", "page": 0, "text": "x"}]}
    llm = MagicMock(
        chat_json=AsyncMock(side_effect=LLMOutputInvalidError("empty response"))
    )
    ctx = _ctx(llm=llm)

    with patch(
        "src.pipeline.matching.orchestrator.load_prompt", return_value=_STUBBED_PROMPT
    ):
        with pytest.raises(LLMOutputInvalidError):
            await _stage3_per_candidate(ctx, job, candidate, parsed)


@pytest.mark.asyncio
async def test_stage3_per_candidate_llm_unavailable_error_still_propagates() -> None:
    """Already-uncaught behaviour, pinned as a regression guard: a
    ``LLMUnavailableError`` from ``chat_json`` must never be silently
    swallowed either."""
    from src.pipeline.matching.orchestrator import _stage3_per_candidate

    job = _job()
    candidate = _stage2_candidate(uuid4())
    parsed = {"chunks": [{"id": "c_001", "section": "summary", "page": 0, "text": "x"}]}
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMUnavailableError("ollama down")))
    ctx = _ctx(llm=llm)

    with patch(
        "src.pipeline.matching.orchestrator.load_prompt", return_value=_STUBBED_PROMPT
    ):
        with pytest.raises(LLMUnavailableError):
            await _stage3_per_candidate(ctx, job, candidate, parsed)


# ── 2. stage3_evidence: re-raise LLMOutputInvalidError/LLMUnavailableError, ──
#       but still isolate a generic Exception to None ───────────────────────


@pytest.mark.asyncio
async def test_stage3_evidence_reraises_llm_output_invalid_error() -> None:
    job = _job()
    resume_id = uuid4()
    candidate = _stage2_candidate(resume_id)
    ctx = _ctx()
    ctx.db.fetchrow = AsyncMock(return_value={"parsed": {"chunks": []}})

    with patch(
        "src.pipeline.matching.orchestrator._stage3_per_candidate",
        new_callable=AsyncMock,
        side_effect=LLMOutputInvalidError("empty response"),
    ):
        with pytest.raises(LLMOutputInvalidError):
            await stage3_evidence(ctx, job, [candidate])


@pytest.mark.asyncio
async def test_stage3_evidence_reraises_llm_unavailable_error() -> None:
    job = _job()
    resume_id = uuid4()
    candidate = _stage2_candidate(resume_id)
    ctx = _ctx()
    ctx.db.fetchrow = AsyncMock(return_value={"parsed": {"chunks": []}})

    with patch(
        "src.pipeline.matching.orchestrator._stage3_per_candidate",
        new_callable=AsyncMock,
        side_effect=LLMUnavailableError("ollama down"),
    ):
        with pytest.raises(LLMUnavailableError):
            await stage3_evidence(ctx, job, [candidate])


@pytest.mark.asyncio
async def test_stage3_evidence_isolates_a_generic_exception_to_none() -> None:
    """A GENERIC (non-LLM) per-candidate exception must still be isolated —
    the existing "one candidate must not sink all" behaviour is preserved
    for anything that is NOT a fail-closed Mode A/B signal."""
    job = _job()
    good_id = uuid4()
    bad_id = uuid4()
    good_candidate = _stage2_candidate(good_id)
    bad_candidate = _stage2_candidate(bad_id)
    ctx = _ctx()
    ctx.db.fetchrow = AsyncMock(return_value={"parsed": {"chunks": []}})

    good_evidence = EvidenceObjectIngest()

    async def _fake_stage3(
        _ctx: Any, _job: Any, candidate: Stage2Candidate, _parsed: Any, **_kw: Any
    ) -> EvidenceObjectIngest | None:
        if candidate.resume_id == bad_id:
            raise RuntimeError("totally unexpected")
        return good_evidence

    with patch(
        "src.pipeline.matching.orchestrator._stage3_per_candidate",
        new_callable=AsyncMock,
        side_effect=_fake_stage3,
    ):
        results = await stage3_evidence(ctx, job, [good_candidate, bad_candidate])

    assert results[bad_id] is None
    assert results[good_id] is good_evidence


# ── 3. generate_shortlist wraps Mode A/B failures into RankingUnavailableError

_DIM = 4


def _vec() -> list[float]:
    return [0.1] * _DIM


@pytest.mark.asyncio
async def test_generate_shortlist_raises_ranking_unavailable_when_stage2_embedder_fails() -> (  # noqa: E501
    None
):
    """A LLMUnavailableError from ``ctx.embedder.embed`` (the stage-2
    seniority-cosine call) inside the per-candidate loop must become a
    RankingUnavailableError, not escape ``generate_shortlist`` as a bare
    LLMUnavailableError (which would NOT trigger an arq retry — ADR-027)."""
    from src.pipeline.matching.orchestrator import RankingUnavailableError

    job_id = uuid4()
    job = _job(required_skills=())
    resume_id = uuid4()
    embedder = MagicMock(embed=AsyncMock(side_effect=LLMUnavailableError("embed down")))
    ctx = _ctx(embedder=embedder)

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch.object(
            ctx.db,
            "fetchrow",
            new=AsyncMock(
                return_value={
                    "parsed": {
                        "total_years_experience": 5,
                        "experience": [
                            {"title": "Senior Backend Engineer", "is_current": True}
                        ],
                        "education": [],
                    }
                }
            ),
        ),
        patch.object(
            ctx.neo4j,
            "session",
            new=MagicMock(return_value=_empty_neo4j_session_cm()),
        ),
    ):
        with pytest.raises(RankingUnavailableError):
            await generate_shortlist(job_id, ctx)


def _empty_neo4j_session_cm() -> MagicMock:
    class _EmptyResult:
        def __aiter__(self) -> _EmptyResult:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

    session = MagicMock(name="session")
    session.run = AsyncMock(return_value=_EmptyResult())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_generate_shortlist_raises_ranking_unavailable_when_stage3_fails() -> (
    None
):
    from src.pipeline.matching.orchestrator import RankingUnavailableError

    job_id = uuid4()
    job = _job()
    resume_id = uuid4()
    candidate2 = _stage2_candidate(resume_id)

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch(
            "src.pipeline.matching.orchestrator._stage2_per_candidate",
            new_callable=AsyncMock,
            return_value=candidate2,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage3_evidence",
            new_callable=AsyncMock,
            side_effect=LLMOutputInvalidError("empty response"),
        ),
    ):
        ctx = _ctx()
        with pytest.raises(RankingUnavailableError):
            await generate_shortlist(job_id, ctx)


@pytest.mark.asyncio
async def test_generate_shortlist_raises_ranking_unavailable_when_stage3_llm_unavailable() -> (  # noqa: E501
    None
):
    from src.pipeline.matching.orchestrator import RankingUnavailableError

    job_id = uuid4()
    job = _job()
    resume_id = uuid4()
    candidate2 = _stage2_candidate(resume_id)

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch(
            "src.pipeline.matching.orchestrator._stage2_per_candidate",
            new_callable=AsyncMock,
            return_value=candidate2,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage3_evidence",
            new_callable=AsyncMock,
            side_effect=LLMUnavailableError("ollama down"),
        ),
    ):
        ctx = _ctx()
        with pytest.raises(RankingUnavailableError):
            await generate_shortlist(job_id, ctx)


@pytest.mark.asyncio
async def test_generate_shortlist_still_produces_a_normal_shortlist_on_a_generic_per_candidate_error() -> (  # noqa: E501
    None
):
    """A generic per-candidate exception is ALREADY isolated to ``None``
    inside ``stage3_evidence`` (see the dedicated test above) — that
    isolation must not be defeated by the new fail-closed wrapping.
    ``generate_shortlist`` must complete normally (no raise) and produce a
    non-empty shortlist when ``stage3_evidence`` itself returns normally."""
    job_id = uuid4()
    job = _job()
    resume_id = uuid4()
    candidate2 = _stage2_candidate(resume_id)

    with (
        patch(
            "src.pipeline.matching.orchestrator.load_job_view",
            new_callable=AsyncMock,
            return_value=job,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage1_coarse",
            new_callable=AsyncMock,
            return_value=[Stage1Candidate(resume_id=resume_id, vec_score=0.9)],
        ),
        patch(
            "src.pipeline.matching.orchestrator._stage2_per_candidate",
            new_callable=AsyncMock,
            return_value=candidate2,
        ),
        patch(
            "src.pipeline.matching.orchestrator.stage3_evidence",
            new_callable=AsyncMock,
            return_value={resume_id: None},  # the isolated-to-None outcome
        ),
    ):
        ctx = _ctx()
        result = await generate_shortlist(job_id, ctx)

    assert len(result.entries) == 1
    assert result.entries[0].resume_id == resume_id
