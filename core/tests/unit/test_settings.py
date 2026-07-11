"""Unit tests for Settings — the single source of configuration truth.

Guards the two invariants that the rest of the port hangs off:

* the **768-d contract** (``llm_embedding_dim`` is the one number that the
  Neo4j vector indexes must agree with — see ``test_neo4j_bootstrap.py``),
* the **offline invariant** (``llm_base_url`` must never point at a cloud
  inference endpoint).
"""

from __future__ import annotations

import pytest
from pytest import MonkeyPatch

from src.settings import Settings, get_settings

# Hosts that would break the offline guarantee if they ever appeared in config.
CLOUD_HOSTS: tuple[str, ...] = (
    "api.openai.com",
    "openai.azure.com",
    "anthropic.com",
    "azure.com",
    "googleapis.com",
    "amazonaws.com",
    "cohere.ai",
    "mistral.ai",
    "huggingface.co",
    "together.xyz",
    "groq.com",
)

LOCAL_HOSTS: tuple[str, ...] = ("localhost", "127.0.0.1", "host.docker.internal")


# ── Defaults ───────────────────────────────────────────────────────────────


def test_postgres_defaults() -> None:
    s = Settings()
    assert s.postgres_dsn == "postgresql://app:app@postgres:5432/recruiter"
    assert s.postgres_pool_min == 2
    assert s.postgres_pool_max == 10


def test_postgres_dsn_is_raw_asyncpg_dsn_not_sqlalchemy_url() -> None:
    """asyncpg.create_pool rejects SQLAlchemy's ``postgresql+asyncpg://``."""
    dsn = Settings().postgres_dsn
    assert dsn.startswith("postgresql://")
    assert "+asyncpg" not in dsn
    assert "+psycopg" not in dsn


def test_neo4j_and_redis_defaults() -> None:
    s = Settings()
    assert s.neo4j_uri == "bolt://neo4j:7687"
    assert s.neo4j_user == "neo4j"
    assert s.neo4j_password == "recruiterpass"
    assert s.redis_url == "redis://redis:6379/0"


def test_llm_defaults() -> None:
    s = Settings()
    assert s.llm_base_url == "http://host.docker.internal:11434/v1"
    assert s.llm_model_generation == "gpt-oss:20b"
    assert s.llm_model_embedding == "nomic-embed-text"
    assert s.llm_timeout_s == 120.0


def test_embedding_dim_is_768() -> None:
    """CONTRACT: must equal the Neo4j vector-index ``vector.dimensions``."""
    assert Settings().llm_embedding_dim == 768


def test_blind_review_default_is_true() -> None:
    """Decision 4 — redaction is ON unless a recruiter opts out."""
    assert Settings().blind_review_default is True


def test_storage_and_pii_defaults() -> None:
    s = Settings()
    assert s.storage_dir == "/data"
    assert s.pii_key == ""  # env-supplied; empty default forces explicit config
    assert s.coverage_threshold == 80


def test_frontend_defaults() -> None:
    s = Settings()
    assert s.api_base_url == "http://api:8000"
    assert s.flask_secret_key == "dev-only"


# ── Offline invariant ──────────────────────────────────────────────────────


@pytest.mark.parametrize("cloud_host", CLOUD_HOSTS)
def test_llm_base_url_is_not_a_cloud_endpoint(cloud_host: str) -> None:
    assert cloud_host not in Settings().llm_base_url.lower()


def test_llm_base_url_points_at_a_local_ollama() -> None:
    url = Settings().llm_base_url.lower()
    assert any(host in url for host in LOCAL_HOSTS)
    assert url.startswith("http://")  # no TLS needed on loopback
    assert url.endswith("/v1")  # OpenAI-compatible surface


# ── Demo fields dropped ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dropped", ["agent_model", "database_url", "max_review_iterations"]
)
def test_template_demo_fields_are_gone(dropped: str) -> None:
    assert dropped not in Settings.model_fields


# ── Env overrides + caching ────────────────────────────────────────────────


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_env_override_embedding_dim(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_EMBEDDING_DIM", "1024")
    assert Settings().llm_embedding_dim == 1024


def test_env_override_blind_review_default(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("BLIND_REVIEW_DEFAULT", "false")
    assert Settings().blind_review_default is False


def test_env_override_pii_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("PII_KEY", "c2VjcmV0LWtleS0zMi1ieXRlcy1iYXNlNjQ=")
    assert Settings().pii_key == "c2VjcmV0LWtleS0zMi1ieXRlcy1iYXNlNjQ="


def test_env_override_postgres_dsn(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@localhost:5433/other")
    assert Settings().postgres_dsn == "postgresql://u:p@localhost:5433/other"
