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
| E | PII email scan was a **6-domain blocklist**; the phone scan missed `(604) 555-1212` and bare 10-digit numbers (its `(?<!555-01)` lookbehind was dead code). r12 pinned its name only in the ADR-007-N1-**permitted** bullet surface, never in the §7-F1-**scrubbed** chunk surface. `[pii].allow_structured_fields` named no surface. F1-R format-divergent leaks had zero coverage | Plant `<user>@<real-university>.ca` / `<name>@<real-employer>.com` in chunk text; strip the name from r12's `c_003` | Scanners **inverted to allowlists**; r12's name pinned in `chunks[].text`; `[pii]` **surface-qualified**; **r17** added (name-in-`summary`, line-broken name, reflowed phone, bare email local-part) |
| F | `max_score_delta = 0.0` flakes or lies: no `seed` reaches Ollama, and the Redis embed cache makes a warm second run compare the **cache to itself** | — (vacuous pass) | Ranking-**order** stability (`max_rank_delta = 0`) is the zero-tolerance invariant; `score_final` gets a `1e-9` epsilon; `seed` + cache-state pinned as a **4c requirement** |
| G | Every `gold_evidence` anchor is an exact substring → verifies at 1.0 → `verification_rate_min = 1.0` is satisfiable by a verifier that **returns `True` unconditionally** | — (no negative control existed) | **`negative_evidence`**: fabricated quotes that MUST score below `fuzz_threshold`, on r09 *and* on fixtures that also carry gold anchors (so the verifier must **discriminate**) |
| H | Nothing executed `run_evals.py`; `load_corpus()` joined `FIXTURES_DIR` with an unvalidated labels-supplied path (an absolute RHS silently replaces the LHS in pathlib) | — | `main()`'s pre-4c non-zero exit is now **gated**; fixture paths `resolve()` + confined to `FIXTURES_DIR` |

Corpus is now **17 fixtures** (7 strong / 4 borderline / 5 weak / 1 adversarial).

### Round 2 of findings-and-fix (`red(4a-hard-2)` → `green(4a-hard-2)`)

The round-1 hardening had itself shipped an unasserted claim stamped as asserted — the defect class the
branch exists to kill. Re-audited by all three merge-blocking gates; security returned **PASS**.

| # | Finding | Mutation that stayed green | Fix |
|---|---|---|---|
| B1 | **The `[adversarial]` arm was INERT.** r09 held a sub-bachelor `Diploma, General Studies` — it fails the JD's `min_level: bachelors` on its own, so a MatchWeights-faithful engine with a **no-op evidence verifier** scored it 0.788 → **rank 8**, outside `k=5`, and **passed** `must_not_surface_in_topk` *and* `precision@5 = 1.0`. Education alone is `0.10 × 0.6 = 0.06` of `score_final`, more than the **0.0485** gap to the top-5 cutoff. The potency test asserted 3 of MatchWeights' **5** structured sub-scores and omitted the two on which r09 was weak (education, vector) — while the toml, the agent doc and its own docstring all said "only the EVIDENCE verifier may reject it" | Give r09 a `BSc Computer Science` — **264 tests stay green**. Delete its education entirely — also green | r09 holds a **JD-allowed BSc** (chunk `c_006` narrates it); **all five** structured sub-scores asserted. Repaired, the no-op-verifier engine ranks r09 **3rd** → `precision@5 = 0.80` → the adversarial arm **correctly FAILS** it |
| B1↩ | **Knock-on, re-derived not papered over.** A bait that is top-tier on every non-evidence signal scores `0.6·structured + 0.3·0 + 0.1·0 ≈ 0.547` and *must* land **adjacent to the borderline tier** (measured rank **11/17**, one slot above the weakest borderline fixture) — so its old `expected_rank_band {min: 12}` and the `weak == adversarial` shared band were **arithmetically infeasible**. This is the round-1 infeasible-band bug pointed at a different tier | — | `adversarial` gets **its own band** `[8, null]` ("below every *strong* fixture, hence outside top-k" — the invariant that survives); `borderline` gains **exactly one** slot of slack `[8, 12]` for the rank the bait displaces. Feasibility check upgraded from "bands tile 1..N" to a full **Hall's-condition** test, which *can* express an overlapping band. 0.547 ≪ the 0.844 top-5 cutoff, so every gate still bites |
| B2 | The "three-way key-set contract enforced in **both** directions" **did not exist**: only the toml ↔ a list literal *inside the test file* was checked; nothing opened the agent doc or `run_evals.py`'s docstring, and `test_every_threshold_key_is_enumerated_by_both_consumers` — named in three places — **was not in the repo** | Delete the `[ordering_controls]` block from `run_evals.py`'s docstring **and** the agent-doc row; delete `[adversarial]` + `min_completeness_in_topk` from the docstring; add a new toml key with both consumer docs left stale | That test now **exists**, asserts `AGENT_DOC_PATH.is_file()` first (a bad path would pass vacuously), and compares **set equality in both directions** against both docs. The agent-doc table is **one row per key** — merged rows were invisible to the parse |
| B3 | `min_completeness_in_topk` was the **last unpinned numeric threshold** (range-checked only) — on the one key whose job is to stop `verification_rate_min = 1.0` passing vacuously | `1.0 → 0.2`, `1.0 → 0.01` | Pinned exactly |
| B4 | **Real email addresses committed as prose** in 6 places while documenting the PII fixes — including in the guard's own source file, which therefore carried exactly the strings its invariant bans | — (scanner scope is JSON fixtures only) | All 6 replaced with non-resolving placeholders that preserve the narrative |
| B5 | The fixture scanner **enumerated filenames** (`resumes/*.json` + a hardcoded JD + `labels.json`), so any *new* non-resume fixture is never PII-scanned — and 4b/4d add exactly that | Add `fixtures/jd_second_role.json` with a real email + phone — **264 tests stay green** | `FIXTURES_DIR.rglob("*.json")` — scoped by **directory**, never by filename |
| B6 | The email scanner required `local@domain` **contiguous** — but the corpus's whole thesis (r17 / ADR-007 F1-R) is that **format-divergent** identifiers are the leak class that matters. `<user> @<domain>.ca` with one space was **not flagged**; the `\n` case was caught only by luck | Plant a spaced address in a chunk | Whitespace allowed around the `@` and stripped before the allowlist check; `_PHONE_SHAPED_RE`'s separator class grew the unicode dashes and `/`. **Both scanners are now themselves gated** by probe tests |

