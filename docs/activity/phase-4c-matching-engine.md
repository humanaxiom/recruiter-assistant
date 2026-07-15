# Phase 4c — Matching engine

**Status:** built and gate-green on branch `feat/phase-4c-matching-engine`, tip `ed4a142`, 6 commits, off
`main` @ `68fe821` (Phase 4b's merge commit, PR #11). **All three merge-blocking gates green. NOT yet
PR'd, NOT merged** — a PR opens after a human check-in.

This sub-phase ports the 4-stage ranking engine (`stages.py` + `orchestrator.py`) from hris, wires
`MatchWeights` through `Settings`, and — for the first time in this project — makes
`core/tests/evals/run_evals.py::main()` a real gate: it now runs the live orchestrator against the
4a/4b corpus and must exit 0, instead of exiting 1 as a scaffold. Full decisions and residuals:
[ADR-009](../adr/009-matching-engine-port.md).

## TDD sequence

Four RED commits, one GREEN, one REFACTOR:

| Commit | Label | What it added |
|---|---|---|
| `5cb24b8` | `red(4c)` | `core/tests/unit/test_matching_stages.py` — pure scoring functions (`score_skill_breakdown`, `score_experience`, `score_education`, `is_senior_candidate`, `normalise_vector_scores`, `verify_evidence`, `stage4_combine`, `_evidence_completeness`, `_motivation_score`), pinning the **corrected** behaviour for blockers #1 and #6 up front — not a literal copy of hris's known-buggy behaviour. Fails at collection with `ModuleNotFoundError` (`src.pipeline.matching.stages` does not exist). |
| `8521fd7` | `red(4c)` | `core/tests/unit/test_settings_matching.py` — the `match_*` Settings block + `weights_from_settings`, pinning two deliberate deviations from a naive hris copy: the bridge lives in `src/settings.py` itself (no sibling `matching/config.py` — CLAUDE.md's single-config-module rule), and `match_reverse_evidence_k` defaults to hris's worker-path value (`10`), not the superseded synchronous-endpoint value (`0`). Fails with `ImportError`. |
| `243cf12` | `red(4c)` | `core/tests/integration/test_matching_orchestrator.py` — the DB/Neo4j/LLM-heavy orchestrator (stage 1 coarse recall, stage 2 per-candidate, stage 3 evidence, stage 4 combine, reverse match) against real Postgres + Neo4j, including `test_stage2_skill_rows_reads_canonical_key_not_canonical_name` (blocker #4) and the `is_senior_candidate` vs. cosine-`seniority` split test (see RED-tester findings below). |
| `5fcb52b` | `red(4c)` | The corpus wiring: `run_evals.py`'s `main()` flipped from the pre-4c scaffold (asserting exit 1) to asserting exit 0 against the not-yet-existing orchestrator, plus `_assert_must_have_penalty_fires_on_r18` (the single-candidate review obligation — see ADR-009 §2) and the `r18`/`r19` skill-dimension twin fixtures (must-have-miss, recency) the 4b blockers required. |
| `5ba1577` | `green(4c)` | `src/pipeline/matching/stages.py` + `orchestrator.py` + `settings.py`'s `match_*` block/`weights_from_settings` — minimal implementation to turn all four RED commits green, including the `_fuzz_ratio`/`partial_ratio` verifier, the `reason == "missing"` must-have-miss key, the `canonical_key` Cypher, and the `git_sha` wiring (still via `os.environ.get` at this point — see the reviewer finding below). |
| `ed4a142` | `refactor(4c)` | Reviewer finding fix: `git_sha` routed through `Settings`/`MatchingContext` instead of `os.environ.get`, plus the new `test_no_scattered_os_environ.py` AST meta-test that makes the rule mechanically enforced going forward. Branch tip. |

## What each blocker fix was

1. **`missing_must` keys off `reason == "missing"` (row `ontology_weight == 0`), not
   `SkillContribution.score == 0.0`.** hris's own check silently stopped firing once family credit
   existed: a genuine miss's *built* contribution defaults `ontology_weight=None` (`None == 0` is
   `False`), and a family-credited-but-zero-tenure row can independently hit `score == 0.0` without
   being a miss. Fixed by computing the row-level `ontology_weight == 0` check **before** the
   contribution object is built, stamping `reason="missing"` at that point, and filtering
   `missing_must` on `reason`, never on the numeric `score`. Full algebra for why this needed a
   single-candidate check (r18) rather than any pairwise rank/gap assertion: ADR-009 §2.
2. **`canonical_name` → `canonical_key`.** `_stage2_skill_rows`' Cypher reads
   `reqSkill.canonical_key`, matching ADR-008's Phase-4b rename. A verbatim hris port
   (`canonical_name`) would fail loud (`SkillContribution.skill: str` ← `None` →
   pydantic `ValidationError`) against a real Neo4j.
3. **`_fuzz_substring` replaced with `rapidfuzz.fuzz.partial_ratio` at `evidence_verify_fuzz = 0.85`.**
   Re-measured against the full corpus during this port: `partial_ratio` rejects all four fabricated
   quotes at 0.41–0.46 and survives all four gold anchors at 1.000; `fuzz.WRatio` leaks r02's
   fabrication at 0.855; `fuzz.ratio` rejects the gold anchors at 0.648/0.796. Full landmine detail:
   ADR-009 §1.
4. **NICE_TO_HAVE skills feed stage-3 evidence text but not the stage-2 structured skill sub-score** —
   ported verbatim (hris's shipped behaviour), recorded as a decision in ADR-009 §4, not silently
   inherited and not "fixed" as unrequested new scope.
5. **`match_reverse_evidence_k` default = 10**, the worker-path value — recruiter-assistant has no
   synchronous reverse-match endpoint to protect, so it inherits hris's current default rather than
   the superseded synchronous-endpoint value of 0.
6. **`git_sha` routed through `Settings`** — a reviewer finding on the GREEN commit (`orchestrator.py`
   read `os.environ.get("GIT_SHA")` at two call sites, violating CLAUDE.md's config-via-settings-only
   rule). Fixed in the REFACTOR commit, plus a new AST-based meta-test
   (`test_no_scattered_os_environ.py`) that walks every module under `src/` and fails if any module
   other than `settings.py` reads `os.environ`/`os.getenv`, in any import shape.

## RED-tester findings

- **The `None == 0` trap.** The tester's first draft of the must-have-miss test suite initially
  mirrored hris's `score == 0.0` check as the "obvious" port target; re-reading `stages.py`'s own
  intended behaviour (per the 4b blocker write-up) caught that a genuinely-missing row's *built*
  contribution defaults `ontology_weight=None`, so `contribution.ontology_weight == 0` is `False` for
  exactly the row it needs to catch. The corrected RED tests
  (`test_missing_must_keys_off_ontology_weight_not_score_mutant`,
  `test_missing_must_have_is_not_masked_by_a_present_family_credited_sibling`) pin the row-level check
  instead and are sharp enough to kill a mutant that keys off the built object's score.
- **The `is_senior_candidate` vs. inline-cosine split.** `is_senior_candidate` (a years-based boolean
  gate feeding the implied-experience relief) and the `seniority` structured sub-score
  (`cosine(jd.title, most-recent role title)`, computed inline in `_stage2_per_candidate` with a live
  embedder) are two entirely different things that share no code path and are easy to conflate by
  name. The unit-test suite (`test_matching_stages.py`) explicitly separates them — `is_senior_candidate`
  is tested as a pure boolean gate with no embedder involved, and a docstring note in both the module
  and the test file states plainly that `seniority` is out of scope for the pure-function file because
  it needs a live embedder, which only the integration suite can exercise.
- **The canonical_key test's missing `_seed_job_node`.** The integration test asserting the
  `canonical_key`-not-`canonical_name` fix initially seeded a `REQUIRES` edge via `_seed_requires`
  without first creating the Neo4j `Job` node the Cypher's `MATCH (j:Job {id})` depends on — against a
  real Neo4j this silently creates **zero** edges (the `MATCH` yields no rows, so the subsequent
  `MERGE` never runs), leaving one orphan `Skill` node and a test that was asserting behaviour on an
  edge that never existed. Caught during the integration RED review and fixed by adding the
  `_seed_job_node` call already used by the other REQUIRES-seeding tests in the same file, without
  touching the test's actual behavioural assertion (`skill == "python"`, not `canonical_name`'s
  never-set value).

## Final gate state — HEAD `ed4a142`

- Offline: ruff / black / `mypy --strict` clean.
- **1916 unit tests @ 90.71% coverage**.
- **87 integration tests** vs real Postgres + Neo4j.
- **`run_evals.py` exits 0** — the corpus's first real live-engine run, not a scaffold.
- **All three merge-blocking gates green:**
  - **security PASS** (empty findings table).
  - **reviewer APPROVE** (after the `git_sha` fix).
  - **ranking-evals PASS** — all six mutation obligations FAIL the corpus as required
    (`weights.education = 0`, `overqual_ratio = 99`, `weights.motivation = 0`, `weights.skill = 0`,
    `must_have_miss_penalty 0.5 → 1.0`, disabled recency decay), plus the optional WRatio-swap
    mutation. `precision@5 = 1.0`; r09 (the keyword-stuffer adversarial bait) ranks outside the top-5
    (rank 12); `gold_recall = 4/4`; `0` PII leaks across `116` scanned inputs; determinism exact
    (`max_rank_delta = 0`, `max_score_delta ≤ 1e-9`) across both determinism runs.

## Carried forward into 4d

- The worker path (`shortlist_job`/`reverse_match_job`) must populate `MatchingContext`/`weights` from
  `Settings` via `weights_from_settings` at the real call site — 4c proves the bridge is correct in
  isolation; nothing yet calls it outside tests.
- **Open human decision, not resolved here:** `score_education` ignores `jd.education.fields` — either
  extend the scorer or drop `fields` from the JD contract (ADR-009 §7).
- Per the plan-of-record, 4d ships the write path only (`persist_shortlist`/`persist_reverse_match`);
  `list_for_job`/`get_one`/`export_rows` and display redaction stay Phase 5.
