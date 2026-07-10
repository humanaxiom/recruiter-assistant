// 001_init.cypher — constraints + vector index
// Run via: make migrate-neo4j

CREATE CONSTRAINT task_id IF NOT EXISTS
FOR (t:Task) REQUIRE t.id IS UNIQUE;

CREATE CONSTRAINT subtask_id IF NOT EXISTS
FOR (s:Subtask) REQUIRE s.id IS UNIQUE;

CREATE CONSTRAINT agent_id IF NOT EXISTS
FOR (a:Agent) REQUIRE a.id IS UNIQUE;

CREATE CONSTRAINT artifact_id IF NOT EXISTS
FOR (ar:Artifact) REQUIRE ar.id IS UNIQUE;

// 768-dim cosine vector index for nomic-embed-text embeddings
CREATE VECTOR INDEX artifact_embeddings IF NOT EXISTS
FOR (ar:Artifact) ON (ar.embedding)
OPTIONS {indexConfig: {
  `vector.dimensions`: 768,
  `vector.similarity_function`: 'cosine'
}};
