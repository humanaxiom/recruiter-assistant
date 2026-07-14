"""arq worker — async task queue backed by Redis.

On startup the worker opens its own asyncpg pool, applies the idempotent
Postgres DDL, bootstraps the Neo4j constraints + vector indexes, and builds the
per-process singletons the parse tasks resolve off ``ctx``:

===================  =========================================================
``ctx`` key          value
===================  =========================================================
``pg_pool``          ``asyncpg.Pool``  (NOT ``pool`` — hris's key; do not drift)
``neo4j``            ``neo4j.AsyncDriver``
``blob_store``       ``src.storage.blob_store.BlobStore`` (no MinIO anywhere)
``llm``              ``src.pipeline.llm.LLMClient`` (local Ollama, /v1)
``redis``            ``redis.asyncio.Redis`` — backs the embedding cache
``embedder``         ``src.pipeline.llm.CachedEmbedder``
===================  =========================================================

Phase 3 registers the two parse tasks (``WorkerSettings.functions``). Phase 4b
adds the graph-projection drainer (``src.worker.graph_tasks.project_to_graph``)
as a ``WorkerSettings.cron_jobs`` entry rather than a ``functions`` entry: it
is never enqueued by name (nothing calls ``arq.enqueue_job("project_to_graph")``
— it runs strictly on its own schedule), and arq dispatches cron jobs
independently of the ``functions`` registry. Keeping it out of ``functions``
also means ``WorkerSettings.functions == [parse_job, parse_resume]`` stays a
correct, stable assertion across phases (see ``test_worker_wiring.py``, a
Phase-3 test this phase does not need to — and per CLAUDE.md's "never modify a
test to make implementation pass" must not — touch).
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from arq import cron
from arq.connections import RedisSettings
from neo4j import AsyncGraphDatabase

from src.models.ddl import init_schema
from src.pipeline.llm import CachedEmbedder, LLMClient
from src.settings import get_settings
from src.storage.blob_store import BlobStore
from src.worker.graph_tasks import project_to_graph
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema
from src.worker.resume_tasks import parse_resume
from src.worker.tasks import parse_job

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    s = get_settings()
    # Fail loud on a misconfigured deploy BEFORE opening any pool/driver/store:
    # pii.set_pii_key binds settings.pii_key (default "") into the app.pii_key
    # GUC unconditionally, so an unset PII_KEY would silently pgp_sym_encrypt
    # every resume's PII with an EMPTY passphrase (weak-key ciphertext) instead
    # of refusing to start.
    if not s.pii_key:
        raise RuntimeError(
            "PII_KEY is empty — refusing to start the worker; pgcrypto would "
            "encrypt candidate PII with an empty passphrase. Set PII_KEY."
        )
    pool = await asyncpg.create_pool(
        dsn=s.postgres_dsn,
        min_size=s.postgres_pool_min,
        max_size=s.postgres_pool_max,
        command_timeout=30,
    )
    await init_schema(pool)
    ctx["pg_pool"] = pool

    driver = AsyncGraphDatabase.driver(
        s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password)
    )
    await bootstrap_neo4j_schema(driver)
    ctx["neo4j"] = driver

    # The worker builds its own store (not shared with the API); the parse tasks
    # read it off the ctx.
    ctx["blob_store"] = BlobStore(s.storage_dir)

    # Every knob comes from settings — the client is the ONLY egress, and it
    # points at the local Ollama /v1 endpoint. No cloud inference, ever.
    llm = LLMClient(
        s.llm_base_url,
        s.llm_model_generation,
        s.llm_model_embedding,
        timeout_s=s.llm_timeout_s,
        max_retries=s.llm_max_retries,
        breaker_threshold=s.llm_breaker_threshold,
        breaker_cooldown_s=s.llm_breaker_cooldown_s,
        debug_llm=s.debug_llm,
        native_chat=s.llm_ollama_native,
        # The 768-d contract, enforced at the source: the same settings value
        # bootstrap_neo4j_schema builds the vector indexes from. A model that
        # returns a different width now fails at embed() instead of surfacing
        # as an opaque Neo4j write error in Phase 4.
        expected_dim=s.llm_embedding_dim,
    )
    ctx["llm"] = llm

    # Same Redis instance as the arq broker, different key space (``emb:v1:*``).
    redis_client: aioredis.Redis[bytes] = aioredis.from_url(s.redis_url)
    ctx["redis"] = redis_client
    ctx["embedder"] = CachedEmbedder(
        llm,
        redis_client,
        s.llm_model_embedding,
        ttl_s=s.embedding_cache_ttl_s,
    )

    logger.info("worker.startup.ok")


async def shutdown(ctx: dict[str, Any]) -> None:
    llm = ctx.get("llm")
    if llm is not None:
        await llm.aclose()
    redis_client = ctx.get("redis")
    if redis_client is not None:
        await redis_client.aclose()
    driver = ctx.get("neo4j")
    if driver is not None:
        await driver.close()
    pool = ctx.get("pg_pool")
    if pool is not None:
        await pool.close()


class WorkerSettings:
    # Phase 3: parse tasks, enqueued by id (parse_job(ctx, job_id_str) etc).
    functions: list[Any] = [parse_job, parse_resume]
    # Phase 4b: the graph-projection drainer runs on its own schedule, not by
    # enqueue — every ~5s, matching decision 3's rationale (skill resolution
    # must be cheap enough to finish well inside one cron tick).
    cron_jobs = [cron(project_to_graph, second=set(range(0, 60, 5)))]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
    job_timeout = 3600  # local inference is slow; ranking a batch takes a while
