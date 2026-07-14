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

Round-3 security re-audit — the SAME ``reject_reason_for_skill_name``
decision function, widened (S1-S6 below; see the block comment above
``_PERSON_MIDDLE_INITIAL_RE`` for the full per-gap rationale) because the
round-2 shape test was case-rigid and ASCII-only, so a candidate's OWN name
in an all-caps résumé header (the single most common header style) sailed
straight through with a PERFECT candidate block:

* **S1** case-insensitive name-shape matching (scoped to avoid regressing
  recall on an unlisted single-word acronym — see the code comment).
* **S2** a technical marker is evidence of skill-ness, not an unconditional
  veto over the whole name.
* **S3** Mc/Mac-internal-caps and apostrophe-joined surnames recognised.
* **S4** the shape check's own token ceiling raised 3 -> 5 with lowercase
  connector particles accepted, closing a disparate-impact gap against
  Hispanic/Portuguese/Dutch/Arabic naming conventions.
* **S5** Unicode-aware (Cyrillic, CJK) rather than ASCII-only.
* **S6** the email-shape regex tolerates whitespace/``(at)``/``[at]``
  obfuscation.
* **S7** (``src.worker.resume_tasks``) redaction fails CLOSED, not open, on
  a completely empty ``CandidateInfo`` — see that module's docstring.
* **S8** an ACCEPTED skill name (auto-merged/LLM-merged/created) is now
  logged as a non-reversible reference only, never verbatim — F8 was
  previously half-closed (rejected names were already category-only).
* **S9** (documented residual) a candidate genuinely surnamed a well-known
  technology (``Kafka``, ``Django``, ``Cassandra``) is kept — the
  vocabulary arm of Decision A's conjunction always wins over the shape arm,
  by design.

Round-4 (recall regression fix) — Decision A's two-way conjunction
(name-shape AND vocab-miss) had itself quietly become the strict allowlist
the human rejected in round 2: ANY multi-word skill missing from the
220-term vocabulary that also happened to be two Title-Case-able alphabetic
words (``distributed systems``, ``data engineering``, ``natural language
processing``, ``event driven architecture``, ``test driven development``)
was indistinguishable, by SHAPE alone, from ``Casey Rivera`` — and was
silently dropped, deflating 4c's 0.40-weighted skill sub-score. The
round-2/3 recall guard never caught this because every one of ITS fixtures
(``Google Cloud Platform``, ``machine learning``, ``ISO 27001``, the
7-token cert) was a vocabulary HIT — it only ever exercised the arm that
was never at risk.

