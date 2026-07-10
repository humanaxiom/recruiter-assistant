"""FastAPI application — task API, gate status, memory queries."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.memory.graph import GraphMemory
from src.models.db import Task, TaskStatus
from src.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    app.state.engine = create_async_engine(s.database_url)
    app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
    app.state.arq = await create_pool(RedisSettings.from_dsn(s.redis_url))
    app.state.memory = GraphMemory()
    yield
    await app.state.memory.close()
    await app.state.arq.close()
    await app.state.engine.dispose()


app = FastAPI(title="Agent Harness API", version="2.0.0", lifespan=lifespan)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with app.state.sessionmaker() as session:
        yield session


# ── Schemas ────────────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    title: str = Field(min_length=3, max_length=255)
    spec: str = Field(min_length=10)


class TaskOut(BaseModel):
    id: uuid.UUID
    title: str
    status: TaskStatus
    branch: str | None

    model_config = {"from_attributes": True}


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks", response_model=TaskOut, status_code=201)
async def create_task(
    payload: TaskCreate, session: AsyncSession = Depends(get_session)
) -> Task:
    """Create a task, generate its agent branch, and enqueue the pipeline."""
    task = Task(title=payload.title, spec=payload.spec, status=TaskStatus.PENDING)
    slug = "-".join(payload.title.lower().split()[:4])
    session.add(task)
    await session.flush()
    task.branch = f"agent/{str(task.id)[:8]}-{slug}"
    await session.commit()

    await app.state.arq.enqueue_job(
        "run_pipeline",
        task_id=str(task.id),
        task_spec=payload.spec,
        branch=task.branch,
    )
    return task


@app.get("/tasks/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Task:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


@app.get("/tasks/{task_id}/lineage")
async def task_lineage(task_id: uuid.UUID) -> list[dict[str, Any]]:
    """Neo4j lineage: subtasks, agents, artifacts for a task."""
    return await app.state.memory.task_lineage(str(task_id))


@app.get("/memory/similar")
async def similar(q: str, k: int = 5) -> list[dict[str, Any]]:
    """Vector search over prior artifacts."""
    return await app.state.memory.similar_artifacts(q, k=k)


@app.post("/gates/run")
async def run_gates(branch: str) -> dict[str, str]:
    job = await app.state.arq.enqueue_job("run_gates_job", branch=branch)
    return {"job_id": job.job_id}
