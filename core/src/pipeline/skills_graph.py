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

Also folds in the 4b-security re-audit's F3 finding (defence layer 1 of 2):

* **A skill name is free text, not a vocabulary allowlist.**
  ``canonicalize_skill_names`` is a passthrough normaliser and
  ``ResumeSkill.name`` is a 200-char-capped free string — an LLM can emit
  contact information (or the candidate's own identity) as a "skill" off a
  header-shaped chunk. This module is the ONLY place a skill name is ever
  handed to the embedder before landing in Neo4j cleartext, so
  ``_resolve_one`` shape-rejects a name that looks like an email address, a
  phone number, or is implausibly long/verbose for a skill, BEFORE any
  ``embed()``/Cypher call ever touches it. A rejected name resolves to
  ``None`` (never a canonical name) and the rejection is logged as a COUNT/
  category only — never the value (R3 discipline). Layer 2
  (``src.worker.resume_tasks``, parse time) additionally scrubs a skill name
  that IS the candidate's own identity (e.g. "Casey Rivera") using candidate
  context this shape-only layer does not have.

Round-2 security re-audit (F3, still open) — ``reject_reason_for_skill_name``
adds Decision A, human-locked over security's proposed strict allowlist:

* **Name-shape reject + vocabulary check, NOT a strict allowlist.** A skill
  name is rejected iff it *looks like a person's name* (2-3 capitalised
  alphabetic tokens with no technical marker — no digit, no ``.``/``+``/``#``
  used as a tech token like ``C++``/``C#``/``.NET``/``Node.js``/``ISO
  27001``/``IPv6``; a middle initial and a comma-reordered form are still
  name-shaped; a bare single capitalised token is checked too, so ``Rivera``
  alone is caught) **AND** it misses the vocabulary (``aliases.yaml`` +
  ``categories.yaml``). The vocabulary check is what protects recall:
  anything the 220-term vocabulary already knows (``Kafka``, ``Django``,
  ``Kubernetes``, ...) is kept regardless of shape — the strict allowlist
  security proposed was rejected by the human specifically because it drops
  every skill outside that vocabulary, a large silent recall loss.
* **Must run on the RAW name, BEFORE canonicalisation (F3b).**
  ``src.worker.resume_tasks._extract_skills_merged`` canonicalises every LLM
  skill name via ``canonicalize_skill_names`` before this module ever sees
  it, and ``_basic_normalise``'s punctuation strip deletes ``@`` — so by the
  time a résumé-path name reaches ``_resolve_one`` here, an email-shaped name
  can never trip ``_EMAIL_SHAPE_RE`` again. ``resume_tasks`` therefore calls
  ``reject_reason_for_skill_name`` directly on each LLM detail's RAW ``name``
  (before any canonicalisation), in addition to this module's own call
  inside ``_resolve_one`` (which still protects the JD/job-side path, where
  no canonicalisation happens before resolution).
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

# F3 (security, layer 1) — a legitimate skill name is short. 200 chars of
# free text, or a dozen tokens, is not a skill; it is exactly the shape a
# looping/hallucinating small model produces when it copies a header block
# into a "skill". Checked against the RAW input (never the alias/punctuation
# -normalised form — `_basic_normalise` strips "@", which would defeat the
# email check below).
_MAX_SKILL_NAME_CHARS = 60
# Round 2 (security re-audit): widened 6 -> 8. Zero of the 220 shipped vocab
# terms trip the OLD 6-token cap, so this is pure headroom for a legitimate
# multi-word certification name (e.g. a 7-token cert), not a regression.
_MAX_SKILL_NAME_TOKENS = 8
_EMAIL_SHAPE_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
# A contiguous run of digits/phone-punctuation (no letters) that carries at
# least 7 digits — long enough to be a real phone number, short enough that
# a legitimate skill name's occasional digit ("ISO 27001", "IPv6") never
# trips it (those digits aren't contiguous with dashes/spaces/parens).
_PHONE_RUN_RE = re.compile(r"[+()\-.\s\d]{7,}")

# Decision A (round-2 security re-audit, F3) — person-name shape. A "word" is
# Title Case ALPHA ONLY (`^[A-Z][a-z]+$`): this deliberately excludes
# ALL-CAPS acronyms ("AWS", "SQL", "REST") and mixed-case technical proper
# nouns ("PostgreSQL"), which never look like a person's name. A middle
# initial ("M.") is its own token shape.
_PERSON_NAME_WORD_RE = re.compile(r"^[A-Z][a-z]+$")
_PERSON_MIDDLE_INITIAL_RE = re.compile(r"^[A-Z]\.$")


