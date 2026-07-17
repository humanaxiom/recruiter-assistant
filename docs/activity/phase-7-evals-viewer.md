# Phase 7 — Evals + minimal Flask viewer

**Status:** MERGED to `main` via PR #16 (squash `1039e5c`), 2026-07-17, CI green. Built on branch
`feat/phase-7-evals-viewer`, off `main` @ `e910669` (Phase 6's merge), pre-merge tip `92ca4ae` (branch now
deleted, local + remote). **All three merge-blocking gates were green (reviewer APPROVE, security PASS,
ranking-evals PASS).** **Post-review addition (2026-07-17): the live end-to-end eval
against the real stack — originally deferred (ADR-013 §5) — was reversed, built, run, and PASSED,
reproduced identically twice, and was a prerequisite for merging PR #16.** See "Live end-to-end eval
(post-review addition)" below.

This phase ships the last v1-scope item from `docs/EXTRACTION_PLAN.md`'s locked decisions: a minimal
read-only web viewer over the Phase 6 HTTP API. It is the first phase with a human-facing surface and the
first client of Phase 6's routes other than a test. Full decisions and residuals:
[ADR-013](../adr/013-phase7-evals-viewer.md).

## TDD sequence

One planning/interim-docs commit, then a single RED/GREEN pair, then a post-review fix pass:

| Commit | Label | What it added |
|---|---|---|
| `55ee0a0` | `docs` | Interim HANDOFF/plan stamp flipping Phase 6 to merged and Phase 7 to active, ahead of any code landing. |
| `942e8f5` | `red` | Failing unit tests for `core/frontend/api_client.py` (not yet existing), the extended `core/frontend/app.py` routes, and the gate-scope meta-test (`test_gates_cover_frontend.py`, asserting `frontend` appears in every relevant `Makefile`/CI gate invocation before the fix that makes it true). Fails at collection/import and on the meta-test's string assertions against the pre-widening `Makefile`/`ci.yml`. |
| `f28c22e` | `green` | `core/frontend/api_client.py` (new — sync httpx wrapper, one function per Phase-6 route), `core/frontend/app.py` (extended with the eight routes below), `core/frontend/templates/*.html` (new), and the `Makefile`/`.github/workflows/ci.yml` gate-scope widening to include `frontend`. Minimal implementation to turn every RED test green. |
| `92ca4ae` | `refactor/fix` | Post-first-green gate round found two security findings (below) and closed both, plus a reviewer nit accepted as a residual. |

## Gate round 1 → fix pass

The first full gate pass after `f28c22e` was **reviewer CHANGES-REQUESTED / security findings, not yet
PASS**:

- **Security finding #1 (the one that mattered): `resume_detail.html` rendered candidate PII fields,
  gated only on the backend's `blinded` flag.** ADR-012 already documents `JobOut.blind_review`'s
  fail-open history as a real prior bug; relying on a single upstream flag for the viewer's own last line
  of defense repeats that class of risk. Fixed by rewriting the template so it has no code path capable of
  rendering `candidate.name`/`email`/`phone`/`location` at all — it only ever reads
  `resume.original_filename`, `resume.status`, `resume.id`.
- **Security finding #2: `_unavailable`'s error page interpolated the raised exception's message,** which
  could carry the backend's base URL or a raw `httpx` exception string into a page served to the browser.
  Fixed by making `error.html` fully static — no backend-supplied text of any kind reaches it.
- **Reviewer nit (accepted, not fixed): `_unavailable(exc: BackendUnavailable)`'s `exc` parameter is now
  unused** after finding #2's fix removed the only thing that read it. Kept because the signature still
  documents the handler's intent, and ruff's unused-argument rules are not enabled in this repo. Recorded
  in ADR-013 as an accepted residual.

Re-verification on `92ca4ae`: reviewer **APPROVE**, security **PASS** (both findings closed), ranking-evals
**PASS** (scoring code byte-unchanged — see "Evals workstream" below).

## What each new/modified module does (see ADR-013 for full rationale)

