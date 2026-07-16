# Resume-Ranking Extraction — Plan of Record

Extract the **resume-ranking feature** from `C:\repos\hris` into a bare-essentials, local-first app built on the [golden template](../USING_THIS_TEMPLATE.md). No review workflow, no JD-Harmonizer. Keep anonymization, evidence-backed ranking, shortlists, and exports.

## Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Assembly strategy | **Template-first port** — golden template as chassis; port clean pipeline modules near-verbatim; rewrite the service/route layer trimmed |
| 2 | Object storage | **Local filesystem** behind a thin `BlobStore` (put/get/delete). MinIO dropped — community edition archived 2026-04-25, source-only, no CVE fixes |
| 3 | Graph store | **Keep Neo4j** — load-bearing for vector recall (stage 1) + skill-graph scoring (stage 2); already in the template |
| 4 | v1 scope | Include cover-letter/motivation, reverse-match, a minimal web viewer, and **blind-review redaction ON by default** (reveal is opt-in + audited) |

## What we keep vs cut

**KEEP (port):** parsing (`extract`/`chunk`, PyMuPDF/python-docx), LLM client + Redis embed cache, the 4-stage matching engine (`orchestrator`/`stages`), `MatchWeights`, PII encryption (`pii.py`, pgcrypto), display-time redaction (`redaction.py`), trimmed `shortlist_service` (persist/list/get/export), exports (csv / evidence-csv / json with `reveal`), schemas (`resumes`, `matching` minus review types, `jobs`), the arq jobs `parse_job` / `parse_resume` / `project_to_graph` / `shortlist_job` / `reverse_match_job`, Neo4j bootstrap (4× 768-d cosine indexes + skill graph).

**CUT:** review workflow (stages, SLA, next-action, approvals, comments, assignments, notifications, email), JD-Harmonizer / JD-Bank / Taleo, the Next.js `apps/web`, CAS (replace with minimal auth). Keep only `jd_import_service.extract_jd_text` (used by plain job creation).

**Decoupling surgery (two files):** `shortlist_service.py` — drop `record_decision`/`transition_stage` and the review sub-selects in the read/export SQL; `routes/shortlist.py` — drop decision/stage endpoints, the `collab_service` import, and review CSV columns.

## The ranking algorithm (ported verbatim)

1. **Coarse recall** — Neo4j `db.index.vector.queryNodes('resume_summary_idx', …)`, per-job scoped, 3× oversample → k=50
2. **Structured score** — `0.40·skill + 0.25·exp + 0.10·edu + 0.15·seniority + 0.10·vector`; skill via `REQUIRES`/`HAS_SKILL` graph with ontology partial-credit, years/recency weighting, must-have-miss penalty
3. **Evidence** — LLM per-requirement evidence, then **anti-fabrication verify**: every quote fuzzy-matched (≥0.85) against its cited resume chunk; unverifiable quotes blanked
4. **Combine + rank** — `0.6·structured + 0.3·evidence_completeness + 0.1·motivation` → `shortlist_entries` (Postgres) + `SHORTLISTED` edges (Neo4j)

Embeddings deliberately **exclude** name/email/phone (PII-equivalent). 768-d `nomic-embed-text`, cosine — must match the Neo4j indexes.

## Target layout (on the template)

```
core/src/
  storage/     # NEW filesystem BlobStore (replaces MinIO)
  models/      # asyncpg + idempotent DDL on startup: jobs, resumes, shortlist_entries, outbox
  schemas/     # resumes, matching (minus review), jobs
  pipeline/    # VERBATIM: parsing/{extract,chunk}, llm/{client,cache}, matching/{orchestrator,stages,config}
  services/    # pii, redaction, shortlist_service (trimmed), resume_service
  worker/      # arq jobs + neo4j_bootstrap
  api/         # routes: jobs, resumes, shortlist(generate/list/get/export)
  web/         # minimal Flask read-only viewer
```

Data access: **raw asyncpg + hand-written jsonb SQL** (port hris's proven queries), schema created via **idempotent DDL on startup** (template's "no migration framework yet" stance — no Alembic).

## Phased build — each phase = one pass through the TDD subagent pipeline

`make gates` must be green before the next phase starts.

