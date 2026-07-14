"""THE GRAPH-WIDE PII SWEEP — the merge-blocking artifact for Phase 4b.

Seeds a ``resumes`` row whose ``parsed`` jsonb holds a header chunk carrying
a synthetic email/phone/name (from the reviewed corpus allowlist —
``casey.rivera@example.test`` / ``555-0101`` / ``Casey Rivera``, the same
literals ``core/tests/evals/fixtures/resumes/r01_casey_rivera.json`` uses),
enqueues the REAL identity-free outbox payload (built via
``ResumeParsed.model_dump(exclude=_OUTBOX_PARSED_EXCLUDE)`` — the SAME named
constant ``test_evals_outbox_fixture.py`` pins, not a hand-rolled re-
implementation of the exclude clause), drains it through the real
``project_to_graph``, and then walks EVERY NODE AND EVERY RELATIONSHIP
PROPERTY IN THE WHOLE DATABASE for the three markers.

A per-field check ("does the Resume node's ``total_years_experience``
contain the email?") is not good enough — R1's whole point is that
``text_preview``/chunk text/whatever future field could carry it, so the
guard has to be structurally blind to WHICH property, walking the graph the
same way a real PIPEDA/FIPPA audit would.

Real Postgres + real Neo4j via testcontainers. LLM/embedder are mocked (no
Ollama in gates — see CLAUDE.md); the embedder returns deterministic 768-d
vectors so the real ``vector.dimensions`` Neo4j contract is respected.

None of ``src.worker.graph_tasks`` / ``src.worker.resume_tasks.project_resume``
/ ``src.pipeline.skills_graph`` exist yet — this file fails at collection
(``ImportError``). RED half of the TDD cycle.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.models.ddl import init_schema
from src.schemas.resumes import ResumeParsed
from src.services import outbox_service
from src.settings import get_settings
from src.worker.graph_tasks import project_to_graph
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema
from src.worker.resume_tasks import _OUTBOX_PARSED_EXCLUDE

CANDIDATE_NAME = "Casey Rivera"
CANDIDATE_EMAIL = "casey.rivera@example.test"
CANDIDATE_PHONE = "555-0101"
HEADER_CHUNK_TEXT = (
    f"{CANDIDATE_NAME}\n{CANDIDATE_EMAIL} | {CANDIDATE_PHONE}\nVancouver, BC"
)

_DIM = get_settings().llm_embedding_dim


def _vec(seed: int) -> list[float]:
    return [((i * 13 + seed * 7) % 971) / 990.0 + 0.01 for i in range(_DIM)]


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
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=1, max_size=4)
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


def _make_llm() -> MagicMock:
    out = MagicMock(match=None)
    return MagicMock(chat_json=AsyncMock(return_value=out))


def _make_embedder() -> MagicMock:
    async def _embed(texts: Any) -> list[list[float]]:
        return [_vec(len(t)) for t in texts]

    return MagicMock(embed=AsyncMock(side_effect=_embed))


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ($1, $2) RETURNING id",
            "Senior Backend Engineer",
            "Build and operate REST APIs and data pipelines.",
        )
    return job_id


async def _insert_resume_with_pii_header_chunk(
    pool: asyncpg.Pool, job_id: uuid.UUID
) -> tuple[uuid.UUID, ResumeParsed]:
    parsed = ResumeParsed.model_validate(
        {
            "candidate": {
                "name": CANDIDATE_NAME,
                "email": CANDIDATE_EMAIL,
                "phone": CANDIDATE_PHONE,
                "location": "Vancouver, BC",
            },
            "summary": (
                f"Backend engineer {CANDIDATE_NAME} with 6 years of "
                "Python experience."
            ),
            "total_years_experience": 6,
            "skills": [{"name": "python", "years": 6, "last_used_year": 2026}],
            "experience": [
                {
                    "company": "Nimbus Analytics Inc",
                    "title": "Senior Backend Engineer",
                    "start": "2022-01",
                    "is_current": True,
                    "bullets": [{"text": "Shipped REST APIs.", "chunk_id": "c_001"}],
                }
            ],
            "education": [],
            "chunks": [
                {
                    "id": "c_001",
                    "section": "header",
                    "page": 1,
                    # The canonical R1 leak shape: a résumé header chunk
                    # carrying the candidate's contact block verbatim.
                    "text": HEADER_CHUNK_TEXT,
                }
            ],
            "cover_letter_chunks": [],
        }
    )
    async with pool.acquire() as conn:
        resume_id: uuid.UUID = await conn.fetchval(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type,
                file_size_bytes, sha256, consent_acknowledged, status, parsed
            ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3, TRUE,
                      'parsed', $4::jsonb)
            RETURNING id
            """,
            job_id,
            f"resumes/{uuid.uuid4().hex}.pdf",
            uuid.uuid4().hex,
            json.dumps(parsed.model_dump()),
        )
    return resume_id, parsed