1. **`core/frontend/api_client.py` (new)** — a thin sync `httpx`-based wrapper: `build_client()` (attaches
   `X-API-Key` iff `settings.api_key` is set, UTF-8-encoding the header so a non-ASCII key can't crash the
   client the way ADR-012 §1's SEC-1 finding described server-side), one function per consumed Phase-6
   route (`list_jobs`, `get_job`, `list_resumes`, `list_shortlist`, `get_shortlist_entry`, `get_resume`,
   `get_match_results`, `export_shortlist`), and a small typed error hierarchy
   (`BackendError` → `NotFound` (404) / `BackendUnavailable` (5xx or connection failure)). Every function
   accepts an optional `client: httpx.Client | None` so tests can inject an `httpx.MockTransport`-backed
   client with no real network.
2. **`core/frontend/app.py` (extended)** — kept `/health`; added `/` (job list), `/jobs/<uuid>` (job +
   résumé list), `/jobs/<uuid>/shortlist` (list, never passes `reveal`), `/shortlist/<uuid>` (get one),
   `/resumes/<uuid>` (hardcodes `reveal=False`, ignores any browser-supplied `?reveal=`),
   `/resumes/<uuid>/match-results`, and `/jobs/<uuid>/shortlist/export` (proxies the backend export
   response's body/`Content-Disposition` without ever exposing the outbound `X-API-Key` to the browser).
   Every route is server-side Jinja2 with no client-side JS that could re-fetch a raw/reveal endpoint.
3. **`core/frontend/templates/*.html` (new)** — `base.html`, `index.html`, `job_detail.html`,
   `shortlist_list.html`, `shortlist_entry.html`, `match_results.html`, `resume_detail.html` (structurally
   PII-incapable, ADR-013 §2), `error.html` (fully static).
4. **`Makefile` (modified)** — `gates`/`gates-fast` now run `ruff check`/`black --check`/`mypy --strict`
   over `src tests frontend` (was `src tests`), and the coverage gate adds `--cov=frontend`.
5. **`.github/workflows/ci.yml` (modified)** — the `static` and `unit` jobs' commands widened the same
   way (`ruff check src tests frontend`, `black --check src tests frontend`, `mypy src frontend --strict`,
   `--cov=src --cov=frontend`).
6. **`core/tests/unit/test_gates_cover_frontend.py` (new)** — a meta-test reading `Makefile`/`ci.yml` text
   and asserting `frontend` appears in every relevant gate invocation, pinning the widening so it can't
   silently regress.

## Route map (viewer)

| Method | Path | Backend route(s) consumed | Redaction posture |
|---|---|---|---|
| GET | `/` | `GET /jobs` | n/a (job metadata only) |
| GET | `/jobs/<uuid>` | `GET /jobs/{id}`, `GET /jobs/{id}/resumes` | n/a |
| GET | `/jobs/<uuid>/shortlist` | `GET /jobs/{id}/shortlist` | unconditionally blind — no `reveal` kwarg exists to pass |
| GET | `/shortlist/<uuid>` | `GET /shortlist/{id}` | unconditionally blind |
| GET | `/resumes/<uuid>` | `GET /resumes/{id}` | hardcoded `reveal=False`; browser `?reveal=` ignored; template structurally cannot render PII |
| GET | `/resumes/<uuid>/match-results` | `GET /resumes/{id}/match-results` | none (mirrors ADR-012 §4 — the backend applies none here either) |
| GET | `/jobs/<uuid>/shortlist/export` | `GET /jobs/{id}/shortlist/export` | proxies the backend's default (`reveal=False`); no browser control to flip it |
| GET | `/health` | — | n/a |

## Locked human decisions this phase (see ADR-013 for full text)

1. Blind-only viewer for v1 — no reveal control anywhere in the viewer, structurally, not just by default.
2. The résumé detail template is redesigned to be structurally PII-incapable, not merely flag-gated.
3. Gate scope widened to `core/frontend/` in the existing `gates`/`gates-fast`/CI targets, pinned by a
   meta-test, rather than a separate frontend-only gate suite.
4. No new evals fixtures this phase — the plan's Phase 7 evals line item (precision@k, evidence-verification
   rate) was already satisfied by Phase 4a (corpus) + 4c (live orchestrator wiring); `run_evals.py::main()`
   already runs inside the gated unit suite.
5. Live end-to-end eval (real pipeline, re-measuring `thresholds.toml` against persisted shortlist rows)
   was recorded here as deferred, needing a reachable host Ollama + `docker compose up`, which CI does not
   provide by design. **Reversed 2026-07-17**: the human un-deferred it and made it a prerequisite for
   merging PR #16 — built, run, and PASSED, reproduced twice. See "Live end-to-end eval (post-review
   addition)" above and ADR-013 §5.