| Phase | Deliverable | Status |
|---|---|---|
| **0 · Seed & infra** | Repo from template; compose (pg/neo4j/redis/ollama, no minio) + `data/` volume; settings (768-d, storage dir); DDL + Neo4j bootstrap on startup | ✅ done |
| **1 · Storage** | Filesystem `BlobStore` (put/get/delete/exists/list_keys, path-safe, `0o600`/`0o700`) + app/worker wiring | ✅ done |
| **2 · Schemas** | Port `resumes`, `matching` (minus review), `jobs` | ✅ done |
| **3 · Ingest + parse** | `extract`/`chunk`, LLM client+cache, `parse_resume`/`parse_job`, cover-letter parse, **PII encryption on parse** (incl. carried-forward criteria: strict `current_setting('app.pii_key')`, no `missing_ok`; per-field `max_length` on LLM string fields) | ✅ done |
| **4 · Ranking engine** | Split into 4 gated sub-phases (below) — each its own branch/PR, `make gates` + reviewer/security/ranking-evals green before the next | 🔄 in progress (started 2026-07-12) |
| &nbsp;&nbsp;**4a · Evals corpus** | `core/tests/evals/` labelled resumes-vs-JD fixtures + `thresholds.toml` (precision@k, evidence-verification-rate, PII-leak, determinism) — **zero product code**; built first so the matching engine's first green build is falsifiable | ✅ corpus done — merged to `main` via PR #8 (merge `875eac2`), CI green, 2026-07-12. **Falsifiability hardening also done** on branch `fix/phase-4a-corpus-falsifiability`, **merged to `main` via PR #10** (merge `464a479`), CI green, tip `583427f`, 18 commits. See "4a hardening" below. [activity](activity/phase-4a-ranking-evals-corpus.md) |
| &nbsp;&nbsp;**4b · Graph projection** | Outbox drainer `project_to_graph` (job+resume → Neo4j; **must NOT project `parsed.candidate` or log payload**; chunk-text preview read from `resumes.parsed`, NOT the outbox — ADR-007 stripped it) + Neo4j skill-graph half of `skill_normalize` (+ `categories.yaml`, ADR-008's canonical-key hashing) + the spelling-recall normalisation fix (`_basic_normalise` trailing-version/parenthetical handling). ✅ done — all three merge-blocking gates green (**1739 unit @ 97.04%**, 82 integration) on branch `feat/phase-4b-graph-projection`, tip `429adc7`, 20 commits, off `main` @ `464a479`. **Ranking-evals ran the 4a corpus through 4b's real code into a real Neo4j and found blockers for 4c — see "4b → 4c BLOCKERS" below (now CLOSED).** [activity](activity/phase-4b-graph-projection.md) | ✅ done — **MERGED via PR #11**, merge `68fe821`, CI green, 2026-07-15 |
| &nbsp;&nbsp;**4c · Matching engine** | `stages` (pure scoring fns) + `orchestrator` (stage 1–4) + `MatchWeights` settings wiring (`weights_from_settings`) + `shortlist_evidence_v1`/`_v2` prompts — opus-tier; first real `ranking-evals` gate. **Carried-forward determinism requirement (4a hardening F1): pin `seed`** on the eval path (`llm/client.py` passes only `temperature`/`num_predict` to Ollama today, and greedy decode is not bit-stable across batch/kv-cache splits) **and specify the embedding-cache state across the two determinism runs** (`llm/cache.py` caches by text hash, so a warm-Redis repeat run compares the *cache* to itself, not the model to itself, and the check passes vacuously). Ranking-**order** stability (`max_rank_delta = 0`) is the zero-tolerance invariant; `score_final` compares at `max_score_delta = 1e-9`. **All four 4b→4c blockers closed** (`missing_must` keyed off `ontology_weight == 0`; must-have-miss + recency skill-dimension twins added; `canonical_name`→`canonical_key` renamed) — see "4b → 4c BLOCKERS" below (CLOSED) and [ADR-009](adr/009-matching-engine-port.md). | ✅ done — gate-green on branch `feat/phase-4c-matching-engine`, tip `ed4a142`, 6 commits, off `main` @ `68fe821`, 2026-07-15. **NOT yet PR'd / NOT merged.** [activity](activity/phase-4c-matching-engine.md) |
| &nbsp;&nbsp;**4d · Shortlist + reverse-match jobs** | `shortlist_job`, `reverse_match_job` arq tasks + write-only `persist_shortlist`/`persist_reverse_match` + `match_resume_to_jobs` + worker wiring. (list/get/export → Phase 5). **Carried from 4c (ADR-009): wire `MatchingContext`/`weights` from `Settings` via `weights_from_settings` at the real call sites — 4c only proved the bridge in isolation.** | not started |
| **5 · Persist + anonymize + export** | Trimmed `shortlist_service`, `redaction` (blind-default), csv/evidence-csv/json export with `reveal`; **redaction MUST mask `candidate.*`/`candidate_name`/`cover_letter_text` before building `ResumeOut`/`ResumeListItem`** (schema can't enforce it — ADR-006 §4) | not started |
| **6 · API** | Routes: job create/parse, resume upload, shortlist generate/list/get/export, reverse-match; minimal auth. **Set `JobOut.blind_review` explicitly from the row** — the DTO defaults it `False` (fail-open) if a route omits it | not started |
| **7 · Evals + viewer** | Ranking-quality fixtures (precision@k, evidence-verification rate); minimal Flask viewer | not started |

## Subagent structure

Template roles (planner · tester · coder · reviewer · security · docs) plus two domain additions:

- **`data-pipeline`** — coder variant carrying the ranking domain contract (4-stage algorithm, Neo4j Cypher, asyncpg jsonb, 768-d invariant, PII-never-in-embeddings). Drives phases 1–6.
- **`ranking-evals`** — merge-blocking quality gate for pipeline phases: runs a fixture corpus (known good/bad resumes vs a JD) and gates on precision@k + evidence-verification rate. Unit tests prove code runs; this proves ranking *quality*.

Per-phase flow: planner → tester (+ evals fixture) → data-pipeline coder (ReviewLoop) → reviewer + security + ranking-evals (all merge-blocking) → docs. Security enforces PIPEDA/FIPPA, offline-egress, and the PII-not-in-embeddings invariant on every diff.

**Model tiering** ([docs/SUBAGENT_MODEL_POLICY.md](SUBAGENT_MODEL_POLICY.md)): cheap producers + strong verifiers. The three merge-blocking gates (`reviewer`/`security`/`ranking-evals`) run on **opus** and are never downgraded; producers (`data-pipeline`/`planner`/`tester`/`coder`) default to **sonnet**; `docs` on **haiku**. `data-pipeline` is overridden UP to `opus` per-call for the hard core (4-stage algorithm, evidence verifier, PII crypto, Neo4j scoring). Quality holds because every producer diff passes the opus gates + CI before merge.

## Current status & next step

**As of 2026-07-15 — Phases 0–3 are merged to `main`, CI green. Phase 4 (Ranking engine) is 🔄 IN
PROGRESS, split into 4 gated sub-phases** (planner pass 2026-07-12). Sub-phase **4a (evals corpus) is
COMPLETE and MERGED to `main`** via PR #8 (merge `875eac2`), CI green, 2026-07-12 (all three merge-blocking
gates green; corpus = 16 labelled fixtures + matched-pair dimension controls + `thresholds.toml` + a
RED-pending-4c harness stub — see
[activity/phase-4a-ranking-evals-corpus.md](activity/phase-4a-ranking-evals-corpus.md)). Its
**falsifiability hardening is also COMPLETE and MERGED**, via **PR #10** (merge `464a479`), CI green,
tip `583427f`, 18 commits. **Sub-phase 4b (graph projection) is COMPLETE and MERGED via PR #11**
(https://github.com/humanaxiom/recruiter-assistant/pull/11), branch `feat/phase-4b-graph-projection`,
tip `429adc7`, 20 commits, off `main` @ `464a479` — **merge `68fe821`, CI green, merged 2026-07-15.**
See [activity/phase-4b-graph-projection.md](activity/phase-4b-graph-projection.md) and "4b → 4c
BLOCKERS" below (now CLOSED). **Sub-phase 4c (matching engine) is COMPLETE, gate-green, and in PR #12** on branch
`feat/phase-4c-matching-engine` (https://github.com/humanaxiom/recruiter-assistant/pull/12), off
`main` @ `68fe821` — **all three merge-blocking gates green (security PASS, reviewer APPROVE,
ranking-evals PASS) AND CI (`gates-all`) fully green; PR #12 is OPEN, MERGEABLE/CLEAN, awaiting human
merge — NOT yet merged.** See
[activity/phase-4c-matching-engine.md](activity/phase-4c-matching-engine.md) and
[ADR-009](adr/009-matching-engine-port.md). **4d (shortlist + reverse-match write path) is the next
sub-phase to build**, once PR #12 is reviewed and merged; each sub-phase runs on its own branch/PR with
the full reviewer/security/ranking-evals gate before the next starts. The split mirrors Phases 0–3's
one-phase-per-PR cadence — Phase 4 is larger than Phase 3 (which took 4 audit rounds even scoped
tighter), and 4b/4c carry the security-sensitive PII-boundary + scoring-correctness surface, so
isolating them keeps each diff auditable.

**4a hardening — `fix/phase-4a-corpus-falsifiability`, merged to `main` via PR #10 (merge `464a479`),
zero product code.** Three opus-tier gates audited the merged corpus and found it **could not fail a
bad 4c engine**: every finding across nine rounds (rounds 1–2 on the original `feat/phase-4a-...` branch,
rounds 3–9 on this branch) was proven by a mutation that left the corpus tests green. Fixed fix-forward so
4c's first green build is genuinely falsifiable.

**Final verdict, HEAD `583427f` (round 9, the last round on this branch):** reviewer **APPROVE** (31 of 32
mutations killed across the whole branch; the one survivor is **R1**, a consciously-carried residual — see
"ACCEPTED 4a RESIDUALS" below — not an open defect); security **PASS** (empty findings table);
ranking-evals **PASS**. Offline gates: ruff · black · `mypy --strict` clean · **1040 unit tests @ 96.63%
coverage** (up from 955 on `main` before this branch — zero `core/src/` changes, so the delta is entirely
new eval-corpus tests) · **65 integration tests** vs real Postgres+Neo4j · `run_evals.py` still exits 1
(correct pre-4c RED state). **The single most important finding for 4c:** hris's `_fuzz_substring` — the
evidence verifier 4c is slated to port — verifies **all four** of the corpus's fabricated quotes and puts
the keyword-stuffer bait at rank 1; it **must be REPLACED, not ported** (see the dedicated section below).
- **precision@k pinned to its exact contract** (`k = 5`, `min_precision = 1.0`). `0.8` at k=5 tolerated
  exactly one bad entry — i.e. it PASSED an engine that ranked the r09 keyword-stuffer at rank 5 — and
  contradicted `[adversarial].must_not_surface_in_topk`. A range check let a `0.8 → 0.2` mutation stay green.
- **The adversarial bait's potency is now asserted** (r09 must be structurally top-tier on every
  *non-evidence* signal: all required + nice-to-have skills, `years ≥ min_years`, `recency_recent`
  bucket, clears `min_years_experience` without tripping `overqual_ratio`). **Only evidence verification
  may reject it** — a defanged bait is rejected by any scorer and the fabrication trap stops trapping.
- **`negative_evidence`** — fabricated quotes that MUST score below `fuzz_threshold`. Every
  `gold_evidence` anchor is an exact substring, so `verification_rate_min = 1.0` was satisfiable by a
  verifier that returns `True` unconditionally.
- **`[ordering_controls]` is now a real toml key**, not prose: the matched-pair assertions
  (r14>r11 education, r15>r13 overqual, r04>r16 motivation) are the corpus's most discriminating
  artifact and nothing forced 4c to implement them. The **education twins' chunk lists are now
  byte-identical** (the differing education chunk was embedded + evidence-retrieved, so r14 could
  out-score r11 through the 0.3 evidence path with `education_partial` a total no-op).
- **PII scanners inverted to allowlists** (every email-shaped match must be `@example.test`; every
  phone-shaped match must normalise to `555-01xx`) — the old 6-domain blocklist passed
  `<user>@<real-university>.ca` and every corporate/university/ISP domain. The `[pii]` structured-field
  exemption is **surface-qualified**
  (ADR-007 N1 exempts the *outbox/at-rest payload only*; embedding input and exported output must be
  PII-free **regardless of originating field**), and **r17** is the ADR-007 **F1-R** regression control
  (name in `summary`, line-broken name, reflowed phone, bare email local-part) — the residual that took
  Phase 3 four audit rounds to close had zero eval coverage.
- The **`thresholds.toml` key set is a three-way contract** with `.claude/agents/ranking-evals.md` and
  `run_evals.py`; a test fails if any of the three drifts.

**4a hardening, round 4 (same branch).** A re-audit found the round-3 hardening had itself shipped an
unasserted claim stamped as asserted. Fixed:
- **The `[adversarial]` arm was INERT.** r09 held a sub-bachelor `Diploma, General Studies`, failing the
  JD's `min_level: bachelors` on its own — so a MatchWeights-faithful engine with a **no-op evidence
  verifier** still dropped it to rank 8 (outside k=5) and **passed** `must_not_surface_in_topk` *and*
  `precision@5 = 1.0`. The potency test asserted 3 of MatchWeights' 5 structured sub-scores and omitted
  the two on which r09 was weak (education 0.10, vector 0.10). r09 now holds a **JD-allowed BSc** and all
  five sub-scores are asserted.
  > **Every number round 4 claimed for the repaired bait was wrong — superseded by round-5 F1** (and by
  > round-7 M-1, which found the three surviving copies of it disagreeing with each other). Round 4 wrote
  > "the repaired bait puts a no-op-verifier engine at precision@5 = 0.80 → FAIL", and "`0.6·structured +
  > 0.3·0 + 0.1·0 ≈ 0.547` lands the bait adjacent to the borderline tier (rank ~11, vs the 0.844 top-5
  > cutoff)". None of that was measured. Round 5 **measured** the same corpus state: seniority **0.271** →
  > r09 **rank 8** → **precision@5 = 1.00** → *a bad engine still PASSED*. Round 4 did not close the bait
  > hole; it **relocated** it from education (0.10) onto seniority (0.15). The three files that carried
  > this figure each stated a *different* false rank for the one state ("rank 2", "3rd", "~11/17"); all
  > are now marked wrong in place rather than quietly re-tuned. What was *right* is the shape of the
  > knock-on argument, and it survives — see the next bullet.
- **Knock-on, re-derived not papered over.** A bait that is top-tier on every non-evidence signal and
  scores zero on evidence lands **just below the strong tier** by construction, not below every
  honestly-weak candidate — so `weak` and `adversarial` **no longer share a rank band**, and the
  band-feasibility check is now a full **Hall's-condition** test (the "bands must tile 1..N" check cannot
  express an overlapping band). (Current measured figure, post-round-5: score ≈ **0.597** vs the **0.785**
  top-5 cutoff — ~0.19 of margin. The bait's *exact rank* is deliberately not written anywhere; see
  "Stale figure reconciled" below.)
- **The three-way key-set contract was enforced in zero directions** against the two consumer docs (only
  the toml ↔ a list literal inside the test file was checked, and the test the comments named did not
  exist). It now reads both docs and asserts set equality. `[evidence].min_completeness_in_topk` — the
  one key whose job is to stop `verification_rate_min = 1.0` passing vacuously — was the last unpinned
  numeric threshold; it is pinned.
- **PII scan scoping + shape.** The fixture scan enumerated *filenames*, so any new non-resume fixture
  (4b's outbox-shaped fixture, 4d's reverse-match JDs) would never be scanned — it now globs the
  directory. The email scanner required `local@domain` **contiguous**, which is exactly backwards for a
  corpus whose thesis (r17 / ADR-007 F1-R) is that *format-divergent* identifiers are the leak class that
  matters; whitespace around the `@` is now tolerated and stripped, and the phone scanner learned the
  unicode dashes and `/` a real PDF paste carries. Both scanners are now themselves gated by probe tests.

**4a hardening, round 5 (same branch) — the first round that read the ENGINE.** Rounds 1–4 hardened the
corpus against an *idealized* algorithm. Round 5 ported the one 4c actually extracts (hris
`packages/pipeline/src/pipeline/matching/{stages,orchestrator}.py`) and found **two of `MatchWeights`' five
structured sub-scores do not compute what their names imply.** Both holes existed *only* against the real
code, which is why three prior audits missed them:
- **`seniority` (0.15) is not a years check.** `orchestrator.py:331-340` computes
  `cosine(jd.title, most-recent role title)`, rescaled from `[seniority_floor, 1]` → `[0, 1]`.
  `score_experience` is the **only** sub-score that reads years. But `thresholds.toml` and the potency test
  justified **both** `experience` (0.25) and `seniority` (0.15) with one years-based claim — so the corpus
  asserted `experience` **twice** and `seniority` **never**, while r09 carried `"title": "Software
  Professional"`, the most JD-distant title of any non-weak fixture. Measured (faithful engine + **no-op
  evidence verifier**): seniority 0.271 → r09 rank 8 → precision@5 = 1.00 → **a bad engine passes**. The
  round-4 fix had **relocated** the bait hole from education (0.10) onto seniority (0.15), not closed it.
  r09's most-recent title is now the **JD title verbatim** — also the most realistic keyword-stuffer
  behaviour — so `cosine(x, x) = 1.0` and seniority is **exactly 1.0 under any embedder, by arithmetic**.
  That matters: `Senior Backend Engineer` measured **0.755** on one nomic-embed-text build and **0.581** on
  another, straddling the **0.638** break-even at which the trap arms — a merely-plausible title would leave
  the corpus's most important guard dependent on the embedding model. A no-op-verifier engine now ranks the
  bait **1st** (precision@5 = 0.80 → FAIL).
- **`education` (0.10) reads the degree LEVEL only** and never `jd.education.fields`, so the r14/r11
  education ordering pair (twins differing in *field*) asserted a mechanism that **does not exist** — both
  were `BSc` → `bachelors` → education = **1.00 for both**. It also passed an education-blind ranker through
  the **vector** path: `_build_summary_text` embeds `education[].degree` into `summary_emb`, so with
  `weights.education = 0.0` r14 *still* outranked r11 — the whole gap was vector. (This is the D1 confound
  round 3 thought it had closed by deleting the education *chunk*; the degree still rode in via the
  structured `education[]` entry.) The twins now differ in **level** (bachelor's vs a sub-bachelor
  associate), both fields are JD-allowed so the field cannot be a second differentiator, and the education
  signal (0.040 of `score_final`) **dominates** the ~0.0009 vector residual — which points at the *lower*
  twin, so the only way to order the pair is to implement the sub-score. The `weights.education = 0.0`
  mutation must FLIP the pair — a **review obligation on the 4c PR**, not a gate (see round-7 M-3 below).
  **Round 7 (R7-2) closed the hole this fix left open:** the residual's *sign* is the load-bearing half of
  the argument, it is **measured, not arithmetic**, and its inputs were unpinned. `_build_summary_text`
  embeds `{degree}, {institution} ({year})`, and the twin test pinned none of those three (the
  embedding-input test compares only the segment *before* `"Education: "`) — the twins even shipped
  *different* institutions. Rewriting r14's institution to `"Backend Data Engineering Institute of Python
  and Airflow"` flips the residual to **+0.0043**, gives the education-blind engine a separation of
  **+6.399e-04 ≥ `min_score_gap`**, and it then **PASSES** the pair on both input orders — the confound
  re-created, with every corpus test green. The twins now **share an institution and a year**, and the
  education dicts *and* the embedded `Education:` segment are asserted to differ **only in degree/field**.

**4a hardening, round 6 (same branch) — finding F5: two of the three ordering pairs did not gate their
dimension.** The pairwise contract was `rank(higher) < rank(lower)` and nothing more — and a rank
comparison is satisfiable by a **tie-break**. Measured against an engine made *blind* to each pair's own
dimension (real `nomic-embed-text` 768-d + real rapidfuzz + an engine replica of the ported hris
`stages`/`orchestrator`):

| pair | blind engine | twin separation | labels order | reversed order |
|---|---|---|---|---|
| education | `weights.education = 0` | −3.266e-04 (−8.716e-04 after round 7) | FAIL | FAIL |
| overqual | `overqual_ratio = 99` | **+0.000e+00 (exact tie)** | FAIL | **PASS** |
| motivation | `weights.motivation = 0` | **+0.000e+00 (exact tie)** | **PASS** | FAIL |

So a **motivation-blind engine passed the motivation pair** in the fixtures' natural order, and the
overqual pair failed only by tie-break luck. Root cause is the **mirror image of F2**:
`_build_summary_text` (`core/src/worker/resume_tasks.py`) embeds `summary`/`skills`/`experience`/
`education` and nothing else — *not* `total_years_experience`, *not* `cover_letter_chunks`, which are
exactly the fields the overqual and motivation twins differ in. Those twins' embedding input is therefore
**byte-identical**, their vector sub-scores equal to the last bit, no residual exists to break the tie,
and `stage4_combine`'s stable sort just inherits stage-1's `ORDER BY vec_score DESC` — arbitrary for
identical vectors. (The education pair is decisive on ranks alone *because* F2 kept a residual and aimed
it at the **lower** twin.)

Fixed in the **contract**, not the fixtures: `[ordering_controls].min_score_gap = 1e-6` is new, and the
assertion is now **`rank(hi) < rank(lo)` AND `score_final(hi) − score_final(lo) ≥ min_score_gap`**, so an
exact tie can never pass under any tie-break, on any input order. The correct engine's gaps
(**+0.0391 / +0.0120 / +0.0900**) clear it by four orders of magnitude. The alternative —
copying F2's inverted-residual trick into the other two twins — was **rejected**: it would re-introduce an
embedder-dependent magnitude, which is precisely the F1 lesson (*pin by arithmetic, not by measurement*).
The twins' byte-identical embedding input is now itself asserted, so the tie cannot later be "fixed" by
narrating years/motivation into a twin's `summary` (that would put the signal back into `summary_emb` and
re-create the F2 confound).