class UnresolvedSkillNameError(RuntimeError):
    """Raised by a projection write-tx callback when it looks up a skill
    name that ``resolve_canonical_names`` was never asked to resolve at all
    — a programming error (a name missing from the resolved mapping is NOT
    the same outcome as a legitimate no-near-candidate or PII-shape-rejected
    resolution, both of which DO have an entry). F6 (security re-audit):
    hris's ``resolved_skills.get(name, name)`` silently falls back to the
    UNRESOLVED raw name, which matches no ``Skill`` node in Cypher and the
    HAS_SKILL/REQUIRES edge silently vanishes (R5's exact failure class,
    reintroduced). Fail loud instead — never include the skill name itself
    in the message (it may be PII; see F3)."""


def _looks_like_phone(name: str) -> bool:
    for m in _PHONE_RUN_RE.finditer(name):
        run = m.group()
        digits = sum(ch.isdigit() for ch in run)
        if digits >= 7:
            return True
    return False


def _looks_like_person_name(name: str) -> bool:
    """Decision A (round-2 security re-audit, F3) shape test: does ``name``
    look like a PERSON'S name (as opposed to a skill)?

    Person-name-shaped = 2-3 capitalised alphabetic tokens, with no
    technical marker (a digit anywhere, or a bare ``+``/``#`` — ``C++``,
    ``C#``, ``IPv6``, ``ISO 27001``), allowing:

    * a middle initial (``Casey M. Rivera``) — a lone capital letter + ``.``
      is its own token shape, distinct from a technical ``.`` (``.NET``,
      ``Node.js`` — a ``.`` ANYWHERE ELSE disqualifies the whole name);
    * a comma-reordered form (``Rivera, Casey`` — the comma is treated as a
      token separator);
    * a hyphenated surname (``Casey-Rivera`` — a hyphen JOINING two Title
      Case alphabetic parts is a name join, not a technical marker; any
      other hyphen shape disqualifies).

    A BARE SINGLE token is included (``Rivera`` alone must be caught), which
    is deliberately broad — the vocabulary check in
    ``reject_reason_for_skill_name`` is what keeps this from eating
    legitimate single-word Title-Case skills (``Kafka``, ``Django``,
    ``Kubernetes``): this function only asks "is this SHAPED like a name",
    never "is this a skill" — that second question is the caller's vocab
    check.
    """
    s = name.strip()
    if not s or any(ch.isdigit() for ch in s) or "+" in s or "#" in s:
        return False
    tokens: list[str] = []
    for tok in s.replace(",", " ").split():
        if _PERSON_MIDDLE_INITIAL_RE.match(tok):
            tokens.append(tok)
            continue
        if "." in tok:
            # A dot anywhere other than a recognised middle initial is a
            # technical marker (".NET", "Node.js") — never name-shaped.
            return False
        parts = tok.split("-")
        if len(parts) > 1:
            if not all(_PERSON_NAME_WORD_RE.match(p) for p in parts):
                return False
            tokens.extend(parts)
        else:
            tokens.append(tok)

    if not tokens or len(tokens) > 3:
        return False
    if not all(
        _PERSON_NAME_WORD_RE.match(t) or _PERSON_MIDDLE_INITIAL_RE.match(t)
        for t in tokens
    ):
        return False
    # At least one real (non-initial) name word — a lone "M." is never
    # sufficient on its own.
    return any(_PERSON_NAME_WORD_RE.match(t) for t in tokens)


def _is_known_vocab_term(name: str) -> bool:
    """Decision A's recall guard: anything the 220-term vocabulary
    (``aliases.yaml`` + ``categories.yaml``) already knows is kept
    regardless of shape. Looked up via the SAME normalisation
    (``_basic_normalise``) the rest of this module uses, so an alias, not
    just a canonical name, still counts as a vocab hit."""
    normalised = _basic_normalise(name)
    return bool(normalised) and (
        normalised in _alias_table() or normalised in _category_table()
    )


