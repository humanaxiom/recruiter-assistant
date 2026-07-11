"""Skill normalisation — Neo4j-free slice.

Trimmed from hris ``apps/worker/src/worker/skill_normalize.py`` (see
``phase3-source-dossier.md`` §12 / the binding contract). This module
keeps ONLY the pure, deterministic functions that don't touch Neo4j:

  - ``_basic_normalise``       lowercase, strip, alias map lookup
  - ``_alias_table``           alias -> canonical_name, from aliases.yaml
  - ``match_skills_in_text``   deterministic vocabulary scan
  - ``canonicalize_skill_names``  normalise + dedupe a name list
  - ``build_summary_text``     canonical JD embedding text

The vector-match / LLM-tiebreaker / Skill-graph half of the hris module
(``normalize_skill``, ``_ask_llm_tiebreaker``, ``categories_for``,
``_family_vocab``, ``skill_categories_vocab``, ``categories.yaml``) is a
Phase 4 concern and is deliberately NOT ported here.

Path-resolution deviation: hris resolves ``aliases.yaml`` via
``Path(__file__).resolve().parents[4]`` to a repo-root ``infra/skills/``
directory. That depth is wrong at this module's (flatter) nesting, and a
repo-root ``infra/`` directory would be invisible inside the Docker build
context anyway (``compose`` builds with ``context: ./core``). The YAML
ships instead at ``core/src/pipeline/skill_data/aliases.yaml`` (inside the
build context) and is resolved relative to ``Path(__file__).resolve().parent``.
A missing file degrades gracefully to an empty table (matches nothing),
never crashes.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from src.schemas.jobs import JDExtracted

log = logging.getLogger(__name__)

_ALIASES_PATH = Path(__file__).resolve().parent / "skill_data" / "aliases.yaml"

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w.+#\- ]+")


@lru_cache(maxsize=1)
def _alias_table() -> dict[str, str]:
    """alias -> canonical_name, materialised from aliases.yaml."""
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
    """Cheap deterministic step. Lowercases, collapses whitespace,
    strips most punctuation, then resolves via the alias map if present.
    """
    s = raw.strip().lower()
    s = _PUNCT_RE.sub("", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return _alias_table().get(s, s)


# ---------------- deterministic skill matching ----------------
#
# Resume skills come from a reliable deterministic scan (this) PLUS a
# best-effort LLM list, merged. The scan finds known vocabulary terms
# (alias-table keys) verbatim in the resume text — no model, no loop.
#
# Boundary = non-[A-Za-z0-9+#]. We keep + and # in the token class so
# "c++"/"c#" match as units and don't fire on substrings ("abc#"). We do
# NOT include "." — otherwise a skill ending a sentence ("PostgreSQL.")
# wouldn't match. Multi-token terms with internal dots ("node.js") still
# match because the alternation is longest-first, so the full literal is
# consumed before its "js" suffix could match separately.


@lru_cache(maxsize=1)
def _skill_scanner() -> re.Pattern[str]:
    table = _alias_table()
    terms = sorted(table.keys(), key=len, reverse=True)
    if not terms:
        return re.compile(r"(?!x)x")  # matches nothing
    alt = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"(?<![A-Za-z0-9+#])(?:{alt})(?![A-Za-z0-9+#])", re.IGNORECASE)


def match_skills_in_text(text: str) -> list[str]:
    """Canonical skills found verbatim in ``text`` via the alias table,
    in first-seen order. Deterministic; no LLM."""
    table = _alias_table()
    seen: dict[str, None] = {}
    for m in _skill_scanner().finditer(text):
        canonical = table.get(m.group(0).lower())
        if canonical and canonical not in seen:
            seen[canonical] = None
    return list(seen)


def canonicalize_skill_names(names: Iterable[str]) -> list[str]:
    """Normalise + alias-resolve + dedupe a sequence of skill names,
    preserving first-seen order. Used to merge the deterministic and
    LLM-extracted skill lists into one clean set."""
    out: dict[str, None] = {}
    for name in names:
        canonical = _basic_normalise(name)
        if canonical and canonical not in out:
            out[canonical] = None
    return list(out)


def build_summary_text(extracted: JDExtracted) -> str:
    """Canonical embedding text for a JD — fed to nomic-embed-text."""
    parts = [extracted.title]
    if extracted.responsibilities:
        parts.append(" ".join(extracted.responsibilities))
    if extracted.required_skills:
        parts.append(
            "Required: " + ", ".join(s.name for s in extracted.required_skills)
        )
    if extracted.nice_to_have_skills:
        parts.append(
            "Nice to have: " + ", ".join(s.name for s in extracted.nice_to_have_skills)
        )
    return ". ".join(parts)


__all__ = [
    "build_summary_text",
    "canonicalize_skill_names",
    "match_skills_in_text",
]