> **Round-7 correction (N-1).** Round 6 wrote that all three gaps "are **arithmetic**". Exactly one is:
> **overqual +0.0120 = `0.6·0.25·(1.00 − 0.92)`**, straight off `MatchWeights` and the twins' years — and
> it is also the *smallest*, hence the one that bounds `min_score_gap` from above, and the only one the
> corpus asserts. **education +0.0391** is an arithmetic `0.6·0.10·(1 − 0.5·2/3) = 0.0400` **less an
> embedder-measured vector residual** (~9e-04). **motivation +0.0900 = `0.1 × 0.9`**, where the `0.9` is
> the **LLM's measured confidence** on r04's cover-letter evidence — not a `MatchWeights` constant at all.
> The sandwich only ever needed the smallest, and the smallest is the arithmetic one.

> **Review obligation, not a gate (round-7 M-3).** The three blind-engine mutations (`weights.education =
> 0`, `overqual_ratio = 99`, `weights.motivation = 0`) are what prove these pairs gate their dimension —
> each must FAIL on **both** input orders. But **nothing in `thresholds.toml` or `run_evals.py` can run
> them**: they require the engine with *mutated* `MatchWeights`, which is a property of 4c's own test
> suite. Adding a toml key for it would be precisely the defect this branch keeps finding (a claim stamped
> as asserted, enforced by nobody). So it is stated plainly instead: **the 4c PR must carry these three
> mutations as tests, and the 4c reviewer must check that each fails on both orders.** Same for the
> `_fuzz_substring` replacement below — a requirement on the 4c *code review*, not a mechanical gate.

