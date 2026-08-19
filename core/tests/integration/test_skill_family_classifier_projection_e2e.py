"""Integration -- ROADMAP A2, Phase 3.3 skill-family classifier, slice 1:
the PROJECTION half, against REAL Postgres + REAL Neo4j (testcontainers).

Proves for real what the mocked-call-capture unit tests
(``test_worker_project_resume.py``) and the pure module tests
(``test_skill_classifier.py``) prove by stubbing: a hashed (out-of-vocab)
skill whose parse-time-assigned ``categories`` ride the ``resume.parsed``
outbox payload really do land on the projected ``Skill`` node, and a JD
requiring an in-vocab skill in that SAME family really does earn stage-2
family credit it would otherwise score 0.0 / ``reason="missing"`` for
(``CLASSIFIER-SPEC.md``'s whole point).

The classifier itself is NOT exercised here -- it runs at PARSE time, calls
an LLM, and this slice's entire design claim is that PROJECTION gains no
LLM call at all (see ``CLASSIFIER-SPEC.md``'s "The design" section, and the
four ADR-008 tests in ``test_worker_project_resume.py`` this slice must
leave green). This file seeds the outbox payload's ``categories`` field
directly, exactly as ``_extract_skills_merged`` would already have attached
it before ``parse_resume`` ever enqueues the row.

``ResumeSkill.categories`` / the projection's categories-from-payload write
do not exist yet -- RED half of the TDD cycle (this file's positive-case
test fails against real Neo4j; the baseline test is expected to already
pass, establishing the "before" comparison point the spec asks for).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.pipeline.matching.orchestrator import _stage2_skill_rows
from src.pipeline.matching.stages import score_skill_breakdown
from src.schemas.matching import DEFAULT_WEIGHTS
from src.services import outbox_service
from src.worker.graph_tasks import project_to_graph
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema

# Reuse, not a second ad hoc copy: `tests/integration/` is a real package
# (`tests/__init__.py` + `tests/integration/__init__.py` both exist), so the
# sibling `test_graph_projection_e2e.py`'s helpers ARE importable, and the
# TESTER-agent instruction for this file was explicit: import if importable,
# only mirror otherwise. `_vec`'s near-orthogonality across distinct seeds
# (see that module's own docstring on `_vec`) is exactly what keeps the two
# tests below off the vector auto-merge/tiebreaker enrichment branches --
# duplicating the formula here would risk a silent drift from that property.
from tests.integration.test_graph_projection_e2e import _make_embedder, _make_llm, _vec

# ── fixtures (same technique as test_graph_projection_e2e.py) ─────────────


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture(scope="session")
def neo4j_container() -> Iterator[Neo4jContainer]:
    with Neo4jContainer("neo4j:5-community") as container:
        yield container


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE jobs, resumes, outbox CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture
async def neo4j_driver(neo4j_container: Neo4jContainer) -> AsyncIterator[AsyncDriver]:
    driver = AsyncGraphDatabase.driver(
        neo4j_container.get_connection_url(), auth=("neo4j", neo4j_container.password)
    )
    await bootstrap_neo4j_schema(driver)
    async with driver.session() as session:
        await session.run("MATCH (n) DETACH DELETE n")
    try:
        yield driver
    finally:
        await driver.close()


# NOTE (originally a DATA-PIPELINE-CODER fix, replaced with an import above
# by the TESTER agent): the job side's projection (`_project_job` ->
# `resolve_canonical_names` -> `_resolve_one`) still runs a REAL
# vector-recall round trip for a brand-new Skill node (this fixture DETACH
# DELETEs the whole graph per test, so even an in-vocab name like "python"
# has no existing node on its first write) -- see `skills_graph.py`'s
# `_resolve_one` step 2, `[emb] = await embedder.embed([normalised])`. A
# bare, unconfigured `AsyncMock()` (as this file originally had) returns a
# `MagicMock()` whose default `__iter__` yields nothing, so that unpack
# raises `ValueError` and the job.parsed row dead-letters -- entirely
# unrelated to anything this file's classifier scenario is actually testing
# (which never touches the job/LLM/embedder path at all; see the module
# docstring). `_make_llm`/`_make_embedder`/`_vec` are imported from the
# sibling `test_graph_projection_e2e.py` above rather than duplicated here.
def _ctx(pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver) -> dict[str, Any]:
    return {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }


async def _insert_job(pool: asyncpg.Pool) -> Any:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "Build and operate REST APIs and data pipelines on Python.",
        )


async def _insert_resume(pool: asyncpg.Pool, job_id: Any) -> Any:
    import uuid

    async with pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE, 'parsed')
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
        )


async def _enqueue_job_requiring_python(pool: asyncpg.Pool, job_id: Any) -> None:
    payload = {
        "embedding": _vec(1),
        "extracted": {
            "title": "Senior Backend Engineer",
            "required_skills": [{"name": "python", "min_years": 2}],
            "nice_to_have_skills": [],
            "min_years_experience": 2,
            "education": {"min_level": "bachelors", "fields": []},
            "location": None,
            "remote_policy": None,
            "responsibilities": [],
        },
        "prompt_version": "jd_extract_v1",
    }
    async with pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="job",
            aggregate_id=job_id,
            event_type="job.parsed",
            payload=payload,
        )


async def _enqueue_resume_with_one_skill(
    pool: asyncpg.Pool,
    *,
    resume_id: Any,
    job_id: Any,
    skill_name: str,
    categories: list[str] | None,
) -> None:
    skill: dict[str, Any] = {"name": skill_name, "years": 4, "evidence_chunk_ids": []}
    if categories is not None:
        skill["categories"] = categories
    payload = {
        "parsed": {
            "total_years_experience": 4,
            "skills": [skill],
            "experience": [],
            "education": [],
            "chunks": [],
            "cover_letter_chunks": [],
        },
        "summary_emb": _vec(2),
        "chunk_embs": {},
        "prompt_version": "resume_core_v1+resume_skills_v2",
        "job_id": str(job_id),
    }
    async with pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=resume_id,
            event_type="resume.parsed",
            payload=payload,
        )


# ── the actual feature, end to end ────────────────────────────────────────


@pytest.mark.asyncio
async def test_hashed_skill_node_gets_classifier_categories_from_the_payload(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="bespoke internal erp customisation",
        categories=["backend"],
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 1

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (:Resume)-[:HAS_SKILL]->(s:Skill) "
            "WHERE s.canonical_key STARTS WITH 'h:' "
            "RETURN s.canonical_key AS key, s.categories AS cats"
        )
        record = await result.single()
    assert record is not None
    assert record["key"].startswith("h:")
    assert record["cats"] == ["backend"], (
        "the parse-time classifier's payload categories did not reach the "
        "hashed Skill node -- projection must write s.categories from the "
        "payload for a hashed skill"
    )


@pytest.mark.asyncio
async def test_hashed_skill_family_credit_where_it_previously_scored_missing(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The end-to-end claim CLASSIFIER-SPEC.md exists to fix: a JD requiring
    an in-vocab skill (``python``, curated family ``backend`` per
    ``categories.yaml``) and a résumé whose ONLY skill is an out-of-vocab
    phrase the parser hashed -- with a parse-time-assigned ``backend``
    family -- now earns stage-2 family credit instead of scoring 0.0 with
    ``reason="missing"``."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_job_requiring_python(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="bespoke internal erp customisation",
        categories=["backend"],
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 2

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)
    assert len(rows) == 1
    overall, contributions = score_skill_breakdown(rows, weights=DEFAULT_WEIGHTS)

    assert contributions[0].reason != "missing", (
        "a résumé skill sharing python's curated family must not score as "
        "a genuine miss any more"
    )
    assert contributions[0].score > 0.0
    assert overall > 0.0


@pytest.mark.asyncio
async def test_baseline_without_categories_still_scores_missing_today(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The "before" comparison point the spec asks for: the SAME scenario,
    minus the classifier's category assignment, still scores exactly as it
    did before this feature existed -- 0.0, ``reason="missing"``. Expected
    to hold both BEFORE and AFTER this slice lands (the strictly-additive
    property CLASSIFIER-SPEC.md's design section claims by construction)."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_job_requiring_python(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="bespoke internal erp customisation",
        categories=None,  # no classifier answer -- today's baseline
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 2

    rows = await _stage2_skill_rows(neo4j_driver, job_id, resume_id)
    assert len(rows) == 1
    overall, contributions = score_skill_breakdown(rows, weights=DEFAULT_WEIGHTS)

    assert contributions[0].reason == "missing"
    assert contributions[0].score == 0.0
    assert overall == 0.0


@pytest.mark.asyncio
async def test_curated_in_vocab_skill_categories_are_not_overridden_by_a_payload_value(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """The résumé side's OWN copy of an in-vocab skill (e.g. it also lists
    "python" directly) must keep its curated ``categories.yaml`` families
    even if some future/buggy producer attached a stray ``categories`` value
    to that skill's outbox row -- curated wins, per ``ensure_categories``,
    never a classifier/payload value, for an in-vocab canonical."""
    job_id = await _insert_job(pg_pool)
    resume_id = await _insert_resume(pg_pool, job_id)
    await _enqueue_resume_with_one_skill(
        pg_pool,
        resume_id=resume_id,
        job_id=job_id,
        skill_name="python",
        categories=["not_a_real_family_a_buggy_producer_sent"],
    )

    delivered = await project_to_graph(_ctx(pg_pool, neo4j_driver), batch=10)
    assert delivered == 1

    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (s:Skill {canonical_key: 'python'}) RETURN s.categories AS cats"
        )
        record = await result.single()
    assert record is not None
    assert "not_a_real_family_a_buggy_producer_sent" not in (record["cats"] or [])
    assert record["cats"], "python's curated categories must still be written"
