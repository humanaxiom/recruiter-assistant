"""Skill normalisation — the Neo4j-backed half, deferred out of Phase 3.

Ported behaviourally from hris ``apps/worker/src/worker/skill_normalize.py``
(the vector-match / LLM-tiebreaker / Skill-graph half hris's ``normalize_skill``
covers), with THREE deliberate, human-locked deviations from
``docs/EXTRACTION_PLAN.md`` (4b row):

* **Decision 3 — resolution runs OUTSIDE any Neo4j write transaction.** hris's
  ``_resolve_canonical`` takes a managed ``tx`` and calls ``embedder.embed`` /
  ``llm.chat_json`` from inside it — up to 40 sequential local-model round
  trips held under one write lock, against a 5s cron. Here,
  ``resolve_canonical_names`` takes a plain Neo4j ``session`` and issues every
  Cypher statement via auto-commit ``session.run(...)`` — never
  ``session.execute_write``/``execute_read``. Callers resolve every skill name
  BEFORE opening a write transaction, then pass only the resolved
  ``{raw_name: canonical_name}`` mapping into the write callback.
* **Decision 4 — the LLM tiebreaker's answer is constrained to the ``near``
  candidate set.** hris's Cypher ``MATCH (s:Skill {canonical_name: $c})``
  against a hallucinated ``$c`` matches nothing and is a silent no-op — the
  skill's edge never gets created, no error, no log (R5). Here, a tiebreaker
  answer that is not one of the offered ``near`` candidates' own names is
  rejected outright and treated exactly like "no match": fall through to
  create-new, so a real Skill node backing the returned name always exists.
* **Thresholds are settings, not hard-coded module constants** — hris's
  ``AUTO_MERGE_THRESHOLD = 0.92`` / ``TIEBREAKER_THRESHOLD = 0.88`` become
  ``settings.skill_auto_merge_threshold`` / ``settings.skill_tiebreaker_threshold``,
  read fresh on every call (never cached at import time) so a settings
  override actually changes behaviour.

Also folds in two hris hardening findings:

* **R4 — ``categories.yaml`` ships inside the Docker build context.**
  ``core/src/pipeline/skill_data/categories.yaml``, beside the existing
  ``aliases.yaml`` — a repo-root ``infra/skills/`` path (hris's resolution) is
  invisible to the ``./core`` build context. A missing file degrades
  gracefully to ``{}``/``[]``, never crashes.
* **R6 — alias updates are dedupe-safe.** hris's
  ``SET s.aliases = coalesce(s.aliases, []) + [$alias]`` never de-dupes, so a
  re-parse appends the same alias forever. The Cypher here guards with a
  ``CASE WHEN $alias IN coalesce(s.aliases, [])`` check.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from src.settings import get_settings

log = logging.getLogger(__name__)

_ALIASES_PATH = Path(__file__).resolve().parent / "skill_data" / "aliases.yaml"
_CATEGORIES_PATH = Path(__file__).resolve().parent / "skill_data" / "categories.yaml"

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w.+#\- ]+")

# Vector recall breadth for the near-candidate query — matches hris's
# `db.index.vector.queryNodes('skill_emb_idx', 5, ...)`.
_NEAR_CANDIDATE_LIMIT = 5


# ---------------- basic normalisation (mirrors src.pipeline.skills) ---------
#
# Duplicated (not imported) rather than shared: `src.pipeline.skills` is the
# Neo4j-free slice Phase 3 already ships and must stay import-clean of this
# module's Neo4j/LLM/embedder dependencies. The alias table lives in the SAME
# ``aliases.yaml``, so the two never disagree on what's canonical.


@lru_cache(maxsize=1)
def _alias_table() -> dict[str, str]:
    if not _ALIASES_PATH.is_file():
        log.warning("skill_aliases.missing path=%s", _ALIASES_PATH)
        return {}
    data = yaml.safe_load(_ALIASES_PATH.read_text(encoding="utf-8")) or []
    out: dict[str, str] = {}
    for entry in data:
        canonical = entry["canonical"].strip().lower()
        for alias in entry.get("aliases", []):
            out[alias.strip().lower()] = canonical
        out[canonical] = canonical
    return out


def _basic_normalise(raw: str) -> str:
    s = raw.strip().lower()
    s = _PUNCT_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return _alias_table().get(s, s)


# ---------------- categories.yaml (curated skill families) ------------------


@lru_cache(maxsize=1)
def _category_table() -> dict[str, list[str]]:
    """canonical_name -> [family, ...], inverted from categories.yaml
    (family -> [skills]). Degrades gracefully to {} when the file is
    missing — matches the aliases.yaml precedent in src.pipeline.skills."""
    if not _CATEGORIES_PATH.is_file():
        log.warning("skill_categories.missing path=%s", _CATEGORIES_PATH)
        return {}
    data = yaml.safe_load(_CATEGORIES_PATH.read_text(encoding="utf-8")) or {}
    out: dict[str, list[str]] = {}
    for family, skills in data.items():
        fam = str(family).strip().lower()
        for skill in skills or []:
            canonical = str(skill).strip().lower()
            out.setdefault(canonical, [])
            if fam not in out[canonical]:
                out[canonical].append(fam)
    return out


def categories_for(canonical: str) -> list[str]:
    """Curated families for a canonical skill name, or [] if not seeded."""
    return list(_category_table().get(canonical.strip().lower(), []))


async def _ensure_categories(tx: Any, canonical: str) -> None:
    """Stamp a Skill node with its CURATED families so stage-2 ontology
    partial-credit has something to read. Curated wins; skills with no
    curated family are simply left uncategorised (no LLM backfill in v1 —
    EXTRACTION_PLAN's `skill_category_task` deferral)."""
    cats = categories_for(canonical)
    if cats:
        await tx.run(
            "MATCH (s:Skill {canonical_name: $c}) SET s.categories = $cats",
            c=canonical,
            cats=cats,
        )


# ---------------- LLM tiebreaker --------------------------------------------


class _LLMTiebreakerOut(BaseModel):
    """Strict schema for the LLM normaliser. ``match`` is the canonical name
    to merge into, or null to indicate 'create new'."""

    match: str | None = Field(default=None)


async def _ask_llm_tiebreaker(
    llm: Any, candidate: str, near: list[dict[str, Any]]
) -> str | None:
    """0.88-0.92 (settings-configured) cosine grey zone: ask the LLM to
    decide. Returns one of the candidate canonical names, or None to
    indicate 'these are not the same skill, create new'. Non-fatal: an LLM
    failure is logged and treated as no match, matching hris."""
    choices = "\n".join(
        f"- {n['name']} (aliases: {', '.join(n.get('aliases', []))})" for n in near
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You disambiguate skill names. Given a candidate and a "
                "shortlist of similar existing canonical skills, decide "
                "whether the candidate is the SAME skill as one of them. "
                'Respond with strict JSON: {"match": "<canonical_name>"} '
                'if it matches one, or {"match": null} if none match. '
                "No prose, no fences."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Candidate: {candidate!r}\n\n"
                f"Existing skills (top vector-similar):\n{choices}\n\n"
                "Return JSON only."
            ),
        },
    ]
    try:
        out = await llm.chat_json(messages, _LLMTiebreakerOut, max_tokens=128)
    except Exception:  # noqa: BLE001 — non-fatal by design, matches hris
        log.warning("skill_normalize.tiebreaker_failed candidate=%s", candidate)
        return None
    match: str | None = out.match
    return match