**Baseline battery (measured — round 6, re-measured unchanged in round 7 after the R7-2 fixture fix).**
Every arm scored against the *full* contract (precision@5 · adversarial · all three ordering pairs with
rank **and** gap), on both input orders, against a real `nomic-embed-text` 768-d embedder on a cold cache.
Only the faithful engine with a correct verifier passes:

| arm | p@5 | r09 rank | ordering pairs | verdict |
|---|---|---|---|---|
| keyword-overlap | 0.80 | 1 | all 3 ✗ (ties the twins) | FAIL |
| lexical tf-idf | 1.00 | 8 | all 3 ✗ | FAIL |
| **embedding** pure-vector (the engine's actual vector path) | **0.80** | **4** | all 3 ✗ | FAIL |
| faithful + **no-op** verifier | 0.80 | 1 | all 3 ✓ | FAIL (adversarial) |
| faithful + hris `_fuzz_substring` | 0.80 | 1 | all 3 ✓ | FAIL (adversarial) |
| faithful + **correct** verifier | 1.00 | 8 | all 3 ✓ | **PASS** |

(The round-5 report's "tf-idf pure-vector" row conflated two different engines and was not reproducible as
stated. A *lexical* tf-idf and the *embedding* pure-vector ranker are different baselines with different
failure modes — both fail, but the mechanism differs, and the embedding one is the one that matters
because it is the engine's own stage-1 signal. The rank of r09 under a lexical tf-idf is also
implementation-dependent (sublinear tf / idf weighting), which is a further reason not to gate on it.)

Round 7 re-ran the whole battery after unifying the education twins' institution (the R7-2 fix, the only
fixture change on that round): **every row above is unchanged**, the top-5 is unchanged, and the rank bands
did not move (0 violations, populations 7/4/5/1). The only measured deltas are r11's score (0.7578 →
0.7583, still rank 7) and the education pair's numbers — correct-engine gap +0.0397 → **+0.0391**, and the
education-blind separation **−3.30e-04 → −8.72e-04**, i.e. the inversion the fix depends on got *stronger*.

**Stale figure reconciled (round 6).** r09's *exact* rank under the correct verifier is **not gated and
not build-stable** — it is near-tied with r04 (0.596994 vs 0.596711, a 2.8e-04 spread whose **sign flips
between `nomic-embed-text` builds**), so "rank 9" (and the older "rank 11") is no longer written anywhere.
What is build-independent, and what the corpus actually gates: r09 ranks **below every strong fixture**,
i.e. outside the k=5 window, with **~0.19** of margin below the 5th-place cutoff.

**4c OPEN DECISION for a human — `jd.education.fields` is decorative.** The JD fixture declares
`education.fields: ["Computer Science", "Software Engineering", "Data Engineering"]`, and the ported
`stages.score_education()` **ignores them entirely** (it compares the degree *level* to `min_level`). So
field-relevance is currently dead weight in the contract. Two options, **not resolved here** — extending the
scorer is a *new requirement*, not a port, and the corpus must not smuggle one in:
1. **Extend `score_education`** to read `fields` (e.g. a non-allowed field earns only `education_partial`).
   New behaviour; needs its own ADR + tests.
2. **Drop `fields`** from the JD contract as unused.
The r14/r11 ordering pair is deliberately built to survive **either** choice (both twins' fields are
JD-allowed, so the pair turns on level alone).

**4a hardening, round 7 (same branch) — R7-1 / R7-2: the fifth consecutive round to find a claim *stamped
as asserted and enforced by nothing*.** Both fixes are one assertion each.
- **R7-1 — `SKILL_EVIDENCE_MARKERS` claimed coverage it did not enforce.** That dict is the **sole
  definition of "JD-relevant"** for the corpus's core falsifiable property (every non-adversarial fixture's
  JD-skill claims must be textually grounded; the adversarial one's must not be), for r10's recency guard
  and for r17. Its comment said it "covers every required_skill AND nice_to_have_skill name used anywhere
  in the corpus" — **nothing checked that**, and a JD skill with no marker is *filtered out* before either
  arm of the trap ever sees it. Three mutations stayed green: delete the `kubernetes` marker; delete it
  *and* re-ground r09's Kubernetes claim (defanging one arm of the fabrication trap outright); and — the
  one that matters for 4b/4d — **give the JD a nice-to-have `Redis` that both r09 and honest-strong r03
  claim with zero textual support**, so neither the "adversarial must be ungrounded" arm nor the "honest
  must be grounded" arm fires. Coverage is now **derived from the JD fixture**
  (`test_skill_evidence_markers_cover_every_jd_skill`): adding a JD skill without a marker is RED, and so
  is deleting a marker. This is the enumerate-instead-of-derive shape, sitting on exactly the surface 4b/4d
  touch when they add JD fixtures.
- **R7-2 — the education twins' `institution`/`year` were unpinned, so the F2 residual could be inverted
  back.** Detailed under round 5 above. One fixture changed (r11 adopts r14's institution); the full
  battery and the bands were re-measured and are unchanged.
- Also: the `min_score_gap` sandwich's "all three gaps are arithmetic" claim was false (**N-1**, corrected
  above); the "4c MUST run these mutations" lines were prose obligations dressed as mechanical ones
  (**M-3**, now stated as review obligations); `min_precision = 0.8` was said to be clearable by a random
  ranker "roughly half the time" when the hypergeometric answer is **39.5%** (**N-2**); the toml's
  reverse-direction key check walked only section tables, so a **top-level scalar key was invisible to the
  three-way contract** (**N-4**, closed); the four ported engine helpers' "if 4c changes them, these must
  change in the same diff" was enforced by nothing (**M-2** — there is now a test that imports the real
  `src.pipeline.matching.{stages,orchestrator}` *when they exist* and compares, skipping until 4c).

**Round numbering (round-7 N-3).** Rounds are counted **cumulatively** over the corpus's hardening history
— rounds 1–2 on `feat/phase-4a-ranking-evals-corpus`, rounds 3–7 on `fix/phase-4a-corpus-falsifiability` —
and that is the scheme `thresholds.toml`, `labels.json`, `test_evals_corpus.py`, `docs/activity/` and this
file all use. The branch's **commit** names count gate iterations *on the branch*, an offset of 2:
cumulative round 4 = `red|green(4a-hard-2)`, round 5 = `(4a-hard-3)`, round 6 = `(4a-hard-4)`, round 7 =
`(4a-hard-5)`. (Before round 7 the docs numbered the same rounds 2/3/4 while the corpus files numbered them
4/5/6, which made every cross-reference ambiguous.)

