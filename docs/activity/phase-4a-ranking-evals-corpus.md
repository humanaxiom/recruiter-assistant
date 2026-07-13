# Phase 4a — Ranking-evals corpus

**Status:** complete, all three merge-blocking gates green on HEAD `e8e83be`
(reviewer APPROVE, security PASS, ranking-evals PASS). Branch
`feat/phase-4a-ranking-evals-corpus`. **Zero product code** — this sub-phase
builds only the labelled evaluation corpus the Phase-4c matching engine will be
scored against, so that 4c's first `ranking-evals` run is falsifiable rather
than a rubber stamp.

## What landed

`core/tests/evals/`:
- `fixtures/jd_backend_data_engineer.json` — the job description all resumes are scored against.
- `fixtures/resumes/r01..r16.json` — 16 synthetic labelled résumés (validated against the real
  `src.schemas` shapes — `JDExtracted` / `ResumeParsed` — so a fixture that drifts from the parse
  contract fails collection).
- `fixtures/labels.json` — per-résumé `tag` (strong / borderline / weak / adversarial),
  `expected_rank_band`, `gold_evidence`, `must_not_surface_in_topk`, and the matched-pair
  `ordering_controls`.
- `thresholds.toml` — the gate thresholds: `precision_at_k` (k=5, min 0.8), evidence
  verification rate = 1.0 with `fuzz_threshold` asserted == `DEFAULT_WEIGHTS.evidence_verify_fuzz`
  (0.85), adversarial `must_not_surface_in_topk`, PII-leak policy, and a determinism gate
  (`max_score_delta = 0.0`, `temperature = 0.0`).
- `run_evals.py` — the harness stub. Loads the corpus, guards the (not-yet-existing)
  `src.pipeline.matching.orchestrator` import, and **fails loud** (exit 1) with a pointer to Phase 4c.
  This RED-pending-4c state is correct for 4a: the corpus is proven valid independently by
  `tests/unit/test_evals_corpus.py`; the metric computation wires in when 4c lands the orchestrator.

`core/tests/unit/test_evals_corpus.py` — 226 tests that make the corpus self-verifying: bidirectional
label↔fixture consistency, schema validity, tier-population↔rank-band feasibility, the adversarial
fabrication trap, gold_evidence exact-substring anchors, PII synthetic-marker guards, and the
matched-pair twin-integrity checks.

## Corpus shape

16 fixtures: **7 strong** (r01,02,03,11,13,14,15) / **4 borderline** (r04,05,10,16) / **4 weak**
(r06,07,08,12) / **1 adversarial** (r09). Rank bands are derived FROM the tier counts so they tile
ranks 1..16 with no gap/overlap: strong `[1,7]` < borderline `[8,11]` < weak/adversarial `[12,null]`.

## Two adequacy gaps closed during the build (round-2 strengthening, `52ee245`)

1. **Infeasible rank bands (GAP 1).** The round-1 corpus shipped `strong=[1,3]` while holding 5
   `strong` fixtures — a band no correct ranker could satisfy. Bands are now computed from live tier
   populations, and `test_expected_rank_bands_fit_tier_populations` re-derives the required window
   widths from the actual tag counts each run, so a future tag change that breaks feasibility fails
   loudly.
2. **Within-tier-only dimension signals (GAP 2).** r11 (education), r13 (overqual), and r04
   (motivation) each only moved score *within* a tier, so a ranker that ignored those dimensions
   still passed every tier-level invariant. Added **twin fixtures** that isolate one dimension each:
   - **r14 ↔ r11** — CS vs Mechanical-Engineering degree → isolates `education_partial`.
   - **r15 ↔ r13** — 6 vs 14 years (ratio 1.2 vs 2.8 against the JD's 5-yr minimum) → isolates `overqual_ratio`.
   - **r16 ↔ r04** — cover letter present vs absent → isolates `motivation` (0.1 top-level weight).

   Each twin is identical to its partner in every scoring-relevant field except the target dimension,
   enforced by per-pair integrity tests. The pairwise `rank(higher) < rank(lower)` assertion itself is
   a Phase-4c test (needs the live ranker); 4a only guarantees the twins are genuinely "identical
   except X" so the eventual 4c assertion is unconfounded.

## Merge-blocking gate findings and fixes

First gate pass: **security PASS**, **reviewer REJECT**, **ranking-evals CHANGES-REQUIRED**.

- **Embedded-`summary` confound (ranking-evals, real — fix `e8e83be`).** `_build_summary_text`
  (`src/worker/resume_tasks.py`) embeds a résumé's `summary` into `summary_emb` — the stage-1 vector
  recall input and the 0.10 vector sub-score. The education and overqual twins narrated their target
  dimension in `summary`, so the summary was a second (or, for overqual, the *sole*) embedded
  differentiator — a no-op overqual penalty could still satisfy 4c's ordering assertion via the vector
  path. Fixed by making each twin pair's `summary` byte-identical and dimension-neutral (mirroring
  r04/r16, already clean), adding a `summary`-equality assertion to all three twin tests, and
  correcting the `labels.json` rationales that over-claimed "every other field matches." ranking-evals
  re-audited and mutation-confirmed the new guard fires.
