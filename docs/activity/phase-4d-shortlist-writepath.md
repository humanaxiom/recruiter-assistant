# Phase 4d — Shortlist + reverse-match write path

**Status:** built and gate-green on branch `feat/phase-4d-shortlist-writepath`, tip `6c2bf43`, 2 commits,
off `main` @ `fd12d1a` (Phase 4c's merge commit, PR #12). **All three merge-blocking gates green
(reviewer APPROVE, security PASS, ranking-evals PASS). NOT yet PR'd, NOT merged** — a PR opens after a
human check-in. CI (`gates-all`, including a live `run_evals.py` re-measurement against Ollama) has not
yet run, since no PR exists.

This sub-phase ships the write path the 4c matching engine was missing: two arq tasks
(`shortlist_job`/`reverse_match_job`) that call the 4c orchestrator and persist its result, plus the one
call site (ADR-009's carried "Requirement 1") that actually builds `MatchingContext`/`MatchWeights` from
`Settings` instead of in-code defaults. Full decisions and residuals: [ADR-010](../adr/010-shortlist-reverse-match-write-path.md).

## TDD sequence

One RED commit, one GREEN commit:

| Commit | Label | What it added |
|---|---|---|
| `24419b0` | `red` | `core/tests/unit/test_services_shortlist_persist.py`, `test_matching_context_settings_wiring.py`, `test_shortlist_pii_framing.py`, `test_worker_shortlist_job.py`, `test_worker_reverse_match_job.py`, updated `test_worker_wiring.py`, and `core/tests/integration/test_shortlist_persistence_pg.py` (6 new tests against a real Postgres). Fails at collection: `src.services.shortlist_service` and `src.worker.matching_tasks` do not exist yet (`ModuleNotFoundError`). |
| `6c2bf43` | `green(4d)` | `src/pipeline/matching/orchestrator.py`'s `matching_context_from_settings` + `non_matchable_families_from_settings` (settings.py); `src/services/shortlist_service.py` (`persist_shortlist`/`persist_reverse_match`); `src/worker/matching_tasks.py` (`shortlist_job`/`reverse_match_job`); `src/worker/main.py`'s `WorkerSettings.functions` extended to `[parse_job, parse_resume, shortlist_job, reverse_match_job]`. Minimal implementation to turn every RED test green. |

Two commits only — no separate refactor pass was needed; the reviewer's one finding on this branch
(git_sha wiring) had already been closed in 4c (ADR-009 §6), so nothing equivalent recurred here.

## Two test-driven divergences from a naive first-cut design

1. **`persist_shortlist`/`persist_reverse_match` take no `*, weights` parameter.** An early sketch of the
   persist signature considered accepting `MatchWeights` directly (mirroring how `generate_shortlist`
   takes `weights=...`), so a caller could pass weights independently of the orchestrator's result. The
   RED tests reject this: `PipelineMeta.weights` already carries the full, validated `MatchWeights` the
   orchestrator scored *with* — `_meta_json` serializes `result.pipeline_meta` (or a `DEFAULT_WEIGHTS`
   fallback for the `pipeline_meta is None` edge case) directly into the `pipeline_meta` jsonb column, and
   nothing else in either INSERT reads a weights value. A separate `weights` parameter would let a caller
   pass a `MatchWeights` that *disagrees* with the one the orchestrator actually scored the entries with —
   a silent inconsistency between "what was scored" and "what got written down as having scored it,"
   which is exactly the kind of bug a persistence-layer test should make impossible, not merely undocumented.
   Weights ride exclusively inside `result.pipeline_meta`.
2. **Per-row `conn.execute`, not `conn.executemany`.** `executemany` would batch every entry's INSERT into
   one round trip, which looks like the obvious optimization for "one INSERT per ranked entry." The
   integration tests against a real Postgres pin per-row `execute` instead, because each row's SQL args
   include per-entry-computed JSON (`json.dumps(breakdown)`, the coerced `evidence_json`) that differs in
   *shape*, not just value, between rows on the shortlist path (`evidence_json` is sometimes the literal
   string `"{}"`, sometimes a full serialized `EvidenceObject`) — `executemany` requires uniform
   positional-argument shape across the batch and offers no natural place to run the per-row
   fold-into-breakdown / null-coercion logic inline; doing that transformation in a pre-pass list
   comprehension before a single `executemany` call was considered and rejected as strictly more code for
   no measured latency benefit at 4d's actual candidate-pool sizes (`coarse_k=50`, `evidence_k=15`) — a
   shortlist run persists at most tens of rows, not thousands, so N round trips inside one already-open
   transaction is not a bottleneck worth the batching complexity.

## What each new call site does (see ADR-010 for full rationale)

1. **`matching_context_from_settings(settings, *, db, neo4j, llm, embedder) -> MatchingContext`** — the
   single site populating `family_weight`/`non_matchable_families`/`llm_concurrency`/
   `evidence_max_tokens`/`model_gen`/`model_emb`/`git_sha` from `Settings`, closing ADR-009's carried
   "Requirement 1." `stages.py` is byte-unchanged — this is additive wiring, not a scoring change.
2. **`shortlist_job(ctx, job_id_str)`** — `missing` (no job row) / `not_parsed`
   (`description_parsed IS NULL`) / `persisted` (non-empty result written) / `empty` (zero candidates,
   still persisted to clear a stale prior run). Calls `generate_shortlist` with
   `weights=weights_from_settings(get_settings())`, never `DEFAULT_WEIGHTS`.
3. **`reverse_match_job(ctx, resume_id_str)`** — same four-status contract, `missing`/`not_parsed`
   keyed on the résumé row/`status != 'parsed'`. Scopes `allowed_job_ids` to
   `jobs WHERE description_parsed IS NOT NULL` (never `None`, which would mean "no filter" to the
   orchestrator) and sources `evidence_k=settings.match_reverse_evidence_k` (not the orchestrator's
   `_REVERSE_EVIDENCE_K` module literal).
4. **`persist_shortlist`/`persist_reverse_match`** — DELETE-first per-run replacement, mirror-image
   handling of `score_structured`/`score_evidence`/`evidence` dictated by the two tables' DDL shapes
   (ADR-010 §2).
5. **`WorkerSettings.functions`** extended to `[parse_job, parse_resume, shortlist_job,
   reverse_match_job]` — `project_to_graph` (the 4b outbox drainer) stays a `cron_jobs` entry, never
   enqueued by name, unchanged from 4b.

## Reviewer, security, ranking-evals — verdicts and the guard set they verified

**Three merge-blocking gate verdicts on GREEN `6c2bf43`:** reviewer **APPROVE**, security **PASS**
(no new PII-leak surface beyond the already-accepted ADR-007 §6/§7 posture — see ADR-010 §6),
ranking-evals **PASS** (no scoring change in this branch; `stages.py`/`orchestrator.py`'s existing
4a/4b/4c corpus obligations are unaffected because `stages.py` is byte-unchanged — `run_evals.py`'s
live-engine run still exits 0).

The reviewer's sign-off rests on eight behavioral guards, each backed by a test built specifically to
fail against the naive-but-plausible wrong implementation named alongside it — every one green on
`6c2bf43`:

| # | Guard | Would fail if... | Test |
|---|---|---|---|
| 1 | Shortlist rerun replaces, not duplicates | persist inserted without deleting first (real `UNIQUE (job_id, resume_id)` violation on rerun) | `test_persist_shortlist_rerun_replaces_prior_run` (integration) |
| 2 | Reverse-match rerun replaces, not duplicates | same class, other table's unique index | `test_persist_reverse_match_rerun_replaces_prior_run` (integration) |
| 3 | `evidence=None` → `{}` satisfies `NOT NULL` on `shortlist_entries.evidence` | raw SQL `NULL` was written instead (real `NotNullViolationError`) | `test_persist_shortlist_none_evidence_satisfies_not_null_constraint` (integration) |
| 4 | `shortlist_job`/`reverse_match_job` use `weights_from_settings(settings)`, never `DEFAULT_WEIGHTS` | the call silently fell back to the orchestrator's default argument (invisible under `Settings()` defaults — see ADR-010 §3) | `test_shortlist_job_passes_weights_from_settings_not_default_weights`, `test_shortlist_job_end_to_end_persists_pipeline_meta_weights_from_settings` (integration) |
| 5 | `reverse_match_job` sources `evidence_k` from `settings.match_reverse_evidence_k` | the orchestrator's `_REVERSE_EVIDENCE_K` module literal was used instead (equal to the default today, so only a non-default override exposes the bug) | `test_reverse_match_job_uses_match_reverse_evidence_k_from_settings`, `test_reverse_match_job_e2e_persists_pipeline_meta_weights` (integration) |
| 6 | `matching_context_from_settings` populates every non-weight tunable from `Settings`, not `MatchingContext`'s dataclass defaults | the factory silently returned the dataclass defaults for any of `family_weight`/`non_matchable_families`/`llm_concurrency`/`evidence_max_tokens`/`model_gen`/`model_emb`/`git_sha` | `test_matching_context_from_settings_populates_tunables`, `test_matching_context_from_settings_default_settings_still_wires_through` |
| 7 | `reverse_match_job` never ranks against an unparsed JD | `allowed_job_ids` was passed as `None` (orchestrator's "no filter" sentinel) instead of the parsed-jobs set | reverse_match_job's own control-flow tests in `test_worker_reverse_match_job.py` |
| 8 | 4d introduces no silent new redaction of evidence text | a scrub/mask pass was added to the write path without review | `test_persist_shortlist_does_not_redact_a_pii_shaped_evidence_string`, `test_persist_shortlist_writes_resume_chunk_evidence_quote_verbatim`, `test_persist_shortlist_writes_cover_letter_evidence_quote_verbatim` |

8 of 8 verified — every guard above is a live, currently-passing test against the real implementation
(the integration-tagged ones against a real testcontainers Postgres), not merely a design intent recorded
in prose.

## Final gate state — HEAD `6c2bf43`

- Offline: ruff / black / `mypy --strict` clean.
- **1947 unit tests @ 91.98% coverage** (up from 1916 on `main` post-4c-merge).
- **93 integration tests** vs real Postgres + Neo4j (87 carried forward + 6 new, all in
  `test_shortlist_persistence_pg.py`).
- **All three merge-blocking gates green:** reviewer APPROVE (guard table above), security PASS (empty
  findings table; ADR-010 §6 records the PII-at-rest extension explicitly rather than leaving it only in
  a test docstring), ranking-evals PASS.
- `run_evals.py`'s live-engine corpus run is unaffected by this branch — `stages.py`/`orchestrator.py`'s
  scoring code is byte-identical to 4c's merged tip; the only orchestrator change is the additive
  `matching_context_from_settings` factory. **CI has not yet re-measured this** (no PR open yet) — the
  live numbers will be re-confirmed against Ollama when the PR's `gates-all` runs, per the plan-of-record
  (do not treat 4c's last-measured `run_evals.py` numbers as re-verified by this branch until CI runs).

## Carried forward into Phase 5

- **`list_for_job`/`get_one`/`export_rows` + display redaction** are Phase 5's job, per the
  plan-of-record — 4d deliberately shipped write-only.
- **The shortlist-side `evidence = {}` ambiguity** (ADR-010 §2): "never evidence-scored" and "scored,
  found nothing" are both `{}` at the raw jsonb level on `shortlist_entries`. Phase 5's read/list/get
  layer is the first code to query these columns outside the write path and should be aware of this if
  its SQL (rather than application code) ever needs to distinguish the two states directly.
- **Open human decision, not resolved here (again):** `score_education` ignores `jd.education.fields`
  (ADR-009 §7, restated ADR-010 §5) — extend the scorer or drop `fields` from the JD contract.
- **No advisory lock on concurrent duplicate shortlist/reverse-match runs** (ADR-010 §1) — accepted for
  v1 since nothing currently enqueues duplicates by design; revisit once Phase 6 ships a user-facing
  regenerate route.