**Update after round 9.** Rounds 8–9 continue the same cumulative count (still on
`fix/phase-4a-corpus-falsifiability`) but the commit-suffix offset stops being a clean `-2`: round 8's fix
is `test(4a-hard-8)` and round 9's is `test(4a-hard-9)`. That's because round 8 also found and rebuilt a
mislabelled commit from the round-7 sequence — `49e85bf`, labelled `red(4a-hard-7)`, was **not actually
red** (311 passed / 0 failed; it gated a fix round 6 had already landed) — into two honestly-labelled
commits, `b996810` / `830965d`, `test(4a-hard-7)`, consuming suffixes between round 7 and round 8. The six
genuinely-red commits from rounds 3–6 are untouched, original hashes. **Three commits on this branch are
labelled `test(...)` rather than `red:`/`green:`** — the rebuilt round-7 pair plus round 9's `583427f` —
which is a **documented deviation from CLAUDE.md's mandatory TDD order**: each of these adds a guard that
passes against the unmutated tree and can only be shown red by *mutation*, not by an honest failing test
committed first. This was verified by the reviewer (who confirmed each guard is real, i.e. does fail under
its corresponding mutation) and is flagged here for the human rather than silently folded into the round-7
numbering.

**4a hardening, round 8 (same branch) — the eighth consecutive instance of the branch's signature
defect.** The reviewer found the **eighth** case of *a claim stamped as asserted but enforced by
nobody*: **r17's chunk `c_008` could be deleted with all 1040 tests still green.**
`test_r17_carries_every_adr007_f1r_format_divergent_variant` enumerated **three** of the four ADR-007
F1-R break shapes (line-broken name, reflowed phone, bare email local-part) and never checked the
**fourth** — the intra-token break `c_008` exists for. This is the round-3 finding-E3 pattern recurring:
stripping r12's name from `c_003` (round 3) left the suite green because the embed-boundary control
could be silently neutered; here it was r17's fourth break shape. Fixed with a **4th assertion**: a
chunk must carry the candidate email broken **inside the domain token**, with de-wrap reconstructing
`candidate.email`. Also found and fixed in the same round: `texts.extend(decoded)` — the de-wrap pass's
sibling in `_scan_texts` — was **ungated** (deleting it left the suite green); the e2e probe gained a
**joint-break-only** phone (`555\n1212`) that only the plain decoded pass can catch. Also, housekeeping:
commit `49e85bf`, labelled `red(4a-hard-7)`, **was not red** (311 passed / 0 failed — it gated a fix
round 6 had already landed); the last two commits of that sequence were rebuilt as `b996810` / `830965d`
with the honest label `test(4a-hard-7)`, leaving the six genuinely-red commits from rounds 3–6 untouched
with their original hashes. Commits: `0edc722` (docs), `6a24c10`, `23176cf`.

**4a hardening, round 9 (same branch, commit `583427f`, `test(4a-hard-9)`) — L1, the last finding.**
Security's last finding on this branch (**L1, low, non-blocking**): `_scan_texts`'s **raw-source pass**
(`texts = [raw]`) was the third and last of its three passes still ungated — replacing it with `[]` left
the suite green. Its unique coverage is PII in a JSON **key** or as a **non-string scalar** (e.g. a phone
stored as a JSON number), both invisible to `_string_values` (which recurses `node.values()` and collects
only `str` leaves). Closed rather than deferred because the `ResumeParsed.model_validate` backstop
**only covers résumé fixtures** — the scan is scoped by directory (round-4 B5), so it also covers
`labels.json`, the JD, and — per this very plan's 4b/4d rows — 4b's outbox-shaped fixture and 4d's
reverse-match JDs, **none of which are pydantic-validated**. All three `_scan_texts` passes (decoded,
de-wrap, raw) are now independently gated, each failing on its own distinct assertion. This was the
branch's last finding: **round 9 is the final round**, HEAD `583427f`, all three merge-blocking gates
green (see the verdict at the top of "Current status & next step").

**4c evidence-verifier requirement — the ported `_fuzz_substring` must be REPLACED, NOT PORTED.**
*(A review obligation on the 4c PR — see M-3 above. No gate in this repo can enforce it: `run_evals.py`
scores whatever verifier 4c ships, and a corpus cannot make a coder not-port a function.)*
`stages._fuzz_substring` — the verifier 4c is told to extract — is a **character-set overlap ratio**: it
slides a window over the haystack and scores `|{chars of window} ∩ {chars of needle}| / len(needle)`. Any
fluent English sentence of the right length scores ~0.9 against any other, because they share an alphabet.
Measured against this corpus's four fabricated `negative_evidence` anchors, it **verifies all four**:
**0.928 / 0.943 / 0.988 / 0.935**, every one ≥ the 0.85 threshold. An engine that ports it verbatim ships a
**fabrication verifier that verifies fabrications** — and it is caught only because
`[evidence].negative_evidence_must_fail` exists, which makes that the single most valuable check in the
corpus. End-to-end, a faithful engine wired with hris's own verifier ranks the r09 bait **1st** →
precision@5 = 0.80 → **FAIL**. Replace it with rapidfuzz `partial_ratio` or `token_set_ratio` (both measured
safe: the same negatives score **0.36–0.46**).

**4c evidence-verifier requirement — WHICH fuzz measure (measured against this corpus, do not re-litigate
at implementation time).** `evidence_verify_fuzz = 0.85` is a **`partial_ratio`** (or `token_set_ratio`)
threshold — the quote is a *span* of its cited chunk, not the whole chunk. Three further measures are
already known-broken for this job:
- `fuzz.ratio` scores the corpus's own **gold** anchors at **0.648 / 0.796** — below 0.85. An engine
  implementing "ratio" literally can never reach `verification_rate_min = 1.0`.
- `fuzz.WRatio` scores r02's **fabricated** negative anchor at **0.855 ≥ 0.85** — it *verifies a
  fabrication*. The corpus correctly fails such an engine via `negative_evidence_must_fail`.
- `partial_token_set_ratio` returns **1.000** on **2 of the 4** negative anchors.
The corpus's stand-in (`_best_partial_ratio`, stdlib `SequenceMatcher`) was cross-checked against real
rapidfuzz: every gold anchor ≥ 0.85 and every negative < 0.85 under **both**, minimum margin 0.392. It is
*stricter* than rapidfuzz on the similarity axis (Ratcliff–Obershelp ≤ LCS/indel) and *more lenient* on
the window axis; if an anchor is ever tightened until its margin is thin, re-check it against real
rapidfuzz rather than trusting the stand-in at the boundary.

### ACCEPTED 4a RESIDUALS — the class of wrong engine this corpus still lets through

Recorded at round 6 and re-confirmed still outstanding at the branch's final round (round 9), **deliberately
not fixed here**, so 4c/4d inherit them with eyes open rather than discovering them after a green
`ranking-evals` run. Each was demonstrated by a mutation of the engine replica that the corpus **passed**.
(For the avoidance of doubt: round 9's finding — the ungated raw-source pass in `_scan_texts` — was the
"ninth consecutive instance" of the branch's signature defect and it was **closed**, not carried forward;
it is not an accepted residual, it's in the round-9 writeup above.)

**R1 — the corpus is blind to the *internals* of the skill sub-score.** It gates the 0.40-weighted skill
score only through *coverage* (does the candidate claim the skill at all). Every one of these mutations
still **PASSES** the full contract:
- `must_have_miss_penalty: 0.5 → 1.0` (the missing-must-have penalty deleted);
- **recency decay disabled entirely** — even though `r10`'s `decision_point` is literally
  `recency_decay_stale_skills`. r10 has **no twin**, so nothing isolates recency and the label is
  *decorative*;
- the whole **implied-experience relief** path (`implied_experience_relief` / `implied_min_coverage` /
  `implied_seniority_factor`) — no fixture is positioned to fire or not-fire it;
- an ontology **"junk-bucket"** that grants 0.5 family credit to *every* missing skill;
- starkest: **`weights.skill = 0.0` — an engine that ignores skills entirely passes.**

The corpus's separation is carried by **evidence and vector**, not skill, because the weak fixtures are
weak on *everything* at once. **Human decision (made at the round-9 close of this branch): R1 is
deliberately CARRIED INTO 4c, not closed on this branch** — closing it needs skill-dimension twin
fixtures, which churns the rank bands (the same tiling that round 4's `[adversarial]` fix already had to
rework once). **4c requirement:** add matched-pair twins for the skill sub-score's internals (a recency
twin for r10 at minimum, and a must-have-miss twin), the same way r14/r15/r16 isolate
education/overqual/motivation — a `weights.skill = 0` mutation must FAIL.

> **CLOSED (Phase 4c, 2026-07-15).** The must-have-miss twin (`r18`) and the recency twin (`r19` vs
> `r10`) both landed; `weights.skill = 0`, `must_have_miss_penalty 0.5→1.0`, and disabled recency decay
> each now FAIL against the live orchestrator. See [ADR-009](adr/009-matching-engine-port.md) and "4b →
> 4c BLOCKERS" above.