def reject_reason_for_skill_name(name: str) -> str | None:
    """The single shape(+vocab) reject decision for a skill name — shared by
    ``_resolve_one`` (this module) AND
    ``src.worker.resume_tasks._extract_skills_merged`` (which calls this
    directly on the RAW LLM skill name, BEFORE canonicalisation — see the
    module docstring's F3b note). Returns a rejection-reason CATEGORY
    (``email_shape`` / ``phone_shape`` / ``length_or_token_cap`` /
    ``person_name_shape``), never the value that triggered it (R3
    discipline) — or ``None`` when the name is fine.
    """
    if len(name) > _MAX_SKILL_NAME_CHARS:
        return "length_or_token_cap"
    if len(name.split()) > _MAX_SKILL_NAME_TOKENS:
        return "length_or_token_cap"
    if _EMAIL_SHAPE_RE.search(name):
        return "email_shape"
    if _looks_like_phone(name):
        return "phone_shape"
    if _looks_like_person_name(name) and not _is_known_vocab_term(name):
        return "person_name_shape"
    return None


def _is_pii_shaped_skill_name(name: str) -> bool:
    """Shape-only PII guard for a raw (pre-normalisation) skill name — no
    identity knowledge, just "does this look like a skill at all". See the
    module docstring's F3 note for the companion identity-aware layer."""
    return reject_reason_for_skill_name(name) is not None


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
        # F8 (security re-audit round 2): never log the raw candidate value
        # (potentially PII-shaped, per F3) — count-only.
        log.warning("skill_normalize.tiebreaker_failed")
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
) -> dict[str, str | None]:
    """Resolve every name in ``names`` to a canonical Neo4j Skill name.

    Runs entirely via auto-commit ``session.run`` (Decision 3) — never
    ``session.execute_write``/``execute_read``, so this can safely be called
    BEFORE a caller opens its own write transaction. Returns a mapping keyed
    by the exact INPUT string (not a re-normalised form) so callers can look
    up ``resolved[skill["name"]]`` with the raw name they already hold.

    A value of ``None`` means ``raw`` was shape-rejected as PII (F3, security
    re-audit) — the caller must skip projecting that skill/edge entirely, NOT
    fall back to the raw name (see ``UnresolvedSkillNameError``/F6). Every
    name in ``names`` gets an entry, rejected or not, so a caller can tell
    "rejected" (key present, value ``None``) apart from "never resolved at
    all" (key absent — a caller bug).
    """
    resolved: dict[str, str | None] = {}
    for raw in names:
        resolved[raw] = await _resolve_one(session, raw, llm=llm, embedder=embedder)
    return resolved


async def _resolve_one(
    session: Any, raw: str, *, llm: Any, embedder: Any
) -> str | None:
    reason = reject_reason_for_skill_name(raw)
    if reason is not None:
        # R3 discipline: category only, never the value.
        log.warning("skill_normalize.pii_shaped_name_rejected reason=%s", reason)
        return None

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
        """
        CALL db.index.vector.queryNodes('skill_emb_idx', $k, $e)
        YIELD node, score WHERE score > $low_threshold
        RETURN node.canonical_name AS name,
               coalesce(node.aliases, []) AS aliases,
               score
        ORDER BY score DESC
        """,
        k=_NEAR_CANDIDATE_LIMIT,
        e=emb,
        low_threshold=settings.skill_tiebreaker_threshold,
    )
    near = [dict(r) async for r in near_cursor]

    if near and near[0]["score"] >= settings.skill_auto_merge_threshold:
        canonical = str(near[0]["name"])
        await _alias_update(session, canonical, normalised)
        # F8: log the CANONICAL name, never `raw` (potentially PII-shaped).
        log.debug(
            "skill_normalize.auto_merged canonical=%s score=%s",
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
            # F8: canonical only, never `raw`.
            log.info(
                "skill_normalize.llm_merged canonical=%s top_score=%s",
                canonical,
                near[0]["score"],
            )
            return canonical
        if match is not None and match not in valid_names:
            # F8: no value logged at all — the hallucinated match is never a
            # real Skill name and `raw` may be PII-shaped.
            log.warning("skill_normalize.tiebreaker_hallucinated answer_rejected=true")

    # 4. Create a new canonical node.
    await session.run(
        "MERGE (s:Skill {canonical_name: $n}) "
        "ON CREATE SET s.aliases = [$n], s.embedding = $e",
        n=normalised,
        e=emb,
    )
    # F8: canonical only, never `raw`.
    log.info("skill_normalize.created canonical=%s", normalised)
    return normalised


__all__ = [
    "UnresolvedSkillNameError",
    "categories_for",
    "reject_reason_for_skill_name",
    "resolve_canonical_names",
]
