"""Integration tests — Neo4j bootstrap against a real Neo4j (testcontainers).

Proves for real what ``tests/unit/test_neo4j_bootstrap.py`` proves by string
matching: the bootstrap is re-runnable, the 4 vector indexes land at
``llm_embedding_dim`` / cosine, and ``ResumeChunk`` carries NO uniqueness
constraint (the shortlist-caps-at-one-candidate regression).

Runs in CI (`make gates-integration`), not in the offline gate suite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from neo4j import AsyncDriver, AsyncGraphDatabase
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema
from testcontainers.neo4j import Neo4jContainer

from src.settings import get_settings

VECTOR_INDEXES: frozenset[str] = frozenset(
    {"resume_summary_idx", "job_summary_idx", "skill_emb_idx", "chunk_emb_idx"}
)

EXPECTED_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "job_id_unique",
        "resume_id_unique",
        "skill_name_unique",
        "company_name_unique",
        "institution_name_unique",
    }
)


@pytest.fixture(scope="session")
def neo4j_container() -> Iterator[Neo4jContainer]:
    with Neo4jContainer("neo4j:5-community") as container:
        yield container


@pytest.fixture
async def driver(neo4j_container: Neo4jContainer) -> AsyncIterator[AsyncDriver]:
    neo4j_driver = AsyncGraphDatabase.driver(
        neo4j_container.get_connection_url(),
        auth=("neo4j", neo4j_container.password),
    )
    # Bootstrap twice — idempotency proven against a live server, not a regex.
    await bootstrap_neo4j_schema(neo4j_driver)
    await bootstrap_neo4j_schema(neo4j_driver)
    try:
        yield neo4j_driver
    finally:
        await neo4j_driver.close()


async def _show(driver: AsyncDriver, what: str) -> list[dict[str, Any]]:
    async with driver.session() as session:
        result = await session.run(f"SHOW {what}")
        return [dict(record) async for record in result]


# ── Vector indexes: the 768-d contract, live ───────────────────────────────


@pytest.mark.asyncio
async def test_the_four_vector_indexes_exist(driver: AsyncDriver) -> None:
    indexes = await _show(driver, "INDEXES")
    vector_names = {i["name"] for i in indexes if i["type"] == "VECTOR"}
    assert vector_names == VECTOR_INDEXES


@pytest.mark.asyncio
async def test_vector_indexes_are_768d_cosine(driver: AsyncDriver) -> None:
    expected_dim = get_settings().llm_embedding_dim
    indexes = [i for i in await _show(driver, "INDEXES") if i["type"] == "VECTOR"]
    assert len(indexes) == 4

    for index in indexes:
        config = index["options"]["indexConfig"]
        assert int(config["vector.dimensions"]) == expected_dim, index["name"]
        assert config["vector.similarity_function"].lower() == "cosine", index["name"]


@pytest.mark.asyncio
async def test_all_indexes_come_online(driver: AsyncDriver) -> None:
    indexes = await _show(driver, "INDEXES")
    failed = [i["name"] for i in indexes if i.get("state") == "FAILED"]
    assert failed == []


@pytest.mark.asyncio
async def test_scoping_and_chunk_lookup_indexes_exist(driver: AsyncDriver) -> None:
    names = {i["name"] for i in await _show(driver, "INDEXES")}
    assert "resume_job_id_idx" in names  # stops cross-job shortlist leakage
    assert "chunk_resume_id_idx" in names


# ── Constraints: the ResumeChunk collision regression, live ────────────────


@pytest.mark.asyncio
async def test_expected_uniqueness_constraints_exist(driver: AsyncDriver) -> None:
    names = {c["name"] for c in await _show(driver, "CONSTRAINTS")}
    assert EXPECTED_CONSTRAINTS <= names


@pytest.mark.asyncio
async def test_no_constraint_on_resumechunk(driver: AsyncDriver) -> None:
    """Chunk ids collide across resumes by design — a unique constraint here
    silently caps every shortlist at one candidate."""
    constraints = await _show(driver, "CONSTRAINTS")
    assert "chunk_id_unique" not in {c["name"] for c in constraints}
    assert [
        c["name"] for c in constraints if "ResumeChunk" in (c["labelsOrTypes"] or [])
    ] == []


@pytest.mark.asyncio
async def test_colliding_chunk_ids_from_two_resumes_both_persist(
    driver: AsyncDriver,
) -> None:
    """The actual failure mode the dropped constraint used to cause."""
    async with driver.session() as session:
        await session.run("MATCH (c:ResumeChunk) WHERE c.id = 'c_001' DETACH DELETE c")
        await session.run("""
            MERGE (a:ResumeChunk {resume_id: 'r-1', id: 'c_001'})
            MERGE (b:ResumeChunk {resume_id: 'r-2', id: 'c_001'})
            """)
        result = await session.run(
            "MATCH (c:ResumeChunk) WHERE c.id = 'c_001' RETURN count(c) AS n"
        )
        record = await result.single()

    assert record is not None
    assert record["n"] == 2