**R2 — the corpus gates the evidence *verifier*, never the evidence *extractor*.** Stage 3 is
LLM-extract-then-verify; every eval assertion is on the verify half (`negative_evidence_must_fail`,
`verification_rate_min`). A stage-3 LLM that simply **fails to find** real evidence is caught only in the
limit — `min_completeness_in_topk` catches "no quote at all" — while a *mediocre* extractor that finds
some quotes and misses others shuffles freely inside the deliberately-wide tier bands. **4c requirement:**
an evidence-**recall** assertion against the `gold_evidence` anchors (each gold anchor's requirement must
come back `met` with a verified quote), not just an evidence-**precision** one.

### 4b → 4c BLOCKERS — ranking-evals against a REAL Neo4j graph projection (2026-07-14) — **CLOSED 2026-07-15**

**All six items below are CLOSED as of Phase 4c** (branch `feat/phase-4c-matching-engine`, tip
`ed4a142`). Full decision record: [ADR-009](adr/009-matching-engine-port.md); activity report:
[activity/phase-4c-matching-engine.md](activity/phase-4c-matching-engine.md). Summary of how each
closed (the original findings below are left intact as the historical record of what 4b's real-Neo4j
run found):

1. **`missing_must` off `ontology_weight == 0`** — CLOSED. `score_skill_breakdown` now stamps
   `reason="missing"` on a row when its (pre-contribution) `ontology_weight == 0`, and
   `missing_must` filters on `reason`, never on the built contribution's numeric `score`. Verified
   single-candidate on fixture `r18` (a pairwise rank/gap check is provably unable to gate this —
   ADR-009 §2 has the algebra) via `run_evals.py::_assert_must_have_penalty_fires_on_r18`.
2. **Two skill-dimension twins, not one** — CLOSED. `r18_casey_rivera_missing_must_have` (must-have-miss
   twin, vs `r01`) and `r19_jamie_okafor_recency_twin` (recency twin, vs `r10`) both landed; both
   independently make `weights.skill = 0`, `must_have_miss_penalty 0.5→1.0`, and disabled recency decay
   FAIL, closing Phase 4a's R1 residual for good (it is no longer merely carried forward).
3. **Spelling-divergence twin** — CLOSED, but not via a new dedicated fixture. The −0.144 swing that
   motivated this item was a **4b graph-normalisation** issue (`_basic_normalise` not folding
   `REST APIs` to the same key as `REST API design`), and was already fixed in 4b itself
   (`_basic_normalise` trailing-version/parenthetical stripping — see
   [activity/phase-4b-graph-projection.md](activity/phase-4b-graph-projection.md)). 4c's own r18/r19
   twins cover the skill sub-score's internals for 4c's own scoring surface; no further fixture was
   needed for this item.
4. **`canonical_name` → `canonical_key` rename** — CLOSED. `_stage2_skill_rows`' Cypher reads
   `reqSkill.canonical_key` from day one of the port, verified against a real Neo4j
   (`test_stage2_skill_rows_reads_canonical_key_not_canonical_name`).
5. **`categories.yaml` family-credit effect** — CLOSED (recorded, not a defect to fix): the mechanism
   itself (0.5 family credit for a same-family skill) is intended; item #1's fix ensures it no longer
   also silences the must-have-miss penalty.
6. **R1 now live, not hypothetical** — CLOSED per item #2: the ontology bug 4b's real-Neo4j run
   surfaced is fixed in 4c's `stages.py`, not merely fenced off by a fixture.

**4b (graph projection) landed** the outbox drainer + the Neo4j skill-graph half of `skill_normalize`
(ADR-008's canonical-key-hashing rework) + `categories.yaml` (**new in 4b** — no ontology/family-credit
data existed through 4a). Its merge-blocking `ranking-evals` gate did something the 4a corpus by itself
could not: it projected the 4a corpus through 4b's real code into a real Neo4j and measured 4c's
matching engine's ACTUAL cost against real data, not a hypothetical one. Recorded here, **blocking on
the 4c PR — do not fix any of this now**: the scorer (`stages.score_skill_breakdown`) does not exist in
this repo yet; these are requirements for whoever ports it.

**1. `missing_must` must key off `ontology_weight == 0`, not `score == 0.0`.** hris's
`stages.score_skill_breakdown` computes `missing_must = [c for c in must if c.score == 0.0]` to decide
whether `must_have_miss_penalty` (×0.5) fires. With `categories.yaml`'s family-credit ontology now live
(4b, absent through 4a), a **family-credited** contribution — the candidate lacks the exact skill but
holds another skill in the same curated family — scores **0.5**, never `0.0`. That makes `missing_must`
**structurally empty whenever a genuine miss happens to share a family with something the candidate
has**, so the penalty **never fires** for exactly the case it exists to catch. Measured: r04 and r16
(**who have no Airflow at all** — see `Cron scheduling` in `core/tests/evals/fixtures/resumes/
r04_morgan_lee.json` / `r16_rowan_castillo.json`) jump **+0.1120 on `score_final`**, of which **+0.1000
is the missing-must penalty being switched off**, landing at 83% of a perfect match on the must-have
they don't have. The borderline→strong margin **eroded 3.3×** (0.1616 → 0.0496). **Fix for the 4c
port:** `missing_must` must be computed from `ontology_weight == 0` (the candidate genuinely does not
hold the skill, nor a family relative it credits from) — never from the numeric score, which conflates
"fully missing" with "partially credited."

**2. Close R1 with TWO skill-dimension twins, not one.** R1 above already records that
`weights.skill = 0` passes today; this session sharpens what 4c must add:
- **A must-have-miss twin**, isolating finding #1: `weights.skill = 0` must FAIL, AND
  `must_have_miss_penalty: 0.5 → 1.0` must ALSO independently FAIL — proving the penalty is wired to the
  right condition, not merely present.
- **A recency twin for r10** (`decision_point: recency_decay_stale_skills` is decorative today — no
  fixture isolates it; R1 above), so disabling recency decay FAILS too.

**3. Add a spelling-divergence twin.** One fixture identical to r01 except `REST API design` →
`REST APIs` — the Phase 4b spelling-recall fix's own headline number: a **−0.144** swing on
`score_final`, the single largest sub-score delta measured this session, bigger than education (0.0391)
+ overqual (0.0120) + motivation (0.0900) **combined**. The corpus today cannot see a swing that size in
its highest-weighted sub-score (skill, 0.40) at all — R2 above already records that the corpus never
gates the skill sub-score's internals; this is the sharpest instance of that gap.

**4. `canonical_name` → `canonical_key` rename — a verbatim hris port breaks loud, not silent.** hris's
`_stage2_skill_rows` Cypher (`orchestrator.py:257`) reads `reqSkill.canonical_name AS skill`. ADR-008
renamed that property to `canonical_key` (Phase 4b) — `canonical_name` **no longer exists** on a `Skill`
node. A verbatim port of that Cypher returns `skill=None` for every row → `SkillContribution.skill: str`
→ pydantic `ValidationError` at scoring time. Loud, immediate failure — not a silent mis-score — but it
costs a debugging session if undiscovered until then, and it is exactly the class of hris-source-vs-
current-schema drift the "read `stages.py` first, not third" lesson (4a hardening, round 5, above)
exists to prevent. **4c must rename this Cypher alias in the port, day one.**