**Decision C** makes the reject a THREE-way conjunction: name-shaped AND
vocab-miss AND (at least one real token hits an offline personal-name
lexicon, OR the name contains a non-Latin script). The lexicon
(``skill_data/person_names.txt``, beside ``aliases.yaml``/
``categories.yaml`` — see that file's header for provenance/licence) is
the missing signal that tells ``distributed systems`` (no token is a
personal name) apart from ``Casey Rivera`` (both tokens are). See
``_hits_person_name_lexicon``/``_contains_non_latin_script`` below.

* A name-shaped, vocab-miss candidate whose tokens hit NEITHER the lexicon
  NOR the non-Latin-script fallback is KEPT (``distributed systems`` etc.)
  — Decision D's recall-guard fixtures pin this, mutation-proved: neutralise
  the lexicon arm back to the old two-way rule and the guard goes RED (see
  ``test_lexicon_arm_mutation_proof_recall_guard_goes_red_without_it`` in
  ``test_pipeline_skills_graph.py``).
* **Fail CLOSED, not open, if the lexicon file cannot be loaded** (``S7``
  precedent): a missing/unreadable ``person_names.txt`` makes
  ``_hits_person_name_lexicon`` report a hit UNCONDITIONALLY, collapsing
  back to the OLD, stricter two-way conjunction rather than silently never
  rejecting a PII-shaped name again on a broken deployment. This trades
  recall for safety exactly the way a working lexicon does not have to.
* **Documented residual — non-Latin-script asymmetry.** A lexicon of Latin
  given names/surnames cannot cover ``Кейси Ривера``/``李伟``; the
  script-based fallback (round-3's S5 Unicode handling) still rejects any
  name-shaped, vocab-miss candidate containing Cyrillic/CJK/other non-Latin
  script characters, REGARDLESS of a lexicon hit. This means a genuine
  non-Latin-script SKILL name (not a person's name) that is also missing
  from the vocabulary would be misclassified as PII and dropped — the same
  false-positive class as S9, just on the opposite arm, accepted as a
  residual for the same reason: the alternative (never rejecting a
  non-Latin-script name at all) is a bigger, unacceptable privacy leak.
* **S9 still applies unchanged** — the vocabulary arm always wins over
  BOTH the shape arm and the (now three-part) lexicon/script arm, so a
  candidate genuinely surnamed ``Kafka``/``Django``/``Cassandra`` is still
  kept.

Round-5 (security re-audit, round 4 of the audit series) — S10/S11/S12:
Decision C's lexicon arm was itself a recall regression, one layer down.
``_hits_person_name_lexicon`` shipped as ``any(tok in lexicon for tok in
tokens)``, which makes ONE name-shaped token condemn the WHOLE candidate:
782 of the lexicon's 783 entries rejected every skill title-cased as a
solo token, and a genuine two-word vendor/product skill built from one
common-name-shaped word plus one technical word (``Amazon Aurora`` —
"aurora" is a common given name; ``IBM Watson`` — "watson" is a common
surname; ``Victoria Metrics`` — "victoria" is a common given name) was
dropped in every casing, since ``multi_token`` case-folding applies
regardless of which half of the compound is the "real" tech word.

* **S10** — ``any(...)`` -> ``all(...)``: reject only when EVERY real
  token hits the lexicon (see ``_hits_person_name_lexicon``'s updated
  docstring). This alone recovers every MULTI-token false positive above,
  since the vendor/generic half of each pair ("amazon", "ibm", "metrics")
  is not itself a lexicon entry. It does NOT recover a single-real-token
  product name that also happens to BE a common given name/surname
  (``Julia``, ``Hudson``) — ``all`` and ``any`` agree on a length-1
  sequence, so those need the vocab backstop below instead. A
  vendor-prefix veto (``_hits_vendor_prefix_veto``) adds a second,
  independent line of defence for the multi-token case: a name LEADING
  with a known vendor/brand word ("Amazon", "IBM", "Apache", ...) is never
  treated as person-shaped, regardless of what the lexicon says about the
  other token — belt-and-braces, not load-bearing on its own for the
  fixtures above (the quantifier fix alone already saves them).
* **``aliases.yaml`` backstop** — ``julia``/``hudson`` are now 220-term-
  vocabulary entries. The vocab arm of Decision A's conjunction always wins
  (S9), so these two are exempted from the person-name check entirely,
  independent of the lexicon/quantifier/vendor-veto machinery — the only
  fix that reaches the single-real-token cases the quantifier change
  structurally cannot. Deliberately NOT extended to the multi-token
  compounds (``Amazon Aurora``/``IBM Watson``/``Apache Felix``/``Victoria
  Metrics``): those are already recovered by the ``all(...)`` quantifier
  fix on its own, and ``VictoriaMetrics`` (no space) was never rejected in
  the first place — see the recall-guard test file for the mutation proof
  that this quantifier fix, not a vocab shortcut, is what saves them.
* **S11** — the round-4 recall guard (``test_non_vocab_multiword_skill_is_
  kept_not_rejected_as_a_name``) only ever exercised the lexicon-MISS arm
  (none of its fixtures contain a person-name token at all); it never
  tested a skill whose token IS a name, so it was blind to the exact S10
  defect. New KEEP fixtures (``Amazon Aurora``, ``IBM Watson``, ``Apache
  Felix``, ``Julia``, ``VictoriaMetrics``, ``Hudson``) plus a casing-
  invariance assertion (raw/Title/lower/UPPER all agree) close that gap.
* **S12** (``src.worker.resume_tasks``) — see ``_redact_skill_names_pii``'s
  docstring: an empty ``CandidateInfo`` collapses ``reject_reason_for_
  skill_name`` to ``strict_lexicon=True`` for the skill-name scrub, since
  there is no candidate identity left to justify the recall exemptions
  above.
* **S13** (test-only) — the R7 concurrency integration test now pins
  ``outbox_drain_deadline_seconds`` to a large value; the 4.0s default let
  a cold-host run legitimately early-exit the bounded-work deadline before
  both drainers finished, which is correct ``project_to_graph`` behaviour,
  not a locking bug — see ``test_graph_projection_e2e.py``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
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
_PERSON_NAMES_PATH = Path(__file__).resolve().parent / "skill_data" / "person_names.txt"

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
# S6 (security re-audit round 3): tolerate whitespace around the separator,
# and an "(at)"/"[at]" obfuscation of "@" — "john.smith @ corp.test" and
# "casey.rivera (at) example.test" both carry a real email address that the
# OLD strict, whitespace-intolerant `local@domain` pattern missed outright.
_EMAIL_SHAPE_RE = re.compile(
    r"[^\s@]+\s*(?:@|\(at\)|\[at\])\s*[^\s@]+\.[^\s@]+", re.IGNORECASE
)
# A contiguous run of digits/phone-punctuation (no letters) that carries at
# least 7 digits — long enough to be a real phone number, short enough that
# a legitimate skill name's occasional digit ("ISO 27001", "IPv6") never
# trips it (those digits aren't contiguous with dashes/spaces/parens).
_PHONE_RUN_RE = re.compile(r"[+()\-.\s\d]{7,}")

# Decision A (round-2 security re-audit, F3) — person-name shape, widened
# across a THIRD round (this file's current state) to close five further
# gaps a case-rigid, ASCII-only, 3-token, veto-on-any-marker test left open:
#
# * S1 — case-INSENSITIVE. An ALL-CAPS ("CASEY RIVERA") or all-lowercase
#   ("casey rivera") name is exactly as name-shaped as Title Case — an
#   all-caps résumé header is the single most common header style, so a
#   strict-Title-Case-only test let a candidate's OWN name straight through
#   with a PERFECT candidate block. Folding is applied per whitespace/comma
#   token, and ONLY when there is more than one such token, OR the token
#   itself already contains a join separator ('-', '.', an apostrophe) —
#   NEVER to a single bare plain word with no separator. That carve-out is
#   deliberate: folding a lone uniformly-cased word always produces
#   something that satisfies the plain Title-Case check, so if it applied to
#   every bare acronym, any unlisted all-caps term (this repo's 220-term
#   vocabulary does not include "REST", for one — see the vocab-recall
#   sweep) would start being rejected as a person's name purely on shape.
#   Every demonstrated leak is multi-token or an internally-joined compound;
#   a compound TECHNICAL token ('.NET', 'Node.js', 'IPv6') stays
#   disqualified after folding anyway (its own internal structure fails
#   independently — see the tests), so widening the fold to compound single
#   tokens costs nothing.
# * S2 — a technical marker (digit / '+' / '#' / a non-middle-initial '.')
#   is EVIDENCE of skill-ness, not an unconditional veto over the ENTIRE
#   name. Only the LAST whitespace-token gets special treatment (a trailing
#   marker is what a stray list-number or copy-paste artifact looks like):
#   a token that is PURELY marker characters ('2', '+') is dropped outright
#   as a stray annotation, provided at least two REAL tokens remain — so
#   "ISO 27001" (which would otherwise collapse to the single bare token
#   "ISO") is deliberately EXCLUDED from the drop and stays disqualified —
#   while a token with a marker GLUED to real letters ("Rivera2",
#   "Rivera+", "Rivera#") is stripped back to its bare letters and
#   re-tested in place. A technical token that isn't rescued by either rule
#   ("C++", "C#", "IPv6", the "27001" half of "ISO 27001") simply fails the
#   per-token name-word check below and disqualifies the whole candidate —
#   there is no separate early-exit branch for "any marker anywhere" any
#   more.
# * S3 — an Mc/Mac-internal-caps surname ("McDonald", "MacArthur") and an
#   apostrophe-joined surname ("O'Brien") are recognised name-word shapes,
#   not just plain Title Case.
# * S4 — the token-count ceiling for THIS shape check only (distinct from
#   `_MAX_SKILL_NAME_TOKENS`, the outer any-skill-name length cap) is raised
#   3 -> 5, and a small set of lowercase connector particles ("del", "van",
#   "der", "de", "bin", "da", "di") are accepted without needing to look
#   name-shaped themselves — a strict Anglo 2-3-token ceiling structurally
#   protected Anglo names while failing common Hispanic/Portuguese/Dutch/
#   Arabic naming conventions ("Maria del Carmen Rivera Lopez", "Ana van
#   der Berg"), which is a disparate-impact bug, not a cosmetic one.
# * S5 — Unicode-aware throughout (Cyrillic "Кейси Ривера", CJK "李伟"):
#   every check below is built on `str.isalpha()`/`.isupper()`/`.islower()`,
#   which are Unicode-aware in the stdlib, rather than an ASCII-only
#   `[A-Z][a-z]+` regex. A script with NO case distinction at all (CJK) is
#   accepted on alphabetic-ness alone, since "is this Title Case" has no
#   meaning there.
#
# Recall stays pinned by the SAME vocabulary conjunction as before (Decision
# A's whole point): none of this widening matters for a name that IS a
# known skill (`_is_known_vocab_term`) — only for one that both looks
# name-shaped AND is nowhere in the 220-term vocabulary. See
# `test_pipeline_skills_graph.py`'s vocab/recall-pinning tests for the
# falsifiable check that this widening did not collaterally eat a real
# skill.
#
# S9 (documented residual, security re-audit round 3): a candidate genuinely
# SURNAMED a well-known technology ("Kafka", "Django", "Cassandra") is kept
# by design — the vocabulary arm of the conjunction always wins over the
# shape arm. This is the accepted Decision-A tradeoff (a strict allowlist
# was rejected by the human as too large a silent recall loss), recorded
# here explicitly rather than left as an unstated side effect.
_PERSON_MIDDLE_INITIAL_RE = re.compile(r"^[A-Z]\.$")
_MC_MAC_SURNAME_RE = re.compile(r"^(Mc|Mac)[A-Z][a-z]+$")
_APOSTROPHE_CHARS = "'’"
_NAME_JOIN_CHARS = "-." + _APOSTROPHE_CHARS
_NAME_CONNECTORS = frozenset({"del", "van", "der", "de", "bin", "da", "di"})
_MAX_NAME_SHAPE_TOKENS = 5
_TRAILING_MARKER_RE = re.compile(r"[\d+#]+$")


def _fold_uniform_case(tok: str) -> str:
    """S1: fold a token that is ENTIRELY uppercase or ENTIRELY lowercase to
    Title Case, per maximal alphabetic run (so a separator-joined token like
    'CASEY-RIVERA' folds to 'Casey-Rivera', not the flattened 'Casey-rivera'
    a naive whole-string ``str.capitalize()`` would produce). A token that
    is ALREADY mixed-case ('PostgreSQL', 'McDonald', 'Node.js') is left
    completely untouched — there is no uniform case to normalise, and
    guessing would destroy real information (an internal cap, a genuine
    mixed-case technical spelling)."""
    if tok.isupper() or tok.islower():
        return re.sub(r"[^\W\d_]+", lambda m: m.group().capitalize(), tok)
    return tok


def _is_name_part(tok: str) -> bool:
    """A single decomposed name PART — plain Title Case, an Mc/Mac surname,
    or a caseless-script word (S5) — checked in whatever casing it already
    carries (no further folding at this level: see `_fold_uniform_case`'s
    docstring for why 'Node.js' must not be rescued here on its lowercase
    'js' half)."""
    if not tok or not tok.isalpha() or len(tok) < 2:
        return False
    if tok[0].isupper() and tok[1:].islower():
        return True
    if _MC_MAC_SURNAME_RE.match(tok):
        return True
    # Caseless script (S5): no character in `tok` carries case at all, so
    # "Title Case" has no meaning — alphabetic is the only signal available.
    return not any(ch.isupper() or ch.islower() for ch in tok)


def _dot_or_hyphen_join_parts(tok: str, sep: str) -> list[str] | None:
    """Split `tok` on `sep` ('.' or '-') and return the parts iff EVERY part
    is a valid `_is_name_part` — or ``None`` if the split isn't a fully
    name-shaped decomposition (the caller then disqualifies `tok`
    outright, matching '.NET'/'Node.js' still failing on their non-name
    halves)."""
    parts = tok.split(sep)
    if len(parts) < 2 or any(not p for p in parts):
        return None
    if all(_is_name_part(p) for p in parts):
        return parts
    return None


def _apostrophe_join_parts(tok: str) -> list[str] | None:
    """S3: an apostrophe-joined surname ("O'Brien"). Exactly two parts are
    expected; the FIRST may be a single uppercase letter (a common Irish/
    French prefix shape `_is_name_part` alone rejects on length), the
    second must be a full `_is_name_part`."""
    sep = "'" if "'" in tok else "’"
    parts = tok.split(sep)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    prefix, rest = parts
    prefix_ok = _is_name_part(prefix) or (
        len(prefix) == 1 and prefix.isalpha() and prefix.isupper()
    )
    if prefix_ok and _is_name_part(rest):
        return [prefix, rest]
    return None


def _drop_or_strip_trailing_marker(tokens: list[str]) -> list[str]:
    """S2: a technical marker on the LAST token only is either (a) a stray
    standalone annotation ("Casey Rivera 2") — dropped outright, but ONLY
    when doing so still leaves at least two other tokens, so "ISO 27001"
    (which would otherwise collapse to the single bare token "ISO") is
    deliberately left alone and falls through to disqualify normally — or
    (b) glued to real letters ("Rivera2", "Rivera+", "Rivera#") — stripped
    back to the bare letters and left in place for the normal per-token
    check to accept or reject on its own merits."""
    if not tokens:
        return tokens
    last = tokens[-1]
    if re.fullmatch(r"[\d+#]+", last):
        if len(tokens) >= 3:
            return tokens[:-1]
        return tokens
    stripped = _TRAILING_MARKER_RE.sub("", last)
    if stripped != last:
        return [*tokens[:-1], stripped]
    return tokens


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


def _decompose_name_shape(name: str) -> list[tuple[str, bool]] | None:
    """Decision A shape test (see the block comment above the regexes for
    the full S1-S5 round-3 rationale), returning the decomposed token list
    (``(word, counts_as_a_"real"_word)``) when ``name`` looks like a
    PERSON's name shape, or ``None`` when it does not.

    A BARE SINGLE token is included (``Rivera`` alone must be caught), which
    is deliberately broad — the vocabulary check AND (round 4, Decision C)
    the personal-name lexicon check in ``reject_reason_for_skill_name`` are
    what keep this from eating legitimate single/multi-word skills
    (``Kafka``, ``distributed systems``): this function only asks "is this
    SHAPED like a name", never "is this a skill" — those further questions
    are the caller's job.

    Returning the token list (not just a bool) lets
    ``_name_shape_real_tokens`` reuse the SAME decomposition for the
    lexicon lookup, rather than re-deriving it and risking the two getting
    out of sync.
    """
    s = name.strip()
    if not s:
        return None

    raw_tokens = [t for t in s.replace(",", " ").split() if t]
    if not raw_tokens:
        return None
    raw_tokens = _drop_or_strip_trailing_marker(raw_tokens)
    if not raw_tokens:
        return None

    multi_token = len(raw_tokens) > 1
    flat: list[tuple[str, bool]] = []  # (word, counts_as_a_"real"_word)

    for tok in raw_tokens:
        should_fold = multi_token or any(ch in tok for ch in _NAME_JOIN_CHARS)
        folded = _fold_uniform_case(tok) if should_fold else tok

        if _PERSON_MIDDLE_INITIAL_RE.match(folded):
            flat.append((folded, False))
            continue
        if folded.lower() in _NAME_CONNECTORS:
            flat.append((folded, False))
            continue
        if any(ch in _APOSTROPHE_CHARS for ch in folded):
            apos_parts = _apostrophe_join_parts(folded)
            if apos_parts is None:
                return None
            prefix, rest = apos_parts
            flat.append((prefix, len(prefix) > 1))
            flat.append((rest, True))
            continue
        if "." in folded:
            dot_parts = _dot_or_hyphen_join_parts(folded, ".")
            if dot_parts is None:
                return None
            flat.extend((p, True) for p in dot_parts)
            continue
        if "-" in folded:
            hyphen_parts = _dot_or_hyphen_join_parts(folded, "-")
            if hyphen_parts is None:
                return None
            flat.extend((p, True) for p in hyphen_parts)
            continue
        if _is_name_part(folded):
            flat.append((folded, True))
            continue
        return None

    if not flat or len(flat) > _MAX_NAME_SHAPE_TOKENS:
        return None
    # At least one real (non-initial, non-connector, non-single-letter-
    # prefix) name word — a lone "M." (or a lone apostrophe prefix) is never
    # sufficient on its own.
    if not any(is_real for _, is_real in flat):
        return None
    return flat


def _looks_like_person_name(name: str) -> bool:
    """Does ``name`` look like a PERSON's name shape? See
    ``_decompose_name_shape`` for the full rationale — this is a thin bool
    wrapper kept for the existing shape-only call sites/tests."""
    return _decompose_name_shape(name) is not None


def _name_shape_real_tokens(name: str) -> list[str]:
    """Decision C (round-4 recall regression): the REAL (non-connector,
    non-middle-initial, non-single-letter-prefix) name-part tokens of a
    name-SHAPED candidate, lower-cased for the personal-name lexicon lookup
    below — or ``[]`` if ``name`` is not name-shaped at all. (It cannot be
    name-shaped with zero real tokens: ``_decompose_name_shape`` already
    requires at least one.)"""
    flat = _decompose_name_shape(name)
    if flat is None:
        return []
    return [tok.lower() for tok, is_real in flat if is_real]


# Decision C (round-4 recall regression fix) — see the module docstring's
# "Round-4" section for the full rationale. Unicode script names containing
# any of these substrings identify a NON-Latin script character — used as
# the accepted-residual fallback signal for a name-shaped, vocab-miss
# candidate a Latin-alphabet personal-name lexicon structurally cannot
# cover (Cyrillic "Кейси Ривера", CJK "李伟", ...).
_NON_LATIN_SCRIPT_MARKERS = (
    "CJK",
    "CYRILLIC",
    "HIRAGANA",
    "KATAKANA",
    "HANGUL",
    "ARABIC",
    "HEBREW",
    "DEVANAGARI",
    "THAI",
    "GREEK",
)


def _contains_non_latin_script(name: str) -> bool:
    """S5 (round 3)/Decision C (round 4): does ``name`` contain at least one
    alphabetic character from a non-Latin script? Used as the script-based
    fallback arm of Decision C's three-way conjunction, independent of the
    (necessarily Latin-alphabet) personal-name lexicon."""
    for ch in name:
        if not ch.isalpha():
            continue
        try:
            uname = unicodedata.name(ch)
        except ValueError:
            continue
        if any(marker in uname for marker in _NON_LATIN_SCRIPT_MARKERS):
            return True
    return False


@lru_cache(maxsize=1)
def _name_lexicon() -> frozenset[str] | None:
    """Decision C's offline personal-name lexicon
    (``skill_data/person_names.txt`` — see that file's header for
    provenance/licence). Returns ``None`` (a distinct sentinel from "loaded
    but empty") when the file cannot be read at all, so
    ``_hits_person_name_lexicon`` can fail CLOSED rather than open."""
    if not _PERSON_NAMES_PATH.is_file():
        log.error("skill_name_lexicon.missing path=%s", _PERSON_NAMES_PATH)
        return None
    names: set[str] = set()
    for line in _PERSON_NAMES_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        names.add(stripped.lower())
    return frozenset(names)


def _hits_person_name_lexicon(tokens: Iterable[str]) -> bool:
    """Decision C, S10-corrected (round-5 security re-audit): do ALL of
    ``tokens`` (already lower-cased real name-shape tokens — see
    ``_name_shape_real_tokens``) hit the offline personal-name lexicon?

    Round-4 shipped this as ``any(...)``, which made the lexicon arm
    INCIDENTAL rather than decisive: one name-shaped token anywhere in a
    multi-word candidate condemned the WHOLE name, so a real vendor/product
    skill built from one common-name-shaped word plus one technical word
    (``Amazon Aurora`` — "aurora" hits; ``IBM Watson`` — "watson" hits;
    ``Victoria Metrics`` — "victoria" hits) was rejected as PII even though
    the OTHER token ("amazon"/"ibm"/"metrics") is plainly not a person's
    name at all. ``all(...)`` makes the lexicon arm DECISIVE instead:
    reject only when EVERY real token is itself a lexicon hit — exactly
    what "every real token looks like a personal name" should mean. A
    single-real-token candidate (``Rivera``, ``Julia``) is unaffected
    (``all`` and ``any`` agree on a length-1 sequence); a genuine
    ``Casey Rivera`` (BOTH tokens are names) still fails closed. See
    ``aliases.yaml``'s S10 backstop for the single-token product-name cases
    (``Julia``, ``Hudson``) this quantifier change cannot reach on its own.

    Fails CLOSED (S7 precedent), not open, if the lexicon could not be
    loaded at all — collapsing back to the OLDER, stricter two-way
    conjunction (name-shape AND vocab-miss) rather than silently widening
    recall (and reopening the F3 PII leak) on a broken deployment where the
    bundled data file went missing. This sentinel is checked BEFORE the
    ``all()``/``any()`` distinction even arises, so it is unaffected by the
    S10 quantifier change — confirmed by
    ``test_lexicon_file_missing_fails_closed_not_open``.
    """
    lexicon = _name_lexicon()
    if lexicon is None:
        return True
    return all(tok in lexicon for tok in tokens)


# S10 (round-5 security re-audit) — a vendor/brand prefix leading an
# otherwise name-shaped, vocab-miss candidate ("Amazon Aurora", "IBM
# Watson", "Apache Felix") marks it as a PRODUCT name, not a person's name,
# regardless of what the (necessarily imperfect) personal-name lexicon says
# about the OTHER token. This is deliberately narrow (a short, well-known
# set of enterprise-tech vendors) rather than a general "any capitalised
# word could be a brand" rule, which would reopen the same recall-vs-strict-
# allowlist tension Decision A's human-locked call already rejected once.
# Checked case-insensitively against the FIRST real name-shape token, since
# every one of these vendor patterns leads with the vendor word
# ("Amazon Aurora", never "Aurora Amazon").
_VENDOR_PREFIX_TOKENS = frozenset(
    {
        "amazon",
        "apache",
        "ibm",
        "microsoft",
        "google",
        "oracle",
        "elastic",
        "redhat",
        "salesforce",
        "adobe",
        "cisco",
        "vmware",
    }
)
# The one two-word vendor name in the set above ("Red Hat") needs its own
# phrase check — neither "red" nor "hat" alone is a useful single-token
# vendor marker, and "red" in particular is common enough as an ordinary
# word that adding it to `_VENDOR_PREFIX_TOKENS` unscoped would be a
# needless widening.
_VENDOR_PREFIX_PHRASES = ("red hat",)


def _hits_vendor_prefix_veto(name: str, real_tokens: list[str]) -> bool:
    """S10: does ``name`` lead with a known vendor/brand prefix? When true,
    this OVERRIDES the person-name-shape verdict for the whole candidate —
    a vendor-prefixed product name is never treated as PII, independent of
    whatever the lexicon/script arms below would otherwise conclude about
    the candidate's other token(s). ``real_tokens`` is the SAME lower-cased
    list ``_hits_person_name_lexicon`` consumes, so the two never see a
    different tokenisation of the same name."""
    if real_tokens and real_tokens[0] in _VENDOR_PREFIX_TOKENS:
        return True
    lowered = name.strip().lower()
    return any(lowered.startswith(phrase) for phrase in _VENDOR_PREFIX_PHRASES)


def _skill_log_ref(canonical: str) -> str:
    """S8 (security re-audit round 3): an ACCEPTED skill-normalisation log
    line (auto-merged / LLM-merged / newly created) previously logged
    ``canonical=%s`` verbatim — exactly as much of an R3 violation as
    logging a REJECTED name would be, since a résumé header name that slips
    past the shape+vocab guard (S9's documented residual, or a future gap)
    still lands in the log line-for-line the moment it's accepted. This
    returns a short, non-reversible reference (12 hex chars of sha256) that
    still lets two log lines about the SAME skill be correlated, without
    ever printing the name. Not a cryptographic guarantee against a
    small-vocabulary brute force — a mitigation, same class as the
    category-only discipline `reject_reason_for_skill_name` already uses
    for the rejected side."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


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


def reject_reason_for_skill_name(
    name: str, *, strict_lexicon: bool = False
) -> str | None:
    """The single shape(+vocab+lexicon) reject decision for a skill name —
    shared by ``_resolve_one`` (this module) AND
    ``src.worker.resume_tasks._extract_skills_merged``/``_redact_skill_names_pii``
    (which call this directly on a skill name outside any Neo4j write —
    see the module docstring's F3b note). Returns a rejection-reason
    CATEGORY (``email_shape`` / ``phone_shape`` / ``length_or_token_cap`` /
    ``person_name_shape``), never the value that triggered it (R3
    discipline) — or ``None`` when the name is fine.

    Decision C (round-4 recall regression fix, see the module docstring's
    "Round-4" section): ``person_name_shape`` is normally a THREE-way
    conjunction — name-SHAPED, AND a vocabulary miss, AND (at least one real
    token hits the offline personal-name lexicon OR the name contains a
    non-Latin script). Shape and vocab-miss alone (the old two-way rule)
    cannot tell ``distributed systems`` apart from ``Casey Rivera`` — both
    are two Title-Case-able alphabetic tokens outside the 220-term
    vocabulary; the lexicon/script check supplies the missing signal.
    Round-5 (S10) makes the lexicon arm ``all(...)`` rather than
    ``any(...)`` (see ``_hits_person_name_lexicon``) and adds a vendor-
    prefix veto (``_hits_vendor_prefix_veto``) that overrides the lexicon/
    script arms entirely for a known vendor-prefixed product name.

    ``strict_lexicon`` (S12, round-5 security re-audit): when true, the
    lexicon AND vendor-prefix arms are skipped entirely and the decision
    collapses to the OLDER, stricter two-way rule — name-SHAPED AND
    vocabulary-miss is sufficient to reject, full stop, exactly as if the
    lexicon had hit unconditionally (the SAME fail-closed collapse that
    already happens automatically when the lexicon file itself cannot be
    read — see ``_hits_person_name_lexicon``). Used by
    ``src.worker.resume_tasks._redact_skill_names_pii`` when
    ``_redaction_available(candidate)`` is False: with no candidate
    identifiers at all to pattern-match a skill name against, there is no
    context left to justify the recall-preserving lexicon/vendor
    exemptions, so this trades recall for privacy exactly the way the
    fail-closed lexicon-missing case already does.
    """
    if len(name) > _MAX_SKILL_NAME_CHARS:
        return "length_or_token_cap"
    if len(name.split()) > _MAX_SKILL_NAME_TOKENS:
        return "length_or_token_cap"
    if _EMAIL_SHAPE_RE.search(name):
        return "email_shape"
    if _looks_like_phone(name):
        return "phone_shape"
    real_tokens = _name_shape_real_tokens(name)
    if real_tokens and not _is_known_vocab_term(name):
        if strict_lexicon:
            return "person_name_shape"
        if not _hits_vendor_prefix_veto(name, real_tokens) and (
            _hits_person_name_lexicon(real_tokens) or _contains_non_latin_script(name)
        ):
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
        # S8 (round 3): NOT the canonical name either — a hash reference
        # only (see `_skill_log_ref`'s docstring for why "canonical only"
        # was still an R3 violation for an ACCEPTED name).
        log.debug(
            "skill_normalize.auto_merged ref=%s score=%s",
            _skill_log_ref(canonical),
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
            # S8 (round 3): hash reference only, never the name itself.
            log.info(
                "skill_normalize.llm_merged ref=%s top_score=%s",
                _skill_log_ref(canonical),
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
    # S8 (round 3): hash reference only, never the name itself.
    log.info("skill_normalize.created ref=%s", _skill_log_ref(normalised))
    return normalised


__all__ = [
    "UnresolvedSkillNameError",
    "categories_for",
    "reject_reason_for_skill_name",
    "resolve_canonical_names",
]