Non-blocking, landed in the same change: the toml named the wrong fuzz measure (`fuzz.ratio` scores the
corpus's *own gold anchors* at 0.648/0.796 — an engine implementing that word literally can never reach
`verification_rate_min = 1.0`); the 4c scorer landmines are recorded in `EXTRACTION_PLAN.md`
(`fuzz.WRatio` **verifies** r02's fabricated anchor at 0.855; `partial_token_set_ratio` returns 1.000 on
2 of 4 negatives); `_best_partial_ratio`'s approximation **direction** and headroom are documented; chunk
ids must now be a **contiguous** one-based run (format alone was checked, and this change set had just
deleted a mid-list chunk from two fixtures); the phone separator class covers `‐‑–—―` and `/`.

Accepted residuals, stated rather than closed: `FAKE_NAMES` constrains `candidate.name` only — a real
**third party's** name in free text passes every scanner (needs NER); deliberate-evasion classes
(homoglyph domains, `[at]`/`(dot)`, base64) are out of threat model (accidental paste, not malicious
insider); the phone scanner is NANP-shaped; `@example.test` is matched with `endswith`, so a legitimate
*subdomain* would be flagged (over-strict, fails safe).

**Gates:** ruff · black · `mypy src --strict` clean; **993 unit tests @ 96.63% coverage** (264 corpus
tests, up from 226). Coverage unmoved — still zero product code.

**Recorded, deliberately NOT done here:** no **outbox-shaped fixture** exists — nothing encodes what
the outbox payload is *allowed* to contain (no `candidate`, no `chunks[].text`, no `summary`). 4b
projects to Neo4j and must add it (recorded as a 4b requirement in `docs/EXTRACTION_PLAN.md`).

### Round 3 of findings-and-fix (`red(4a-hard-3)` → `green(4a-hard-3)`) — the first round that read the ENGINE

Rounds 1–2 hardened the corpus against an *idealized* algorithm. Round 3 ported the one 4c actually
extracts (hris `matching/{stages,orchestrator}.py`) and found **two of `MatchWeights`' five structured
sub-scores do not compute what their names imply**. Both holes existed only against the real code.

