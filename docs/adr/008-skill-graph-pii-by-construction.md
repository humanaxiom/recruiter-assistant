# ADR-008: Skill-Graph Projection — PII Elimination By Construction

**Status:** Accepted (supersedes the heuristic PII filter built and re-audited five times inside
ADR-007's Phase 4b `skills_graph` module; extends ADR-004's 768-d/cosine embedding contract)
**Date:** 2026-07-14

## Context

Phase 4b's skill-graph projection (`src/pipeline/skills_graph.py`, `src/worker/tasks.py::
_job_projection_tx`, `src/worker/resume_tasks.py::_resume_projection_tx`) resolves every JD/résumé
skill name to a canonical Neo4j `Skill` node. A résumé's skill list comes from an LLM call
(`resume_skills_v2`) against untrusted document text; a small model, prompted to extract "skills"
from a header-shaped chunk, will sometimes emit the candidate's own name, email, or phone number as
a "skill". Left unhandled, that string is embedded (`nomic-embed-text`) and written to Neo4j
cleartext — a structural PII leak independent of, and downstream from, every redaction Phase 3
already does to `resumes.parsed`/the outbox.

Five rounds of a security re-audit tried to close this by **pattern-matching** the leak out of the
skill name before it reached the graph: a person-name shape detector (capitalisation, tokens,
technical-marker exceptions), then an offline personal-name lexicon, then a vendor/brand-prefix
veto, then a "strict mode" collapse for when no candidate context was available to redact against.
Every fix to one side of the trade-off reopened the other:

- Round 3 tightened the shape detector for privacy and broke recall on legitimate multi-word skills
  (`distributed systems`, `data engineering`) that are shaped exactly like a person's name.
- Round 4 added a personal-name lexicon to fix that recall regression, but shipped the lexicon check
  as `any(token in lexicon for token in tokens)` — one name-shaped token anywhere in a multi-word
  skill condemned the whole string, so real vendor/product names (`Amazon Aurora`, `IBM Watson`,
  `Victoria Metrics`) were dropped as false positives.
- Round 5 fixed the quantifier (`any` → `all`) and added a vendor-prefix veto to recover those
  fixtures — but the same `all()` quantifier let a two-real-name candidate through whenever it
  happened to also start with a vendor word (`IBM John Smith`), and the vendor veto and lexicon
  fail-closed logic combined to let `Sean Kvistad` / `Torbjorn Kvistad` / `Ludovica Brambilla`
  (single-lexicon-miss full names) and `Google Кейси Ривера` (script-mixed) through.

Five rounds, the same two failure classes trading places. **A skill name is untrusted free text; no
shape heuristic can distinguish "person's name" from "unlisted multi-word skill" with the recall and
precision this system needs simultaneously.**

## Decision

Eliminate the leak class **by construction** instead of detecting it, exploiting one structural
asymmetry: **a job description carries no candidate PII. Only the résumé side can leak an
identity into the graph.**

### 1. `Skill.canonical_key` is either a cleartext vocab term or a salted hash — never free text

The `Skill` node's unique key (`canonical_key`, replacing the old `canonical_name`) is computed by
one shared function (`_canonical_key_for_normalised`), called identically from both sides:

- **Vocab hit** (`aliases.yaml` / `categories.yaml`, a closed ~220-term set): the canonical term,
  cleartext. A closed vocabulary cannot contain a person's name by definition — zero PII risk,
  independent of shape.
- **Everything else**: `"h:" + sha256(settings.skill_hash_salt + normalised)[:32]` — opaque,
  un-invertible, and (critically) computed from the exact same normalised string on both sides, so a
  JD requiring a skill and a résumé having the identical skill text still land on the same node.
  `REQUIRES`/`HAS_SKILL` still meet; no requirement silently vanishes.

### 2. `display_name` (cleartext, human-readable) is written ONLY by the job/JD side

`src/worker/tasks.py::_job_projection_tx` stamps the Skill node's `display_name` with the raw JD
text, in a dedicated Cypher statement — always safe, since a job description carries no candidate
identity, even when the node's own key is an opaque hash. `src/worker/resume_tasks.py::
_resume_projection_tx` never sets this field.

### 3. The résumé side never embeds, never vector-searches, never writes cleartext

`src/pipeline/skills_graph.py::resume_skill_canonical_key` is the **entire** résumé-side skill
resolution — a pure function: no Neo4j session, no `embedder`, no `llm` parameter exists on it or on
anything that calls it (`project_resume`'s signature dropped both entirely). A résumé-derived
non-vocab skill name is therefore **never** handed to the embedder, so it can never surface as a
Neo4j vector-search near-candidate on a later job's resolution either (the old auto-merge/
LLM-tiebreaker mechanism — kept for the job side — only ever finds nodes that carry an `embedding`,
and a hash-keyed node from the résumé side never does).

The leak case from the audits — `"Casey Rivera"` extracted as a "skill" — now becomes a Skill node
keyed `h:<hash>`, with no `display_name`, no `embedding`, no `categories`, that no job ever requires:
unreadable, un-invertible, and it contributes nothing to any score.

### 4. `reject_reason_for_skill_name` is demoted to pure junk filtering

The email-shape / phone-shape / length-and-token-cap checks are **kept** (they cost nothing and stop
obvious garbage — a copy-pasted header block — from ever becoming a graph node), but the
person-name-shape detector, the offline personal-name lexicon (`skill_data/person_names.txt`,
deleted), the vendor-prefix veto, and the `strict_lexicon` parameter are **deleted outright**. A name
that IS the candidate's own identity now sails through this function unshaped — that is a deliberate,
documented behaviour change, not a regression: privacy no longer depends on this function at all.

### 5. The salt is a required setting, fails loud exactly like `PII_KEY`

`settings.skill_hash_salt` (`SKILL_HASH_SALT` env var) defaults to `""`. `src/worker/main.py::startup`
refuses to start when it is empty, mirroring the existing `PII_KEY` check — an unsalted hash of a
likely-candidate-name keyspace is dictionary-attackable (an attacker who can read the graph could
precompute `sha256(common_name)` for a list of common names/phrases and confirm one is present).
`_hash_key` fails loud on an empty salt too, as an independent second line of defence.

## Consequences

- **A non-vocab skill loses vector auto-merge / synonym resolution at its terminal "nothing matched,
  mint a new key" step.** Two different spellings of the same unlisted skill hash to two different
  keys and never connect, unless one of them is a literal alias of a vocab term. This is an accepted
  cost, not a silent one: vocab skills keep the full canonicalisation path (exact/alias match, then
  vector-match/LLM-tiebreak against other *existing* canonical nodes — unchanged from before this
  ADR); only a genuinely novel non-vocab name is affected.
- **The disparate-impact problem (the round-3 shape widening's own S1/S4/S5 fixes) disappears
  entirely** — there is no shape heuristic left to be biased against any naming convention, because
  there is no shape heuristic left, full stop.
- **Rotating `SKILL_HASH_SALT` changes every non-vocab skill's key** and requires re-projecting the
  whole graph (every job and résumé re-parsed) to reconnect `REQUIRES`/`HAS_SKILL` edges under the new
  keys. Documented on the setting itself.
- Graph debugging shows opaque `h:<hash>` keys for every non-vocab résumé-derived skill. That is
  intended, not a bug to "fix" by adding cleartext back.
- `resumes.parsed` (Postgres) still holds the skill name cleartext at rest (ADR-007 §6 already
  accepts this), and the outbox still carries it unencrypted (ADR-007 §7 / N1, unchanged) — this ADR
  is scoped to the **graph**, which is the artifact a recruiter-facing UI and the ranking/evidence
  pipeline actually read from and could leak through. `_redact_skill_names_pii` (parse-time,
  candidate-identity-aware structured scrub) is kept as defence in depth for that Postgres/outbox
  surface, but is no longer the control this ADR depends on.

## Alternatives Considered

- **A stricter allowlist** (only the ~220-term vocabulary is ever projected) — rejected in the
  original round-2 audit and again here: it silently drops every legitimate skill outside the
  vocabulary, a large, invisible recall loss that the 4a evals corpus (whose own recorded residual is
  `weights.skill = 0.0`) would not have caught.
- **A sixth heuristic round** (yet another shape/lexicon refinement) — rejected per the human
  direction that started this ADR: "you cannot reliably pattern-match PII out of an untrusted
  free-text field. Stop trying." Five rounds of evidence support that conclusion directly.
