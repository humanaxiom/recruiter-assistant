"""Central configuration — the single source of truth, loaded from env.

Nothing else in the codebase may read ``os.environ``. Two fields carry
contracts that other modules assert against:

* ``llm_embedding_dim`` — the Neo4j vector indexes are built from this number
  (see ``src/worker/neo4j_bootstrap.py``); the two must never drift apart.
* ``llm_base_url`` — must always point at a local, OpenAI-compatible endpoint
  (Ollama on the host). This project is offline by design: no cloud inference.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Postgres (transactional store; raw asyncpg DSN, not a SQLAlchemy URL) ─
    postgres_dsn: str = "postgresql://app:app@postgres:5432/recruiter"
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10

    # ── Neo4j (skill/experience graph + vector retrieval) ────────────────────
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "recruiterpass"

    # ── Redis (arq broker + embedding cache) ─────────────────────────────────
    redis_url: str = "redis://redis:6379/0"

    # ── Inference (Ollama on metal, OpenAI-compatible /v1 — never a cloud host)
    llm_base_url: str = "http://host.docker.internal:11434/v1"
    llm_model_generation: str = "gpt-oss:20b"
    llm_model_embedding: str = "nomic-embed-text"
    llm_embedding_dim: int = 768  # CONTRACT: == Neo4j `vector.dimensions`
    llm_timeout_s: float = 120.0
    llm_max_retries: int = 2
    llm_breaker_threshold: int = 10  # consecutive failures before the breaker opens
    llm_breaker_cooldown_s: float = 30.0
    # Ollama's OpenAI-compat layer only intermittently honours `think: false`, so a
    # reasoning model (the default gpt-oss:20b is one) can burn its whole token budget
    # on a discarded reasoning trace and return empty content. The native /api/chat
    # route honours it reliably — flip this on if JSON-mode parses come back empty.
    llm_ollama_native: bool = False
    debug_llm: bool = False  # MUST stay False by default — prompts carry PII

    # ── Embedding cache (Redis read-through) ─────────────────────────────────
    embedding_cache_ttl_s: int = 60 * 60 * 24 * 90  # 90 days

    # ── Storage (filesystem BlobStore root — no MinIO/S3) ────────────────────
    storage_dir: str = "/data"

    # ── Privacy ──────────────────────────────────────────────────────────────
    pii_key: str = ""  # env-supplied pgcrypto key for the app.pii_key GUC
    blind_review_default: bool = True  # decision 4 — redaction ON by default

    # ── Gates ────────────────────────────────────────────────────────────────
    coverage_threshold: int = 80

    # ── Flask viewer ─────────────────────────────────────────────────────────
    api_base_url: str = "http://api:8000"
    flask_secret_key: str = "dev-only"

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