6. Corrected a recurring documentation inaccuracy: prior HANDOFF/plan text said CI runs "a live
   `run_evals.py` re-measurement against Ollama." CI runs the offline deterministic stand-in harness inside
   the unit suite; it never calls Ollama (`.github/workflows/ci.yml`'s own comment: "CI never calls a model
   endpoint; inference is host-only by design"). Corrected wherever it appeared. This remains accurate — the
   live eval added in item 5 above runs as a separate script, outside CI, not inside the gated unit suite.

## Evals workstream — confirmed already built, not re-verified from scratch this phase

- Phase 4a (`core/tests/evals/`): the labelled corpus, `thresholds.toml`, nine hardening rounds — merged
  long before this phase.
- Phase 4c (`run_evals.py`): live orchestrator wiring — `precision@5 = 1.0`, `gold_recall = 4/4`, 0 PII
  leaks / 116 inputs scanned, determinism exact, all six mutation obligations FAIL as required — merged
  before this phase.
- The viewer build itself (first-green commit `f28c22e` through `92ca4ae`) touched neither directory.
  `run_evals.py::main()` continues to run inside `pytest tests/unit` (part of the 2229-test count below) and
  continues to exit 0. **Post-review (2026-07-17), `core/tests/evals/` gained one new file**
  (`run_evals_live.py`, the live end-to-end harness — see "Live end-to-end eval (post-review addition)"
  above); `run_evals.py` itself is unchanged.

## Reviewer, security, ranking-evals — verdicts

**Final verdict, HEAD `92ca4ae`:** reviewer **APPROVE** (the résumé-template PII finding closed, the
`_unavailable` unused-`exc` nit accepted as a residual); security **PASS** (both hardening findings closed
— structurally-PII-incapable résumé template, fully static error page); ranking-evals **PASS** (scoring
code byte-unchanged: `stages.py`/`orchestrator.py` untouched by this phase; offline corpus 352 tests green;
`run_evals.py::main()` exits 0).

| # | Guard | Would fail if... | Test class |
|---|---|---|---|
| 1 | `resume_detail.html` never renders `candidate.*` fields | the template regained a branch printing name/email/phone/location | template-rendering test asserting the raw bytes are absent from the rendered HTML regardless of the backend response shape |
| 2 | `error.html` renders no backend-supplied text | a future edit reintroduced interpolating the exception message | route test asserting the 503 body contains no backend URL/exception text |
| 3 | Shortlist list/detail routes never pass `reveal` to the backend | a future edit added a `reveal` kwarg to `list_shortlist`/`get_shortlist_entry` | signature/call-site test on `api_client` + route test inspecting the outbound request's query params |
| 4 | `/resumes/<uuid>` ignores a browser-supplied `?reveal=` | the route started reading `request.args.get("reveal")` | route test hitting `/resumes/<uuid>?reveal=true` and asserting the outbound backend request still carries `reveal=false` |
| 5 | `frontend` is present in every gate invocation in `Makefile`/`ci.yml` | either file's gate commands dropped `frontend` from the `ruff`/`black`/`mypy`/`--cov` argument list | `test_gates_cover_frontend.py` (string/regex assertions over both files' text) |
| 6 | `X-API-Key` attaches correctly and non-ASCII keys don't crash the client | the UTF-8-encode step on `httpx.Headers(..., encoding="utf-8")` was removed | targeted unit test with a non-ASCII `settings.api_key` |

