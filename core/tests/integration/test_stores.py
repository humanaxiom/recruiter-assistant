"""Integration tests — real Postgres + Neo4j via testcontainers.

Works offline once images are cached locally. These run in the
`integration-tests` gate and CI's integration job.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock, patch

import pytest
from neo4j import AsyncGraphDatabase
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.neo4j import Neo4jContainer
from testcontainers.postgres import PostgresContainer

from src.memory.graph import GraphMemory
from src.models.db import Base, Task, TaskStatus

FAKE_EMBEDDING = [0.1] * 768


# ── Containers (session-scoped: one boot per test session) ─────────────────

@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.fixture(scope="session")
def neo4j_container() -> Iterator[Neo4jContainer]:
    with Neo4jContainer("neo4j:5-community") as neo:
        yield neo


# ── Postgres integration ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_lifecycle_in_postgres(pg_url: str) -> None:
    engine = create_async_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        task = Task(title="Integration test task", spec="Do the thing properly")
        session.add(task)
        await session.commit()

        fetched = await session.get(Task, task.id)
        assert fetched is not None
        assert fetched.status == TaskStatus.PENDING

        fetched.status = TaskStatus.ITERATING
        fetched.branch = f"agent/{str(task.id)[:8]}-integration"
        await session.commit()

        again = await session.get(Task, task.id)
        assert again is not None
        assert again.status == TaskStatus.ITERATING
        assert again.branch is not None and again.branch.startswith("agent/")

    await engine.dispose()


# ── Neo4j integration ──────────────────────────────────────────────────────

@pytest.fixture
async def memory(neo4j_container: Neo4jContainer) -> AsyncIterator[GraphMemory]:
    driver = AsyncGraphDatabase.driver(
        neo4j_container.get_connection_url(),
        auth=("neo4j", neo4j_container.password),
    )
    mem = GraphMemory(driver=driver)
    # Embeddings mocked — no Ollama needed in CI
    with patch.object(mem, "_embed", AsyncMock(return_value=FAKE_EMBEDDING)):
        yield mem
    await mem.close()


@pytest.mark.asyncio
async def test_subtask_lineage_roundtrip(memory: GraphMemory) -> None:
    await memory.record_subtask(
        task_id="T-100",
        subtask_id="S-100-1",
        agent_id="coder",
        description="Implement rate limiter",
    )
    lineage = await memory.task_lineage("T-100")

    assert len(lineage) == 1
    assert lineage[0]["subtask"] == "S-100-1"
    assert lineage[0]["agent"] == "coder"


@pytest.mark.asyncio
async def test_artifact_stored_with_embedding(memory: GraphMemory) -> None:
    await memory.record_subtask("T-200", "S-200-1", "coder", "Auth module")
    await memory.record_artifact(
        subtask_id="S-200-1",
        artifact_id="A-200-1",
        content="def authenticate(): ...",
        kind="code",
    )
    lineage = await memory.task_lineage("T-200")
    assert "A-200-1" in lineage[0]["artifacts"]
