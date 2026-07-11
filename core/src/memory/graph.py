"""Neo4j graph memory — agent lineage graph + vector retrieval of artifacts.

Schema:
    (:Task)-[:DECOMPOSED_INTO]->(:Subtask)-[:EXECUTED_BY]->(:Agent)
    (:Subtask)-[:PRODUCED]->(:Artifact {embedding: vector})
    (:Run)-[:OF_TASK]->(:Task)
    (:Run)-[:GATE_RESULT]->(:GateResult)

Vector index `artifact_embeddings` supports semantic retrieval so agents
can ask "have we solved something like this before?".
"""

from __future__ import annotations

from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase
from openai import AsyncOpenAI

from src.settings import get_settings

# Constraints + 768-dim cosine vector index for nomic-embed-text embeddings.
# Applied idempotently on startup (IF NOT EXISTS) — there is no separate
# migration step; the graph schema comes up with the app.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
    "CREATE CONSTRAINT task_id IF NOT EXISTS " "FOR (t:Task) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT subtask_id IF NOT EXISTS "
    "FOR (s:Subtask) REQUIRE s.id IS UNIQUE",
    "CREATE CONSTRAINT agent_id IF NOT EXISTS " "FOR (a:Agent) REQUIRE a.id IS UNIQUE",
    "CREATE CONSTRAINT artifact_id IF NOT EXISTS "
    "FOR (ar:Artifact) REQUIRE ar.id IS UNIQUE",
    "CREATE VECTOR INDEX artifact_embeddings IF NOT EXISTS "
    "FOR (ar:Artifact) ON (ar.embedding) "
    "OPTIONS {indexConfig: {"
    "`vector.dimensions`: 768, `vector.similarity_function`: 'cosine'}}",
)


class GraphMemory:
    def __init__(self, driver: AsyncDriver | None = None) -> None:
        s = get_settings()
        self._driver = driver or AsyncGraphDatabase.driver(
            s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password)
        )
        self._embed_client = AsyncOpenAI(
            base_url=s.ollama_base_url, api_key="ollama"  # Ollama ignores key
        )
        self._embed_model = s.embed_model

    async def close(self) -> None:
        await self._driver.close()

    async def ensure_schema(self) -> None:
        """Create constraints + the vector index if absent (idempotent)."""
        async with self._driver.session() as session:
            for statement in _SCHEMA_STATEMENTS:
                await session.run(statement)

    # ── Write path ─────────────────────────────────────────────────────────

    async def record_subtask(
        self, task_id: str, subtask_id: str, agent_id: str, description: str
    ) -> None:
        query = """
        MERGE (t:Task {id: $task_id})
        MERGE (s:Subtask {id: $subtask_id})
        SET s.description = $description
        MERGE (a:Agent {id: $agent_id})
        MERGE (t)-[:DECOMPOSED_INTO]->(s)
        MERGE (s)-[:EXECUTED_BY]->(a)
        """
        async with self._driver.session() as session:
            await session.run(
                query,
                task_id=task_id,
                subtask_id=subtask_id,
                agent_id=agent_id,
                description=description,
            )

    async def record_artifact(
        self, subtask_id: str, artifact_id: str, content: str, kind: str
    ) -> None:
        """Store an artifact with its embedding for later semantic retrieval."""
        embedding = await self._embed(content)
        query = """
        MATCH (s:Subtask {id: $subtask_id})
        MERGE (ar:Artifact {id: $artifact_id})
        SET ar.content = $content, ar.kind = $kind, ar.embedding = $embedding
        MERGE (s)-[:PRODUCED]->(ar)
        """
        async with self._driver.session() as session:
            await session.run(
                query,
                subtask_id=subtask_id,
                artifact_id=artifact_id,
                content=content[:10_000],
                kind=kind,
                embedding=embedding,
            )

    # ── Read path ──────────────────────────────────────────────────────────

    async def similar_artifacts(
        self, query_text: str, k: int = 5
    ) -> list[dict[str, Any]]:
        """Vector search: find prior artifacts semantically similar to a query."""
        embedding = await self._embed(query_text)
        query = """
        CALL db.index.vector.queryNodes('artifact_embeddings', $k, $embedding)
        YIELD node, score
        RETURN node.id AS id, node.kind AS kind,
               left(node.content, 2000) AS content, score
        ORDER BY score DESC
        """
        async with self._driver.session() as session:
            result = await session.run(query, k=k, embedding=embedding)
            return [dict(record) async for record in result]

    async def task_lineage(self, task_id: str) -> list[dict[str, Any]]:
        """Full lineage of a task: subtasks, agents, artifacts."""
        query = """
        MATCH (t:Task {id: $task_id})-[:DECOMPOSED_INTO]->(s:Subtask)
        OPTIONAL MATCH (s)-[:EXECUTED_BY]->(a:Agent)
        OPTIONAL MATCH (s)-[:PRODUCED]->(ar:Artifact)
        RETURN s.id AS subtask, s.description AS description,
               a.id AS agent, collect(ar.id) AS artifacts
        """
        async with self._driver.session() as session:
            result = await session.run(query, task_id=task_id)
            return [dict(record) async for record in result]

    # ── Internals ──────────────────────────────────────────────────────────

    async def _embed(self, text: str) -> list[float]:
        response = await self._embed_client.embeddings.create(
            model=self._embed_model, input=text[:8_000]
        )
        return response.data[0].embedding
