"""Idempotently create the Neo4j constraints + vector indexes the ranking
pipeline needs. Ported near-verbatim from hris
``apps/worker/src/worker/neo4j_bootstrap.py``.

Run from the worker's ``on_startup`` and the API lifespan. Cheap to re-execute:
every statement is ``IF NOT EXISTS`` / ``IF EXISTS``.

Two things here are load-bearing and must not be "tidied":

1. **``ResumeChunk`` carries NO uniqueness constraint on ``id``.** Chunk ids
   (``c_001``, ``c_002``, …) are deterministic per-resume and intentionally
   COLLIDE across resumes. The old ``chunk_id_unique`` meant the second
   resume's projection always failed, which silently capped every shortlist at
   one candidate. It is actively DROPPED here so existing installs heal on the
   next restart; a plain composite index on ``(resume_id, id)`` keeps citation
   lookups fast without forcing global uniqueness.
2. **``resume_job_id_idx``** lets the stage-1 vector query scope to "resumes
   uploaded for THIS job". Without it, other jobs' candidates leak into a
   recruiter's shortlist.

The vector dimension is read from ``settings.llm_embedding_dim`` — the single
source of the 768-d contract. Change the embedding model and this moves with
it; the two can never drift apart.
"""

from __future__ import annotations

import logging
from typing import Final

from neo4j import AsyncDriver

from src.settings import get_settings

logger = logging.getLogger(__name__)

# CONTRACT: one number, one place. nomic-embed-text → 768-d, cosine.
_DIM: Final[int] = get_settings().llm_embedding_dim

_VECTOR_OPTIONS: Final[str] = (
    "OPTIONS { indexConfig: { "
    f"`vector.dimensions`: {_DIM}, "
    "`vector.similarity_function`: 'cosine' } }"
)


def _vector_index(name: str, var: str, label: str, prop: str) -> str:
    return (
        f"CREATE VECTOR INDEX {name} IF NOT EXISTS "
        f"FOR ({var}:{label}) ON {var}.{prop} "
        f"{_VECTOR_OPTIONS}"
    )


_STATEMENTS: tuple[str, ...] = (
    # ── Uniqueness constraints ───────────────────────────────────────────────
    "CREATE CONSTRAINT job_id_unique IF NOT EXISTS "
    "FOR (j:Job) REQUIRE j.id IS UNIQUE",
    "CREATE CONSTRAINT resume_id_unique IF NOT EXISTS "
    "FOR (r:Resume) REQUIRE r.id IS UNIQUE",
    # ADR-008: the unique key is `canonical_key` (a vocab term, cleartext, or
    # a salted hash for a non-vocab name) — never `canonical_name`, which
    # would tempt someone to write a résumé-derived name into it.
    "CREATE CONSTRAINT skill_name_unique IF NOT EXISTS "
    "FOR (s:Skill) REQUIRE s.canonical_key IS UNIQUE",
    "CREATE CONSTRAINT company_name_unique IF NOT EXISTS "
    "FOR (c:Company) REQUIRE c.canonical_name IS UNIQUE",
    "CREATE CONSTRAINT institution_name_unique IF NOT EXISTS "
    "FOR (i:Institution) REQUIRE i.canonical_name IS UNIQUE",
    # ── The chunk-collision fix (see module docstring) ───────────────────────
    "DROP CONSTRAINT chunk_id_unique IF EXISTS",
    "CREATE INDEX chunk_resume_id_idx IF NOT EXISTS "
    "FOR (c:ResumeChunk) ON (c.resume_id, c.id)",
    # ── Per-job shortlist scoping ────────────────────────────────────────────
    "CREATE INDEX resume_job_id_idx IF NOT EXISTS FOR (r:Resume) ON r.job_id",
    # ── Vector indexes (llm_embedding_dim, cosine) ───────────────────────────
    _vector_index("resume_summary_idx", "r", "Resume", "summary_embedding"),
    _vector_index("job_summary_idx", "j", "Job", "summary_embedding"),
    _vector_index("skill_emb_idx", "s", "Skill", "embedding"),
    _vector_index("chunk_emb_idx", "c", "ResumeChunk", "embedding"),
)


async def bootstrap_neo4j_schema(driver: AsyncDriver) -> None:
    """Run all constraint + index DDL. Idempotent."""
    async with driver.session() as session:
        for statement in _STATEMENTS:
            await session.run(statement)
    logger.info("neo4j.bootstrap.ok statements=%s dim=%s", len(_STATEMENTS), _DIM)
