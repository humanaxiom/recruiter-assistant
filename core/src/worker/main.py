"""arq worker — async task queue backed by Redis.

Jobs:
    run_pipeline    — full Planner→Tester→Coder(loop)→Reviewer→Security→Docs
    embed_artifact  — background embedding + Neo4j storage
    run_gates_job   — on-demand gate suite for a branch
"""

from __future__ import annotations

import logging
from typing import Any

from arq.connections import RedisSettings

from src.agents.orchestrator import Orchestrator
from src.gates.runner import run_all_gates
from src.memory.graph import GraphMemory
from src.settings import get_settings

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    ctx["memory"] = GraphMemory()
    logger.info("Worker started — memory connected")


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["memory"].close()


async def run_pipeline(
    ctx: dict[str, Any], task_id: str, task_spec: str, branch: str
) -> dict[str, Any]:
    """Full multi-agent pipeline. Enqueued by POST /tasks."""
    memory: GraphMemory = ctx["memory"]
    orchestrator = Orchestrator(branch=branch, memory=memory)
    result = await orchestrator.run(task_id, task_spec)

    return {
        "task_id": task_id,
        "success": result.success,
        "blocked_by": result.blocked_by,
        "subtasks": {
            sid: {"agent": o.agent_id, "success": o.success, "summary": o.reasoning}
            for sid, o in result.outputs.items()
        },
        "loop_iterations": (
            result.loop_outcome.iterations_used if result.loop_outcome else None
        ),
    }


async def embed_artifact(
    ctx: dict[str, Any], subtask_id: str, artifact_id: str, content: str, kind: str
) -> None:
    memory: GraphMemory = ctx["memory"]
    await memory.record_artifact(subtask_id, artifact_id, content, kind)


async def run_gates_job(ctx: dict[str, Any], branch: str) -> dict[str, Any]:
    suite = await run_all_gates(branch)
    return {
        "all_green": suite.all_green,
        "results": [
            {"name": r.name, "status": r.status.value, "duration_ms": r.duration_ms}
            for r in suite.results
        ],
    }


class WorkerSettings:
    functions = [run_pipeline, embed_artifact, run_gates_job]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
    job_timeout = 3600  # full pipelines can be long on local inference