## Final gate state — HEAD `92ca4ae`

- Offline: ruff / black / `mypy src frontend --strict` clean.
- **2229 unit tests @ 91.67% coverage.**
- Frontend is now format/type/coverage-gated for the first time (previously invisible to every quality
  gate — it is a sibling of `core/src/`, not nested under it).
- **Post-review update (2026-07-17):** `test_evals_live_metrics.py` (16 new offline unit tests) brings the
  offline suite to **2245 unit tests @ 91.67% coverage**; ruff/black/mypy remain clean. See "Live end-to-end
  eval (post-review addition)" above.
- **All three merge-blocking gates green:** reviewer APPROVE (guard table above), security PASS (both
  findings closed), ranking-evals PASS (no scoring code touched; offline corpus 352 tests green;
  `run_evals.main()` exits 0).

## Files shipped

- `core/frontend/api_client.py` (new)
- `core/frontend/app.py` (modified — extended from `/health`-only stub)
- `core/frontend/templates/{base,index,job_detail,shortlist_list,shortlist_entry,resume_detail,match_results,error}.html` (new)
- `core/tests/unit/test_frontend_api_client.py` (new)
- `core/tests/unit/test_frontend_routes.py` (new)
- `core/tests/unit/test_gates_cover_frontend.py` (new)
- `Makefile` (modified — gate-scope widening)
- `.github/workflows/ci.yml` (modified — gate-scope widening)
- `core/tests/evals/run_evals_live.py` (new, post-review addition, 2026-07-17 — live end-to-end eval
  harness against the real stack; see "Live end-to-end eval (post-review addition)" above)
