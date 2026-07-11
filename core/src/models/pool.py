"""asyncpg connection-pool lifecycle + the per-request connection dependency.

The pool is process-global: one per uvicorn worker, parked on ``app.state``.
Each request takes a single connection for its duration; transactions are
scoped by the caller (``async with conn.transaction(): ...``).

Ported from hris ``apps/api/src/api/db/session.py``. Raw asyncpg — the DSN in
settings is a plain ``postgresql://`` URL, never SQLAlchemy's
``postgresql+asyncpg://`` form, which ``asyncpg.create_pool`` cannot parse.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Annotated

import asyncpg
from fastapi import Depends, FastAPI, Request

from src.settings import Settings, get_settings

logger = logging.getLogger(__name__)


async def init_pool(app: FastAPI, settings: Settings | None = None) -> asyncpg.Pool:
    """Create the asyncpg pool and attach it to ``app.state.pg_pool``."""
    s = settings or get_settings()
    pool = await asyncpg.create_pool(
        dsn=s.postgres_dsn,
        min_size=s.postgres_pool_min,
        max_size=s.postgres_pool_max,
        command_timeout=30,
    )
    app.state.pg_pool = pool
    logger.info("pg.pool.init min=%s max=%s", s.postgres_pool_min, s.postgres_pool_max)
    return pool


async def close_pool(app: FastAPI) -> None:
    """Close the pool if it was ever opened. A no-op otherwise."""
    pool: asyncpg.Pool | None = getattr(app.state, "pg_pool", None)
    if pool is not None:
        await pool.close()
        app.state.pg_pool = None
        logger.info("pg.pool.close")


async def get_db(request: Request) -> AsyncIterator[asyncpg.Connection]:
    """FastAPI dep — one connection per request, released back to the pool."""
    pool: asyncpg.Pool | None = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise RuntimeError("Postgres pool not initialised (lifespan not running)")
    async with pool.acquire() as conn:
        yield conn


Db = Annotated[asyncpg.Connection, Depends(get_db)]