**5. `categories.yaml` is new in 4b — record what changed, not just that it exists.** Through 4a, no
ontology/family-credit data existed at all; `categories.yaml` (`core/src/pipeline/skill_data/
categories.yaml`) landed with 4b specifically to back stage-2's family partial-credit. Measured effect
on the 4a corpus once real: **+0.1120 on `score_final`** for a must-have-miss candidate (finding #1),
of which **+0.1000** is the missing-must penalty nullification and the remainder is the family-credit
mechanism doing its documented job (0.5 credit for a same-family skill is intended — the bug is only
that it also silences the penalty). R1 above already calls this an "ontology junk-bucket that grants 0.5
family credit to every missing skill" and lists it as a mutation the corpus **passes** — 4b did not
invent this failure mode, but it made it **real** (see #6).

**6. R1 (above) is now LIVE, not hypothetical.** R1 was written against an engine that did not exist in
this repo — "the corpus is blind to the skill sub-score's internals" described a class of future risk.
4b's `categories.yaml` + this session's real-Neo4j projection measured finding #1 as an ACTUAL,
already-shipped mechanism — the ontology R1 warned about is the exact one now seeded in `core/src/
pipeline/skill_data/categories.yaml`. Do not read R1 as "a mutation the corpus can't catch, in the
abstract" any longer: it is a live scoring bug waiting for 4c's `stages.py` port to inherit, unless
items #1–#3 above are addressed in that port's own PR.

**Phase-4 decisions adopted from the planner pass** (recommended defaults; reversible):
- **Chunk-text preview source (required deviation, Risk #1):** hris's `_resume_projection_tx` reads
  `chunk.text` off the outbox payload, but ADR-007 stripped `chunks[].text` from the outbox in Phase 3.
  4b MUST instead read chunk text from `resumes.parsed` (Postgres) inside the projection — a verbatim
  port would `KeyError`/write empty `ResumeChunk.text_preview`. Evidence citations (stage 3) likewise
  source chunks from `resumes.parsed`, never the outbox.
- **Reverse-match evidence depth** `match_reverse_evidence_k > 0` — recruiter-assistant runs reverse
  match only on the async `reverse_match_job` path (no synchronous endpoint to protect), so port hris's
  worker-path default, not its sync-timeout `k=0`.
- **`skill_category_task` (LLM category backfill cron) deferred** out of Phase 4 — `ensure_categories`'
  curated seed already covers stage-2 family partial-credit; revisit later if the corpus shows gaps.
- **NICE_TO_HAVE skills** contribute to stage-3 evidence text but NOT the stage-2 structured skill
  sub-score — hris's shipped behavior, ported verbatim and recorded (not "fixed") in the 4c ADR.
- **4d ships the write path only** (`persist_shortlist`/`persist_reverse_match`); `list_for_job`/
  `get_one`/`export_rows` + display redaction stay Phase 5 per the plan-of-record.

Per-phase TDD subagent loop as usual; `data-pipeline` runs on **opus** for 4b/4c (the 4-stage algorithm,
evidence verifier, Neo4j scoring) and **sonnet** for mechanical settings/plumbing. Full decomposition +
hris→target file map + risk table live in the planner output captured for this session.

**Phase 3 · Ingest + parse — complete and merged to `main` via PR #6 (merge `49196d7`), CI green**
(merged 2026-07-12). All three merge-blocking gates were green on final HEAD `c7b497e`
(reviewer APPROVE, security PASS, ranking-evals PASS) after **four rounds** of
findings-and-fix: round 1 and round 2 closed general security/reviewer findings and the DOCX/PDF
decompression-bomb + `RecursionError` + outbox-PII guards (F1–F6); **round 3** found **F1 (HIGH) —
candidate PII embeddable via the outbox's `chunk_embs`/`summary_emb` vectors** plus F2 (outbox
`summary` field), F3 (empty `PII_KEY` didn't fail loud), and F5 (`_extract_pdf`'s `needs_pass` read
could raise untyped and escape), all fixed with a `_redact_candidate_pii` embed-boundary scrub, an
outbox-summary drop, a worker-startup fail-loud check, and a wrapped `needs_pass` read; **round 4**
found **F1-R (MEDIUM) — a residual under-redaction** in the round-3 embed scrub (whitespace/format
divergence — line-broken names, reflowed phone numbers, bare email local-parts — could still leak into
embedded text), closed with a whitespace-flexible redaction pattern + email-local-part scrubbing.
Final offline gates: 729 unit tests, ~96.6% coverage; `black` pinned to `==26.5.1` for gate
reproducibility. Full rationale: [ADR-007](adr/007-phase3-ingest-parse-hardening.md); activity report:
[docs/activity/phase-3-ingest-parse.md](activity/phase-3-ingest-parse.md).

**Phase 2 · Schemas — complete and merged to `main` via PR #3 (merge `cefd545`), CI green** (merged 2026-07-11). Three pydantic **v2** modules plus an `__init__` re-export in `core/src/schemas/` — the contract layer Phases 3–6 code against (API DTOs, strict LLM `chat_json` schemas, jsonb shapes, ranking weights). `jobs.py` = job DTOs + `Skill`/`Education`/`JDExtracted`; `resumes.py` = parse shapes + resume DTOs + `_coerce_year`/`_drop_invalid_rows`/`_coerce_*` lossy validators; `matching.py` = `MatchWeights` (+ `DEFAULT_WEIGHTS`) + score/evidence/shortlist shapes. Pure data models — no I/O. Two boundaries held: (a) the **2nd-review workflow + Taleo/JD-comments are CUT** and not importable (`PipelineStage`/`DispositionReason`/`ShortlistDecision*`/`StageTransition*` deleted; `ShortlistEntry` drops `current_decision`/`current_stage`; `JobListItem` drops `comment_count`/`source`/`external_last_seen_at`; no `approval_required_2nd_review`) — a merge-blocking cut guard enforces it; (b) three **DDL-alignment deviations** — `created_by`/`uploaded_by` are `str | None` (nullable TEXT actor labels, no users table in v1), `JobCreate.blind_review` defaults `True` (blind-by-default, decision 4), no `approval_required_2nd_review`. `MatchWeights` is the ranking-weight contract (0.6/0.3/0.1 top; 0.40/0.25/0.10/0.15/0.10 sub; `evidence_verify_fuzz=0.85`; frozen; sums-to-1.0 validator). Two commits (red `1645178` → green `5bbf7c2`). Gates: offline green — ruff (no `--fix`), black, mypy --strict, **486 unit tests, 97.52% coverage**; all three merge-blocking gates passed (reviewer APPROVE, security PASS, ranking-evals PASS incl. a weight-validator mutation test proving `_sums_close_to_one` is real). The GREEN step was completed by the coordinator directly after a coder subagent hit a session limit mid-port (`matching.py` + `__init__.py` hand-authored from the extraction, re-verified by reviewer + evals). Full write-up: [docs/activity/phase-2-schemas.md](activity/phase-2-schemas.md); trim/DDL-alignment/redaction-boundary rationale: [ADR-006](adr/006-schema-port-trim-ddl-alignment.md).

**Phase 1 · Storage — complete and merged to `main` via PR #2 (merge `f7e7cbe`), CI green** (merged 2026-07-11). The filesystem `BlobStore` (`core/src/storage/blob_store.py`) is implemented and wired: async `put`/`get`/`delete`/`exists`/`list_keys` over `settings.storage_dir`, stdlib-only (`pathlib`/`asyncio`/`os`, IO via `asyncio.to_thread`), replacing MinIO. Security core is the `_resolve` path-traversal guard (rejects `..` segments, absolute/Windows-drive/backslash keys, empty/root/null-byte keys, and symlink escapes via realpath + `is_relative_to`), with blobs `0o600` and store-created dirs `0o700` for blobs-at-rest (PIPEDA/FIPPA). Wired onto `app.state.blob_store` (with a `get_blob_store` dependency) and worker `ctx["blob_store"]`; **no call site invokes it yet** — the upload/fetch/flush sites (`resume_service`, `resume_tasks`, admin/flush, routes) are ported in Phases 3–6. Four commits (red → green → red-harden → green-harden). Gates: offline green — ruff (no `--fix`), black, mypy --strict, **240 unit tests, 99.46% coverage**; all three merge-blocking gates passed (reviewer APPROVED, security PASS, ranking-evals PASS with a guard-mutation test proving the traversal guard is real). Full write-up: [docs/activity/phase-1-storage.md](activity/phase-1-storage.md); interface & path-safety rationale: [ADR-005](adr/005-filesystem-blobstore-interface-path-safety.md).

**Scope correction — the two carried-forward Phase-0 security criteria split across two phases.** Only **#1 (path-traversal)** was a Phase 1 concern and is **done** (above). **#2 (strict `current_setting('app.pii_key')`, no `missing_ok`)** is **not** a `BlobStore` concern — it belongs to **Phase 3** (`pii.py`), where the PII read path lands. It is in Phase 3's acceptance criteria below so it is neither lost nor wrongly attributed to Phase 1.

**Immediate next step — Phase 3 (Ingest + parse):** port `parsing/{extract,chunk}` (PyMuPDF/python-docx), the LLM client + Redis embed cache, `parse_resume`/`parse_job`, cover-letter parse, and **PII encryption on parse** (`pii.py`, pgcrypto) per Appendix A. Carry these acceptance criteria into Phase 3:
- **STRICT PII-key GUC read** (carried from Phase 0/1): wire `settings.pii_key` into `app.pii_key` with `current_setting('app.pii_key')` **without** `missing_ok=true` — a `missing_ok` read of an unset key yields NULL → NULL ciphertext → silent data loss; fail loud.
- **Per-field `max_length` on LLM string fields** (Phase 2 security low): add belt-and-braces caps on the free-text LLM-output fields at the ingest boundary.

Then Phase 5 carries the **redaction-boundary contract** (Phase 2 security, ADR-006 §4): `ResumeOut`/`ResumeListItem` can serialize decrypted PII with `blinded=True`, so Phase 5 redaction MUST mask `candidate.*`/`candidate_name`/`cover_letter_text` **before** DTO construction — the schema can't enforce it. And Phase 6 carries the **`JobOut.blind_review` fail-open** note (Phase 2 security low): the DTO defaults `blind_review` to `False`, so a route must set it explicitly from the row.

---

### Historical: Phase 0 status (for reference)

**Phase 0 was complete and green; Phase 1 was next at the time of this note.**

Done:
- Repo created: **`github.com/humanaxiom/recruiter-assistant`** (private). Local `origin` repointed here. Frozen template stays at `adamsalah13/agent-harness-template`.
- This plan + the `data-pipeline` and `ranking-evals` subagents committed to `main`.
- The 4 decisions above are locked.
- **Phase 0 · Seed & infra — merged to `main` via PR #1 (merge commit `8b2b47c`), CI green** (merged 2026-07-11). Seven commits + a merge commit (red → green → three review fixes → docs → ruff-pin fix `22abcb9`). CI (GitHub Actions) passed branch-name, `ruff·black·mypy`, `unit·coverage ≥ 80%`, and `integration (pg + neo4j + redis)`. The 7th commit — `22abcb9` "fix: pin ruff + declare src first-party so CI and local isort agree" — pinned `ruff==0.15.21` and added `known-first-party = ["src"]` to `core/pyproject.toml` after CI's ruff and the local container resolved different ruff versions (`requirements-dev.txt` only floor-pinned `ruff>=0.6.0`) and disagreed on first-party import grouping, failing the static gate with I001. What landed:
  - Template demo app removed; ranking-domain foundation in its place. Rebrand to `recruiter-assistant` across `pyproject.toml` / README / compose.
  - `docker-compose.yml`: pg/neo4j/redis/ollama, **no MinIO**, `./data` bind mount for the filesystem BlobStore.
  - `settings.py`: `llm_embedding_dim = 768` (single source of the contract), `storage_dir`, LLM base url, Neo4j creds.
  - **asyncpg idempotent startup DDL** (`init_schema`) for 5 tables (`jobs`, `resumes` (+PII BYTEA cols), `shortlist_entries`, `reverse_match_entries`, `outbox`); SQLAlchemy dropped from requirements.
  - **Neo4j bootstrap**: 4× 768-d cosine vector indexes (`resume_summary_idx`, `job_summary_idx`, `skill_emb_idx`, `chunk_emb_idx`) + skill-graph constraints, dimension derived from `settings.llm_embedding_dim`.
  - Three deliberate schema deviations from hris recorded in **ADR-004** (blind_review DEFAULT TRUE; nullable-TEXT actor cols; `score_final` unified to `DOUBLE PRECISION` with a 0–1 CHECK).
  - **Gates:** offline green — ruff / black / mypy --strict, 172 unit tests, coverage 88.79%. Integration green — 39 tests against real Postgres + Neo4j.

**Checklist reconciliation note (`core/src/gates/`):** the Phase 0 checklist said "keep `gates/`". The template demo's product-code gate-runner module (`core/src/gates/`, alongside `core/src/agents|memory` and `models/db.py`) **was deleted** — it was demo code the ranking domain replaces. "Keep gates" was (correctly) read as the **build** harness: `make gates`, CI, `.claude/`, and pre-commit are all intact. Don't mistake the deleted demo module for the still-live gate suite.

**(Phase 1 — Storage) — DONE** (see "Current status & next step" above): filesystem `BlobStore` (put/get/delete/exists/list_keys) replacing every MinIO call site — the `resume_service.py` put/remove and `resume_tasks._fetch_blob` get sites (Appendix A, "MinIO → filesystem") will call it when they land in Phases 3–6.

Security criteria carried forward from Phase 0's gate — **split across two phases** (correction to the original wording, which grouped both under Phase 1):

1. **Path-traversal rejection** — a **Phase 1** acceptance criterion, **DONE**. `blob_key` / `cover_letter_blob_key` are unvalidated `TEXT` and `storage_dir` is a bind-mounted root, so `BlobStore._resolve` rejects `..`, absolute paths, null-byte keys, and symlinks that escape `storage_dir` before any IO — landed in Phase 1's first commit (guard-mutation eval proves it).
2. **STRICT PII-key GUC read** — **NOT a Phase 1 item; moved to Phase 3** (`pii.py`, where the PII read path lands). When wiring `settings.pii_key` into the `app.pii_key` GUC, use `current_setting('app.pii_key')` **without** `missing_ok=true`. A `missing_ok` read of an unset key yields NULL → NULL ciphertext → silent data loss; fail loud instead. **This is a Phase 3 acceptance criterion.**

Then Phases 2–7 per the table above.

## Appendix A — hris source file map (minimal port set)

Exact paths in `C:\repos\hris` to port from (avoids re-exploring). Everything here is confirmed free of review/JD coupling except where noted.

**Schemas** (`packages/schemas/src/schemas/`): `resumes.py`, `matching.py` (drop review types: `PipelineStage`, `DispositionReason`, `ShortlistDecision*`, `StageTransition*`; drop `ShortlistEntry.current_decision/current_stage`), `jobs.py` (`Skill`, `JDExtracted`).

**Pipeline — port near-verbatim** (`packages/pipeline/src/pipeline/`): `config.py`; `llm/{client.py, cache.py}`; `parsing/{extract.py, chunk.py}`; `matching/{orchestrator.py, stages.py, config.py}`. (Do NOT copy `pipeline/bank/`, `pipeline/quality/`, `pipeline/sources/` — those are JD-Harmonizer.)

**Prompts** (`packages/prompts/src/prompts/templates/`): `resume_core_v1`, `resume_skills_v2`, `shortlist_evidence_v1` (+`_v2`), `cover_letter_v1` (+`_v2`) — each `.system.j2` + `.user.j2`.

**Worker** (`apps/worker/src/worker/`): `resume_tasks.py` (`parse_resume`, `project_to_graph`, `_build_summary_text` — the PII-exclusion point), `tasks.py` (`parse_job`), `shortlist_task.py`, `reverse_match_task.py` (v1 includes reverse match), `skill_normalize.py`, `skill_category_task.py`, `neo4j_bootstrap.py` (4 vector indexes, 768-d cosine: `resume_summary_idx`, `job_summary_idx`, `skill_emb_idx`, `chunk_emb_idx`; nodes Job/Resume/ResumeChunk/Skill/Company/Institution; rels HAS_CHUNK/HAS_SKILL/REQUIRES/SHORTLISTED), `main.py` + `config.py` (prune to ranking jobs only).

**API services** (`apps/api/src/api/services/`): `pii.py` (pgcrypto `pgp_sym_encrypt`, session key `app.pii_key`), `redaction.py` (`redact_text`, `pseudonym` — blind default ON), `resume_service.py` (upload/store/encrypt — **replace MinIO calls with BlobStore**), `match_service.py`, `shortlist_service.py` (**trim**: keep `persist_shortlist`/`list_for_job`/`get_one`/`export_rows`; drop `record_decision`/`transition_stage` and strip the `shortlist_decisions`/`stage_transitions` sub-selects & joins from read/export SQL).

**API routes** (`apps/api/src/api/routes/`): `resumes.py`, `shortlist.py` (**trim**: keep generate/list/get/export; drop `/decision`, `/stage`, the `collab_service` import, and review CSV columns), `jobs.py` (minus the JD-comments block; keep `jd_import_service.extract_jd_text` — used by plain job creation), reverse-match route.

**Infra glue**: `apps/api/src/api/minio_client.py` → **replace** with `storage/BlobStore`; `apps/api/src/api/{arq.py, db/session.py, config.py}` → adapt to template settings. Skill data: `infra/skills/{aliases.yaml, categories.yaml}`.

**Postgres DDL reference** (`apps/api/migrations/versions/` — read the raw SQL, re-express as startup DDL): `0002` jobs+outbox, `0003` resumes+PII (BYTEA name/email/phone, `candidate_email_hash`), `0004` `shortlist_entries` (split from `shortlist_decisions`), `0013` cover-letter cols, `0015` `reverse_match_entries`.

**MinIO → filesystem**: MinIO is used only in `resume_service.py` (put/remove) and `resume_tasks.py` `_fetch_blob` (get). Replace with `BlobStore.{put,get,delete}` over `./data/resumes/{id}`. The `resume-previews` bucket is unused — ignore it.

**Contract invariants (enforce in review/evals):** 768-d `nomic-embed-text` cosine everywhere (must match Neo4j indexes); embeddings exclude name/email/phone; evidence quotes verified ≥0.85 against cited chunk or blanked; weights only via `MatchWeights` from settings.