| # | Finding | Fix |
|---|---|---|
| F1 | **`seniority` (0.15) is not a years check** — it is `cosine(jd.title, most-recent role title)`, rescaled. The toml and the potency test justified **both** `experience` and `seniority` with one years claim, so the corpus asserted `experience` twice and `seniority` **never**, while r09 carried the most JD-distant title in the corpus. Measured (faithful + **no-op verifier**): seniority 0.271 → r09 rank 8 → precision@5 = 1.00 → **a bad engine passes**. Round 2 had **relocated** the bait hole from education (0.10) onto seniority (0.15), not closed it | r09's most-recent title is the **JD title verbatim** → `cosine(x,x) = 1.0` → seniority **exactly 1.0 by arithmetic, under any embedder**. That matters: `Senior Backend Engineer` measured **0.755** on one `nomic-embed-text` build and **0.581** on another, straddling the **0.638** break-even at which the trap arms |
| F2 | **`education` (0.10) reads the degree LEVEL only**, never `jd.education.fields` — so the r14/r11 twins (differing in *field*) asserted a mechanism that **does not exist** (both `BSc` → education = 1.00), and the pair still passed an education-blind ranker through the **embedded-degree vector leak** | Twins now differ in **level**; both fields JD-allowed. Education moves `score_final` by 0.0400 and **dominates** the ~3e-04 vector residual, which now points at the **lower** twin — so ordering the pair *requires* implementing the sub-score |
| F3 | hris's shipped `_fuzz_substring` is a **character-set overlap ratio**: it **verifies all four** of the corpus's fabricated anchors (0.928/0.943/0.988/0.935) | Recorded as a hard 4c requirement: **replace it, do not port it** (rapidfuzz `partial_ratio` scores the same negatives 0.36–0.46) |

### Round 4 of findings-and-fix (`red(4a-hard-4)` → `green(4a-hard-4)`)

| # | Finding | Mutation that stayed green | Fix |
|---|---|---|---|
| F5 | **Two of the three ordering pairs did not gate their dimension.** `rank(higher) < rank(lower)` is satisfiable by a **tie-break**. `_build_summary_text` reads neither `total_years_experience` nor `cover_letter_chunks`, so the overqual and motivation twins' **embedding input is byte-identical** → a dimension-blind engine ties them **exactly** (+0.000e+00) and the stable sort decides the pair arbitrarily. A **motivation-blind engine PASSED** the motivation pair in the fixtures' natural order; the overqual pair failed only by luck (it PASSES on the reversed order) | `weights.motivation = 0` (motivation pair passes); `overqual_ratio = 99` (overqual pair passes on reversed input order) | **`[ordering_controls].min_score_gap = 1e-6`** (three-way-contract change). The assertion is now `rank(hi) < rank(lo)` **AND** `score_final(hi) − score_final(lo) ≥ min_score_gap` — no tie can pass on any input order. Correct-engine gaps +0.0397/+0.0120/+0.0900, all **arithmetic**. The twins' byte-identical embedding input is now itself asserted, so the tie cannot be "fixed" by narrating the dimension into a `summary` (that is F2 again) |

Rejected alternative: copying F2's inverted-residual trick into the other two twins — it would re-introduce
an **embedder-dependent magnitude**, and F1 is exactly the lesson that a measured quantity can straddle a
threshold between two builds of the same model. **Pin by arithmetic, not by measurement.**

Also reconciled: r09's **exact rank is no longer written anywhere** (it was "rank 9" in three files, and
"~11" in two more). It is near-tied with r04 — 0.596994 vs 0.596711, a spread whose **sign flips between
`nomic-embed-text` builds — and is gated by nothing. What is build-independent, and what the corpus does
gate: r09 sits **below every strong fixture**, outside the k=5 window, with **~0.19** of margin.

**Baseline battery, re-measured against the full contract** (both input orders; see
`docs/EXTRACTION_PLAN.md` for the table): keyword-overlap **FAIL** · lexical tf-idf **FAIL** · embedding
pure-vector (p@5 0.80, r09 rank 4) **FAIL** · faithful + no-op verifier **FAIL** · faithful + hris
`_fuzz_substring` **FAIL** · faithful + correct verifier **PASS**. The round-3 report's "tf-idf
pure-vector" row conflated the lexical and embedding baselines and is corrected there.

**Accepted 4a residuals, recorded not fixed** (`docs/EXTRACTION_PLAN.md`, "the class of wrong engine this
corpus still lets through"): the corpus is **blind to the skill sub-score's internals** — `weights.skill =
0.0`, a disabled recency decay (even though r10's `decision_point` is `recency_decay_stale_skills`, it has
no twin), a doubled `must_have_miss_penalty` and an ontology junk-bucket all still PASS — and it gates the
evidence **verifier** but never the evidence **extractor**. Both are 4c requirements now.

**Gates:** ruff · black · `mypy src --strict` clean; **1034 unit tests @ 96.63% coverage** (305 corpus
tests); `run_evals.py` still exits 1. Zero `core/src/` changes.