async def _enqueue_real_outbox_payload(
    pool: asyncpg.Pool, *, resume_id: uuid.UUID, job_id: uuid.UUID, parsed: ResumeParsed
) -> None:
    """The REAL identity-free payload — built the same way
    ``src.worker.resume_tasks.parse_resume`` builds it, via the shared
    ``_OUTBOX_PARSED_EXCLUDE`` constant (not a hand-rolled re-implementation
    of the exclude clause)."""
    outbox_payload = {
        "parsed": parsed.model_dump(exclude=_OUTBOX_PARSED_EXCLUDE),
        "summary_emb": _vec(1),
        "chunk_embs": {c.id: _vec(hash(c.id) % 97) for c in parsed.chunks},
        "prompt_version": "resume_core_v1+resume_skills_v2",
        "job_id": str(job_id),
    }
    async with pool.acquire() as conn:
        await outbox_service.enqueue_outbox(
            conn,
            aggregate="resume",
            aggregate_id=resume_id,
            event_type="resume.parsed",
            payload=outbox_payload,
        )


async def _walk_every_property_value(driver: AsyncDriver) -> list[str]:
    """Every node property AND every relationship property, in the WHOLE
    database, stringified. Structurally blind to which label/property
    carries a leak — that's the point."""
    values: list[str] = []
    async with driver.session() as session:
        node_result = await session.run("MATCH (n) RETURN properties(n) AS props")
        async for record in node_result:
            values.extend(str(v) for v in record["props"].values())
        rel_result = await session.run("MATCH ()-[r]->() RETURN properties(r) AS props")
        async for record in rel_result:
            values.extend(str(v) for v in record["props"].values())
    return values


@pytest.mark.asyncio
async def test_no_pii_marker_survives_anywhere_in_the_projected_graph(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    job_id = await _insert_job(pg_pool)
    resume_id, parsed = await _insert_resume_with_pii_header_chunk(pg_pool, job_id)
    await _enqueue_real_outbox_payload(
        pg_pool, resume_id=resume_id, job_id=job_id, parsed=parsed
    )

    ctx = {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }
    delivered = await project_to_graph(ctx, batch=10)
    assert delivered == 1

    all_values = " \n ".join(await _walk_every_property_value(neo4j_driver))

    assert CANDIDATE_EMAIL not in all_values, (
        "candidate email survived somewhere in the projected graph (R1 — the "
        "PII sweep is structurally blind to which node/property carried it)"
    )
    assert (
        CANDIDATE_PHONE not in all_values
    ), "candidate phone survived somewhere in the projected graph (R1)"
    assert (
        CANDIDATE_NAME not in all_values
    ), "candidate name survived somewhere in the projected graph (R1)"
    # Belt-and-braces: the header chunk's RAW text must not survive either,
    # even a truncated preview of it (decision 1 — no chunk text, ever).
    assert HEADER_CHUNK_TEXT not in all_values


@pytest.mark.asyncio
async def test_the_resume_node_and_chunk_carry_no_text_preview_property(
    pg_pool: asyncpg.Pool, neo4j_driver: AsyncDriver
) -> None:
    """Belt-and-braces alongside the sweep above: pins decision 1's exact
    shape (no ``text_preview`` KEY at all) so a future re-introduction of a
    "harmless-looking preview" is caught even before it could carry PII."""
    job_id = await _insert_job(pg_pool)
    resume_id, parsed = await _insert_resume_with_pii_header_chunk(pg_pool, job_id)
    await _enqueue_real_outbox_payload(
        pg_pool, resume_id=resume_id, job_id=job_id, parsed=parsed
    )
    ctx = {
        "pg_pool": pg_pool,
        "neo4j": neo4j_driver,
        "llm": _make_llm(),
        "embedder": _make_embedder(),
    }
    await project_to_graph(ctx, batch=10)

    async with neo4j_driver.session() as session:
        result = await session.run("MATCH (c:ResumeChunk) RETURN keys(c) AS ks")
        rows = [record["ks"] async for record in result]
    assert rows, "expected at least one ResumeChunk node"
    for keys in rows:
        assert "text_preview" not in keys
        assert "preview" not in keys
        assert "text" not in keys