- **Dead `# type: ignore[assignment]` in `run_evals.py` (reviewer MAJOR, real — fix `e8e83be`).**
  `run_match` is `Any` once the guarded import's `[import-not-found]` is suppressed, so the
  `[assignment]` ignore was unused under `mypy --strict`. It slipped the standard gate because
  `make gates` runs `mypy src` only (test files aren't type-checked). Removed; the load-bearing
  `[import-not-found]` on the orchestrator import stays.
- **False-CRITICAL (reviewer, not a defect).** The first reviewer run reported r14 Kubernetes
  `years=4` with the education twin test failing. This was a **concurrency race**: the ranking-evals
  gate was mutation-testing that exact field (bumping 3→4) on the *shared working tree* while the
  reviewer read it. Committed state is `years=3` for both r14 and r11, tree clean, test passing.
  **Process lesson:** never run a mutation-testing gate concurrently with a reviewer on the same
  working tree — re-gate sequentially after a fix.

## Gates (offline, container)

ruff · black · `mypy src --strict` clean; **955 unit tests @ 96.63% coverage** (incl. 226 corpus
tests); `run_evals.py` exits 1 (expected RED-pending-4c). No product code changed, so the src
coverage number is unmoved from Phase 3.

## Carried into 4b/4c

- 4c wires the live orchestrator into `run_evals.py::_run_corpus` and turns the harness green; the
  pairwise twin-ordering assertions become live 4c tests then.
- `run_evals.py` documents that the 4c integration point must run the PII-leak check (candidate
  name/email/phone must never reach embedding text or exported output).
- Pre-existing tech debt (not 4a's, not gated): `mypy src tests --strict` surfaces ~18 typing errors
  in Phase-3 test files (`test_worker_parse_resume.py` etc.) merged to `main`; `make gates` runs
  `mypy src` only. Worth a cleanup pass but out of 4a's scope.

---

## Round 3 — falsifiability hardening (`fix/phase-4a-corpus-falsifiability`, post-merge fix-forward)

Three opus-tier gates re-audited the corpus **after** it merged (PR #8) and reached the same verdict
from three directions: **the corpus as merged could not fail a bad Phase-4c engine.** Every finding
below was *proven* by a mutation that left all 226 corpus tests green. Landed before 4b/4c, zero
product code (`core/tests/evals/**`, `core/tests/unit/test_evals_corpus.py`,
`.claude/agents/ranking-evals.md`, docs).

| # | Finding | Mutation that stayed green | Fix |
|---|---|---|---|
| A | `min_precision = 0.8` at `k = 5` tolerates one bad entry — an engine ranking r09 **at rank 5 passes** the metric built to catch it; contradicts the file's own prose and `[adversarial].must_not_surface_in_topk`. The test only range-checked `0 < p ≤ 1` | `0.8 → 0.2` | `min_precision = 1.0`; `k` and `min_precision` **pinned to exact values** |
| B | The bait's potency was **unasserted** — the toml promises r09 is caught "however high its keyword overlap" and labels.json claims that overlap is the corpus's highest; nothing checked it | Defang r09 to a single ungrounded `Python` (`years: 1`, `last_used_year: 2005`) | r09 asserted structurally **top-tier on every non-evidence signal**; only evidence verification may reject it |
| C | The toml grew `[adversarial]` + `min_completeness_in_topk`; **neither consumer** (agent doc, `run_evals.py` docstring) enumerated them. The matched-pair ordering controls existed only as **prose** | — (contract drift) | Key set is a **three-way contract**, test-enforced both directions; **`[ordering_controls]`** is now a real key |
| D | The r11/r14 education twins relaxed to *cited*-chunk equality, so the education chunk `c_005` differed — but every chunk is embedded and evidence-retrieved, so r14 could out-score r11 through the **0.3 evidence path** with `education_partial` a total no-op | — (structural confound) | `c_005` deleted from both; **full chunk-list equality** asserted, matching the other two pairs |
| E | PII email scan was a **6-domain blocklist**; the phone scan missed `(604) 555-1212` and bare 10-digit numbers (its `(?<!555-01)` lookbehind was dead code). r12 pinned its name only in the ADR-007-N1-**permitted** bullet surface, never in the §7-F1-**scrubbed** chunk surface. `[pii].allow_structured_fields` named no surface. F1-R format-divergent leaks had zero coverage | Plant `asalah@sfu.ca` / `j.smith@shopify.com` in chunk text; strip the name from r12's `c_003` | Scanners **inverted to allowlists**; r12's name pinned in `chunks[].text`; `[pii]` **surface-qualified**; **r17** added (name-in-`summary`, line-broken name, reflowed phone, bare email local-part) |
| F | `max_score_delta = 0.0` flakes or lies: no `seed` reaches Ollama, and the Redis embed cache makes a warm second run compare the **cache to itself** | — (vacuous pass) | Ranking-**order** stability (`max_rank_delta = 0`) is the zero-tolerance invariant; `score_final` gets a `1e-9` epsilon; `seed` + cache-state pinned as a **4c requirement** |
| G | Every `gold_evidence` anchor is an exact substring → verifies at 1.0 → `verification_rate_min = 1.0` is satisfiable by a verifier that **returns `True` unconditionally** | — (no negative control existed) | **`negative_evidence`**: fabricated quotes that MUST score below `fuzz_threshold`, on r09 *and* on fixtures that also carry gold anchors (so the verifier must **discriminate**) |
| H | Nothing executed `run_evals.py`; `load_corpus()` joined `FIXTURES_DIR` with an unvalidated labels-supplied path (an absolute RHS silently replaces the LHS in pathlib) | — | `main()`'s pre-4c non-zero exit is now **gated**; fixture paths `resolve()` + confined to `FIXTURES_DIR` |

Corpus is now **17 fixtures** (7 strong / 4 borderline / 5 weak / 1 adversarial).

**Gates:** ruff · black · `mypy src --strict` clean; **993 unit tests @ 96.63% coverage** (264 corpus
tests, up from 226). Coverage unmoved — still zero product code.

**Recorded, deliberately NOT done here:** no **outbox-shaped fixture** exists — nothing encodes what
the outbox payload is *allowed* to contain (no `candidate`, no `chunks[].text`, no `summary`). 4b
projects to Neo4j and must add it (recorded as a 4b requirement in `docs/EXTRACTION_PLAN.md`).