# ---------------- alias update (R6: dedupe-safe) -----------------------------

_ALIAS_UPDATE_CYPHER = (
    "MATCH (s:Skill {canonical_name: $c}) "
    "SET s.aliases = CASE WHEN $alias IN coalesce(s.aliases, []) "
    "THEN coalesce(s.aliases, []) "
    "ELSE coalesce(s.aliases, []) + [$alias] END"
)


async def _alias_update(session: Any, canonical: str, alias: str) -> None:
    await session.run(_ALIAS_UPDATE_CYPHER, c=canonical, alias=alias)


# ---------------- main entry point ------------------------------------------


async def resolve_canonical_names(
    session: Any,
    names: Iterable[str],
    *,
    llm: Any,
    embedder: Any,
) -> dict[str, str]:
    """Resolve every name in ``names`` to a canonical Neo4j Skill name.

    Runs entirely via auto-commit ``session.run`` (Decision 3) — never
    ``session.execute_write``/``execute_read``, so this can safely be called
    BEFORE a caller opens its own write transaction. Returns a mapping keyed
    by the exact INPUT string (not a re-normalised form) so callers can look
    up ``resolved[skill["name"]]`` with the raw name they already hold.
    """
    resolved: dict[str, str] = {}
    for raw in names:
        resolved[raw] = await _resolve_one(session, raw, llm=llm, embedder=embedder)
    return resolved


async def _resolve_one(session: Any, raw: str, *, llm: Any, embedder: Any) -> str:
    settings = get_settings()
    normalised = _basic_normalise(raw)

    # 1. Direct alias/exact resolution may already point at a node we know
    # exists in the graph.
    exact = await session.run(
        "MATCH (s:Skill) WHERE s.canonical_name = $n OR $n IN s.aliases "
        "RETURN s.canonical_name AS name LIMIT 1",
        n=normalised,
    )
    row = await exact.single()
    if row:
        return str(row["name"])

    # 2. Vector match.
    [emb] = await embedder.embed([normalised])
    near_cursor = await session.run(
        f"""
        CALL db.index.vector.queryNodes('skill_emb_idx', {_NEAR_CANDIDATE_LIMIT}, $e)
        YIELD node, score WHERE score > $low_threshold
        RETURN node.canonical_name AS name,
               coalesce(node.aliases, []) AS aliases,
               score
        ORDER BY score DESC
        """,
        e=emb,
        low_threshold=settings.skill_tiebreaker_threshold,
    )
    near = [dict(r) async for r in near_cursor]

    if near and near[0]["score"] >= settings.skill_auto_merge_threshold:
        canonical = str(near[0]["name"])
        await _alias_update(session, canonical, normalised)
        log.debug(
            "skill_normalize.auto_merged raw=%s canonical=%s score=%s",
            raw,
            canonical,
            near[0]["score"],
        )
        return canonical

    # 3. Grey zone -> LLM tiebreaker. Decision 4: the answer must be one of
    # the OFFERED near candidates, or it is treated as no match — a
    # hallucinated name is never used to MATCH an existing node.
    if near:
        valid_names = {str(n["name"]) for n in near}
        match = await _ask_llm_tiebreaker(llm, normalised, near)
        if match is not None and match in valid_names:
            canonical = match
            await _alias_update(session, canonical, normalised)
            log.info(
                "skill_normalize.llm_merged raw=%s canonical=%s top_score=%s",
                raw,
                canonical,
                near[0]["score"],
            )
            return canonical
        if match is not None and match not in valid_names:
            log.warning(
                "skill_normalize.tiebreaker_hallucinated raw=%s answer_rejected=true",
                raw,
            )

    # 4. Create a new canonical node.
    await session.run(
        "MERGE (s:Skill {canonical_name: $n}) "
        "ON CREATE SET s.aliases = [$n], s.embedding = $e",
        n=normalised,
        e=emb,
    )
    log.info("skill_normalize.created raw=%s canonical=%s", raw, normalised)
    return normalised


__all__ = [
    "categories_for",
    "resolve_canonical_names",
]
