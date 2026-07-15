# Phase 4b — Graph projection

**Status:** built and gate-green on branch `feat/phase-4b-graph-projection`, opened as **PR #11**
(https://github.com/humanaxiom/recruiter-assistant/pull/11), tip `429adc7`, 20 commits, off `main` @
`464a479` (Phase 4a hardening, PR #10's merge commit). **CI is fully green — but PR #11 is OPEN,
awaiting human merge, NOT yet merged.**

This sub-phase builds the outbox drainer and the Neo4j half of `skill_normalize`: the code that turns
Phase 3's Postgres-only `job.parsed`/`resume.parsed` rows into a queryable skill graph. It is also the
sub-phase where the corpus Phase 4a spent nine rounds hardening finally ran against **real** product
code — and it found things the idealized-engine audits couldn't.

## What shipped

- **`core/src/worker/graph_tasks.py`** — the outbox drainer. Cron-driven, claims undelivered rows with
  `FOR UPDATE SKIP LOCKED`, bounded by a batch size, a deadline, and a per-row skill-resolution cap;
  dead-letters a row after `outbox_max_delivery_attempts` and records only `type(exc).__name__` in
  `outbox.last_error` (never the exception message or payload — a message can carry candidate text).
- **`core/src/worker/tasks.py::_job_projection_tx`** / **`core/src/worker/resume_tasks.py::
  _resume_projection_tx`** — the job/résumé Neo4j projection. Writes `Job`/`Resume`/`ResumeChunk`/
  `Skill` nodes and `HAS_CHUNK`/`HAS_SKILL`/`REQUIRES` edges only. Per the plan's required deviation
  (Risk #1), chunk-text preview is read from `resumes.parsed` in Postgres, not the ADR-007-stripped
  outbox payload. **No `parsed.candidate`, no chunk text, no summary is ever projected** — the
  invariant Phase 3 built the outbox boundary to protect continues past the outbox into the graph.
- **`core/src/pipeline/skills_graph.py`** + **`core/src/pipeline/skill_data/categories.yaml`** — the
  Neo4j skill-graph half of `skill_normalize`: canonical-key resolution, vocab/alias matching, and the
  family-credit ontology (`categories.yaml` is new in 4b — no ontology data existed through 4a).
- **`core/tests/evals/fixtures/outbox/{job_parsed,resume_parsed}.json`** — the outbox-shaped fixture
  the 4a hardening audit flagged as missing (nothing before 4b encoded what the outbox payload is
  *allowed* to contain).

## The headline: the PII rework (ADR-008)

`ResumeSkill.name` is untrusted free text — it comes from an LLM call (`resume_skills_v2`) against a
résumé chunk, and a small model that fumbles a header line into `skills[]` will occasionally emit the
candidate's own name. Unhandled, that string becomes a `nomic-embed-text` vector written cleartext to
Neo4j's `skill_emb_idx`, structurally leaking identity — and, worse, propagating into *other*
candidates' tiebreaker prompts via the shared skill vocabulary, since a near-duplicate vector can
auto-merge onto an existing node.

**Five rounds of heuristic pattern-matching tried to close this and failed, in the CLAUDE.md
review-iterate sense — five iterations, a critical still open at the end:**

| Round | Commits | Mechanism added | New hole opened |
|---|---|---|---|
| 1 | `red(4b-sec)` `6f5f617` → `green(4b-sec)` `16fd4d7` | Shape rejection — reject skill names shaped like a person (capitalisation/token count) + fail-loud on unresolved skills | Broke recall on legitimate multi-word skills shaped exactly like a name (`distributed systems`, `data engineering`) |
| 2 | `red(4b-sec2)` `0486dfc` → `green(4b-sec2)` `b9742c4` | Tightened the shape+vocab reject to survive the real production path; widened the token cap | Tightening for privacy cost more real skills |
| 3 | `red(4b-sec3)` `c3eb00a` → `green(4b-sec3)` `a340687` | Case/script-insensitive name-shape guard; fail-closed redaction | Still traded recall against precision on the same axis |
| 4 | `green(4b-sec4)` `1ef713c` | Offline personal-name lexicon added (three-way reject: shape + vocab-miss + name lexicon) to recover the recall round 3 cost | Lexicon checked with `any(token in lexicon for token in tokens)` — one name-shaped token anywhere in a multi-word skill condemned the whole string, so real vendor/product names (`Amazon Aurora`, `IBM Watson`, `Victoria Metrics`) were dropped as false positives |
| 5 | `red(4b-sec5)` `e284087` → `green(4b-sec5)` `398a8ba` | Quantifier fixed (`any`→`all`) + a vendor/brand-prefix veto to recover the vendor-name fixtures | The same `all()` quantifier let a two-real-name candidate through whenever it also started with a vendor word (`IBM John Smith`); the vendor veto and lexicon fail-closed logic combined to let single-lexicon-miss full names (`Sean Kvistad`, `Torbjorn Kvistad`, `Ludovica Brambilla`) and script-mixed names (`Google Кейси Ривера`) straight through |

Five rounds, the same two failure classes (over-reject legitimate skills / under-reject real names)
trading places, hitting CLAUDE.md's 5-self-iteration cap with a critical still open. Full failure
narrative: [ADR-008](../adr/008-skill-graph-pii-by-construction.md) §Context.

**The human's call: stop pattern-matching, eliminate the class by construction.** The insight that
unlocked it — *a job description contains no candidate PII*. Only the résumé side can leak an identity
into the graph, so only the résumé side needs to be structurally incapable of it:

- `red(4b-arch)` `70339b0` → `green(4b-arch)` `2a0832240a` (ADR-008): `Skill.canonical_key` is
  computed by one shared pure function, `_canonical_key_for_normalised`, called identically from both
  sides — either the **vocab canonical term** (cleartext; a closed ~220-term vocabulary cannot contain
  an arbitrary name) or `"h:" + sha256(salt + normalised)[:32]` (opaque, un-invertible). `display_name`
  (cleartext, human-readable) is written **only** by the JD side; the résumé-side `MERGE` is bare and
  cannot set it on an existing node. `project_resume`'s signature drops `embedder`/`llm` entirely — the
  résumé side never embeds, never vector-searches, never writes cleartext, so a mis-extracted name can
  never surface as a vector-merge candidate for a later job either. `SKILL_HASH_SALT` is a required
  setting, fails loud at worker startup exactly like `PII_KEY`. All name-detection heuristics from the
  five rounds above — the shape detector, the lexicon, the vendor veto, `person_names.txt` — are
  **deleted outright**, not tuned again.
- `red(4b-arch2)` `4715ac9` → `green(4b-arch2)` `d57db5d`: security's re-audit of the rearchitecture
  found (**F1**) the first cut of `_resolve_one` didn't actually hold the invariant — three of its four
  branches (exact match, vector auto-merge, LLM tiebreak) returned an *existing* node's own key instead
  of calling the pure function, so a JD-side `REQUIRES` could point at a different node than the
  résumé-side `HAS_SKILL`, silently zeroing that skill's score, and the auto-merge/LLM branches then
  *persisted* the divergence via an alias write — permanent, self-reinforcing drift. Also found
  (**F2**) the `canonical_key` uniqueness constraint migration used `CREATE CONSTRAINT ... IF NOT
  EXISTS` under the *old* constraint name (reused from Phase 3) — a silent no-op against a real Neo4j
  that already had that name, giving no uniqueness guarantee at all and allowing duplicate `Skill`
  nodes under concurrent drain. Fixed: `_resolve_one` now returns `_canonical_key_for_normalised` on
  **every** branch unconditionally (vector auto-merge/LLM tiebreak demoted to alias-list enrichment
  only, never key selection); the constraint migration renamed and re-verified against a real Neo4j
  with the old constraint pre-created.
- `refactor(4b)` `8b51c40`: closed the remaining security LOWs and recorded the ADR-008 residuals list
  (14 items — see ADR-008 §Security Sign-off; not restated here).
- `refactor(4b)` `afbf353`: closed reviewer findings on the rearchitecture.

Security **PASSED** after mutation-killing all four `_resolve_one` branches and verifying the
constraint fix against a real Neo4j. The disparate-impact problem the five heuristic rounds kept
re-opening (a filter tuned to protect Anglo-shaped names necessarily discriminates against others) is
gone in full, because there is no longer a shape heuristic to be biased — full stop.

## The ranking cost — measured against a real Neo4j, then fixed

`ranking-evals` did something the Phase 4a hardening rounds, working against an idealized engine
replica, could not: it **projected the actual 4a corpus through 4b's real code into a real Neo4j** and
scored the result. Two findings came out of that run, both new because they only exist against real
data:

**1. Spelling-recall collapse.** ADR-008's residual #6 (non-vocab skills lose auto-merge permanently)
measured worse than expected once real: **37.5% spelling recall** across the corpus's skill claims.
The costliest single case — `REST APIs` (résumé) vs `REST API design` (JD) failing to resolve to the
same node — cost a strong candidate **−0.144 on `score_final`**, more than `education` (0.0391) +
`overqual` (0.0120) + `motivation` (0.0900) *combined*, enough on its own to drop a qualified
candidate out of the k=5 shortlist. Root cause: `_basic_normalise` didn't strip trailing version
tokens (`PostgreSQL 14` ≠ `postgresql`) or parenthetical qualifiers before computing the key, so a
vocabulary hit on one side and a miss on the other left `REQUIRES`/`HAS_SKILL` pointing at different
nodes for what is obviously the same skill.

**Fixed** in `feat(4b)` `56aecd5`: `_basic_normalise` now strips trailing version tokens and
parenthetical qualifiers **before** the canonical key is derived, applied symmetrically on both the
JD and résumé sides, plus a small, judgement-call set of new alias entries (`psql`, `docker compose`,
`kafka streams`, a new `rest api design` concept covering `rest api`/`rest apis`/`restful api`/
`restful apis`). Measured effect on the specific divergence class this fix targets: recall **40% →
100%**, verified 5/5 required + 3/3 nice-to-have skills meeting at the same node. This is a partial
fix, not a general one — see ADR-008 residual #6/#8 for the corrected vocabulary counts (147 concepts,
~229 spellings) and the honest statement that a vocab skill spelled a way the normalisation still
doesn't recognise keeps missing, same as any non-vocab spelling.

**2. `_basic_normalise` was duplicated, not shared.** The same function had been copied into two
modules "byte-identical by convention" — it wasn't, in fact, kept in sync, which is exactly the kind
of drift that could silently reopen the spelling-recall fix in one module while leaving it fixed in
the other. `refactor(4b)` `4481005` deduplicates it into a single shared object with a parity test
across both call sites.

**3. Parenthetical-split skill inflation — gated.** The parenthetical fallback the spelling fix
introduced (extract skill from `"X (Y)"` when the outer phrase doesn't resolve) could, unguarded,
extract a real vocab skill out of a name-bearing string: `"Casey Rivera (Python)"` → `python`,
`"Rivera (psql)"` → `postgresql`. Not a new *PII* residual (the hash/cleartext-vocab split already
covers any name that reaches the resolver), but a mis-extracted name minting a spurious `HAS_SKILL`
edge is a scoring-integrity hazard the corpus can't see. `refactor(4b)` `429adc7` (branch tip) closes
this with `_outer_phrase_is_vocab_adjacent`: the parenthetical's content is only used as a fallback
when the outer phrase (parens stripped) is empty or itself shares a vocab-table token — a name-bearing
outer phrase (`"Casey Rivera"`, `"Rivera"`, `"Casey"`) shares none, so the fallback never runs for it.
Same commit widens the ADR-008 residuals list to record the gate's honest limits (it's a token-overlap
check, not semantic — an adversarial phrase that happens to contain a vocab word as a substring could
still unlock its parenthetical).

## Final gate state — HEAD `429adc7`

- ruff / black / `mypy --strict` clean.
- **1739 unit tests @ 97.04% coverage** (up from 1040 on `main` before this branch).
- **82 integration tests** vs real Postgres + Neo4j (up from 65).
- `run_evals.py` still exits 1 — correct pre-4c RED state; the harness's metric computation only wires
  in once 4c lands the orchestrator.
- **All three merge-blocking gates green on final HEAD:** security **PASS**, reviewer **APPROVE**,
  ranking-evals **PASS**.

## Carried into 4c as blockers

`ranking-evals`, running the 4a corpus through 4b's real graph for the first time, surfaced four items
that block the 4c PR — full detail in [docs/EXTRACTION_PLAN.md](../EXTRACTION_PLAN.md), "4b → 4c
BLOCKERS":

1. **`missing_must` must key off `ontology_weight == 0`, not `score == 0.0`.** With `categories.yaml`'s
   family-credit ontology now live, a family-credited miss scores 0.5 (never 0.0), so
   `must_have_miss_penalty` never fires for the case it exists to catch. Measured: a candidate with
   **no Airflow at all** scored **+0.1120 on `score_final`** relative to correct, 83% of a perfect
   match on a must-have they don't hold.
2. **R1 (Phase 4a's accepted residual — the corpus is blind to the skill sub-score's internals) is now
   LIVE, not hypothetical.** 4c needs a must-have-miss twin and a recency twin (R1 already flagged
   these as needed; this session's real-Neo4j run confirmed the underlying bug is real and shipped,
   not theoretical).
3. **A spelling-divergence twin is needed.** The corpus's highest-weighted sub-score (skill, 0.40)
   has no fixture that isolates a spelling-recall miss — this session's own headline number (−0.144)
   is the sharpest instance of a gap the corpus otherwise can't see.
4. **`canonical_name` → `canonical_key` rename.** hris's stage-2 Cypher reads
   `reqSkill.canonical_name`; ADR-008 renamed that property. A verbatim port fails loud
   (`SkillContribution.skill: str` ← `None` → `ValidationError`) rather than silently mis-scoring, but
   it costs a debugging session if not caught on day one of the port.

## Accepted residuals (full list in ADR-008)

Not restated in full here — 14 items, see [ADR-008](../adr/008-skill-graph-pii-by-construction.md)
§Security Sign-off. The two most load-bearing for anyone building on 4b: the **vocabulary (147
concepts / 231 spellings) is now the single ranking bottleneck** — growing it is the only lever that
improves non-vocab recall, since auto-merge no longer affects scoring at all — and **a candidate whose
name collides with a vocabulary term** (`julia`, `hudson`, `kafka`, `django`, `cassandra`, and the
wider set of spellings the version-strip/paren-split normalisation now routes to the same collision
node) gets a deniable-but-cleartext `canonical_key`.