- `core/tests/unit/test_evals_live_metrics.py` (new, post-review addition, 2026-07-17 — 16 offline tests
  for the live harness's pure metric layer)

## Live end-to-end eval (post-review addition)

**Reversed 2026-07-17.** ADR-013 §5 originally recorded a live end-to-end eval as deferred — the one
genuine verification gap Phase 7 left open. After the viewer's own gates went green and PR #16 was opened,
the human reversed that decision and made a live run a **prerequisite for merging PR #16**. It was then
built, run, and independently reproduced. Full decision text: [ADR-013 §5](../adr/013-phase7-evals-viewer.md).

**What was built.** `core/tests/evals/run_evals_live.py` (new, 812 lines) drives the 4a/4c corpus through
the real stack: real `nomic-embed-text` 768-d embeddings, the real Neo4j skill graph, the real
`shortlist_job` orchestrator, and real Postgres persistence. `core/tests/unit/test_evals_live_metrics.py`
(new, 16 tests) offline-unit-tests the pure metric layer (`eval_*` functions) against known-bad rankings —
weak-in-topk, a fabricated surfaced quote, a dropped gold anchor, reversed/tied ordering, a PII leak, score
drift — asserting each one FAILS. The live orchestration script itself lives under `core/tests/evals/`, not
`core/tests/unit/`, so `pytest tests/unit` never collects it and CI stays green with no Ollama reachable.

**Design (faithfulness).** The 4a corpus is pre-parsed by design — no raw résumé/JD documents, only
pre-parsed JSON, because 4a fixed the parsed representation to isolate ranking quality from
non-deterministic LLM parsing. So the harness does **not** literally "upload the corpus through Phase 6's
HTTP routes" (ADR-013 §5's original wording) — that would re-introduce parse variance the corpus was built
to exclude and drift every calibrated threshold. Instead it seeds the corpus at the **post-parse
boundary**: a `jobs` row + 20 `resumes` rows with `parsed` jsonb, PII encrypted through the real `pii.py`
path, and `job.parsed`/`resume.parsed` outbox events carrying real `nomic-embed-text` embeddings computed
through the production embed boundary (with PII redaction applied). From there it drives the real
`project_to_graph` (Neo4j) → real `shortlist_job` → reads the persisted `shortlist_entries` rows →
evaluates every gate in `thresholds.toml`. It reuses `run_evals.load_corpus`/`load_thresholds`/`_labels`
(no duplication) and calls the **real** `stages.verify_evidence` and the **real** redaction functions for
the anti-fabrication and PII gates, not `run_evals.py`'s offline stand-ins. `project_to_graph`/
`shortlist_job` ran with a directly-constructed `ctx` (not dequeued off arq/Redis) — identical production
task code and live dependencies, but the queue hop itself is unexercised. Ran against a remote Ollama with
the calibrated models (`nomic-embed-text` + `gpt-oss:20b`); the local metal host lacked them.

**Verified results — reproduced identically on two independent runs, exit 0 both times:**

| Gate | Result |
|---|---|
| `precision@5` | 1.000 (top-5 all `strong`) |
| adversarial bait (r09) | ranked 14th, outside k=5 — `must_not_surface`: no offenders |
| `evidence.verification_rate` | 78/78 = 1.000 |
| `evidence.min_completeness_in_topk` | 5/5 = 1.000 |
| `evidence.gold_recall` | 4/4 = 1.000 |
| `evidence.negative_evidence_must_fail` | 4 fabrications, all scrubbed |
| `ordering_controls` | education +0.0411, overqual +0.0120, motivation +0.0900, skill_missing_must +0.1460, recency +0.1440 — all pass |
| `pii.embedding_input_pii_free` | 0 leaks / 20 fixtures |
| `pii.exported_output_pii_free` | 0 leaks / top-5 |
| determinism | order identical, `max_rank_delta=0`, `max_score_delta=0` |

The measured ordering gaps track `labels.json`'s arithmetic predictions against a real embedder closely
(overqual +0.0120 exact; education +0.0411 vs the ~0.0391 predicted) — evidence the real pipeline
reproduces the calibrated behaviour the corpus's thresholds assume, not just what the offline stand-in
reproduces.

**Deviations and residuals, recorded honestly:**
- ADR-013 §5's original literal "HTTP upload" wording is intentionally not followed — see "Design" above.
- `project_to_graph`/`shortlist_job` ran with a real `ctx` constructed directly, not enqueued on the arq
  worker — identical production task code + live dependencies, but bypasses the queue hop.
- The second determinism run used a warm Redis embed cache, so the embedding half of the comparison is
  cache-vs-itself, not model-vs-itself on a cold cache; `gpt-oss:20b`'s greedy decode is not guaranteed
  bit-stable, so a nonzero score drift on the generation half would have been a real observation — it
  measured exactly zero.
- The `jd.education.fields` open decision (ADR-009 §7) remains unresolved and untouched.

**Gate effect.** Offline suite grew from 2229 to **2245 unit tests @ 91.67% coverage** (+16, the new
`test_evals_live_metrics.py` tests); ruff/black/mypy remain clean.

## Carried forward, still unresolved

- **`score_education` ignores `jd.education.fields`** (ADR-009 §7, restated through ADR-012) — Phase 7
  touches no scoring code, so this is untouched again.
- **`reverse_match_job`'s `allowed_job_ids` filter** (ADR-010 §4, restated ADR-012 §3) — still
  `description_parsed IS NOT NULL`, not `status = 'open'`, unaffected by this phase.
- **No advisory lock on concurrent shortlist/reverse-match runs** (ADR-010 §1, restated ADR-012) — the
  viewer is read-only and triggers no writes, so this is unaffected by Phase 7.
- **Live end-to-end eval against the real HTTP-adjacent/Postgres/Neo4j/Ollama path** (ADR-013 §5) — no
  longer a gap. Built, run, and PASSED, reproduced twice — see "Live end-to-end eval (post-review
  addition)" above. It does not exercise the Phase 6 HTTP upload/parse routes themselves or the arq/Redis
  queue hop; those remain covered only by Phase 3/4/6's own tests.
