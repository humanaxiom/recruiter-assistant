"""arq worker — async task queue backed by Redis.

Phase 0 wires the infrastructure only: on startup the worker opens its own
asyncpg pool, applies the idempotent Postgres DDL, and bootstraps the Neo4j
constraints + vector indexes. The ranking jobs (parse, project, rank) land in
Phases 3-4.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from arq.connections import RedisSettings
from neo4j import AsyncGraphDatabase

from src.models.ddl import init_schema
from src.settings import get_settings
from src.storage.blob_store import BlobStore
from src.worker.neo4j_bootstrap import bootstrap_neo4j_schema

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    s = get_settings()
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

    # The worker builds its own store (not shared with the API); parse/project
    # tasks in Phases 3-4 read it off the ctx.
    ctx["blob_store"] = BlobStore(s.storage_dir)

    logger.info("worker.startup.ok")


async def shutdown(ctx: dict[str, Any]) -> None:
    driver = ctx.get("neo4j")
    if driver is not None:
        await driver.close()
    pool = ctx.get("pg_pool")
    if pool is not None:
        await pool.close()


class WorkerSettings:
    functions: list[Any] = []  # ranking jobs arrive in Phases 3-4
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
    job_timeout = 3600  # local inference is slow; ranking a batch takes a while
