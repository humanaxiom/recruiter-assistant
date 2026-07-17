# Phase 6 — API routes

**Status:** built and gate-green on branch `feat/phase-6-api-routes`, off `main` @ `6deade3`, tip
`837de9e`. **All three merge-blocking gates green (reviewer APPROVE, security PASS, ranking-evals PASS) —
re-verified after the security-hardening round.** NOT yet opened as a PR — a PR opens after a human
check-in. CI (`gates-all`, including a live `run_evals.py` re-measurement against Ollama) has not run
since no PR exists yet.

This phase ships the HTTP surface over everything Phases 3–5 built: job create/read/list/status, résumé
upload/read/list, shortlist generate/list/get/export, reverse-match trigger/read. It is the first phase
where any of that service-layer code runs behind a real ASGI request rather than being called directly
from a test. Full decisions and residuals: [ADR-012](../adr/012-api-routes-auth-upload-scope.md).

## TDD sequence

Three RED/GREEN pairs — an initial build, then two hardening cycles surfaced by the security gate after
first green (one of which was resumed mid-session after a session-limit interruption):

| Commit | Label | What it added |
|---|---|---|
| `209bff7` | `red` | Failing unit + integration tests for `api/deps.py`, `api/routes/{jobs,resumes,shortlist}.py`, `services/zip_upload.py`, `services/jd_import_service.py`, and the `main.py` router/exception-handler wiring — none of which existed yet. Fails at collection/import. |
| `bc9a3d6` | `green` | The initial route implementation: all eleven routes, `require_api_key`/`resolve_actor`/`get_arq` deps, zip expansion, JD-extract helper, the `AppError` → JSON exception handler, `job_service`/`resume_service`/`shortlist_service` extensions for create/upload/transition/reverse-match-read. Minimal implementation to turn every RED test green. This pass also hit a mid-build session-limit interruption and was resumed in a later session before the round completed. |
| `1f2b161` | `red` | Security-gate findings (round 2) from the first full audit, written as failing regression tests FIRST: SEC-1 (non-ASCII `X-API-Key` crashes `require_api_key` into an unhandled 500 instead of a 401), SEC-2 (upload file-count cap checked after some file bodies were already read into memory), SEC-4 (`X-Actor-Name` unbounded). |
| `344f6bf` | `green` | Fixes for SEC-1/SEC-2/SEC-4 (ADR-012 "Security hardening" section) plus exact `==` pins on `fastapi`/`starlette`/`python-multipart` (the repo's route-walker test depends on a FastAPI-internal structure, so an unpinned minor bump could silently break the gate elsewhere). |
| `c75f4a7` | `red` | A second security re-audit found the round-2 SEC-1 fix incomplete on one path, and a reviewer finding pinned the SEC-2 file-count-cap ordering as a permanent regression test rather than a one-off fix: failing tests for the non-ASCII-key 401 case on a second code path and an explicit upload-ordering pin (files rejected on count BEFORE `UploadFile.read()` is ever called on any of them). |
| `837de9e` | `green` | Both closed: the non-ASCII `X-API-Key` 401 fix generalized (UTF-8-byte comparison, not `str`), and the upload-ordering regression test locked in. HEAD of the branch. All three merge-blocking gates re-verified green here. |

## What each new/modified module does (see ADR-012 for full rationale)

1. **`core/src/api/deps.py` (new)** — `require_api_key` (the configurable auth switch: empty
   `settings.api_key` disables auth entirely, non-empty enables fail-closed 401 via
   `secrets.compare_digest` on UTF-8 bytes), `resolve_actor` (optional `X-Actor-Name`, capped at 128
   chars, default `"api"`), `get_arq` (hands the lifespan-built arq pool to routes, mirroring
   `get_db`/`get_blob_store`), `log_auth_mode` (called once at API startup — loud `WARNING` when auth is
   disabled).
2. **`core/src/api/routes/jobs.py` (new)** — `POST /jobs` (create + enqueue `parse_job`), `POST
   /jobs/jd-extract` (pre-fill helper, no DB write — declared before `/jobs/{job_id}` so the literal
   segment can't be swallowed by the path param), `GET /jobs` (list, paginated + status filter), `GET
   /jobs/{job_id}`, `PATCH /jobs/{job_id}/status` (the one status-mutating route — forward-only, 409 on
   an invalid transition).
3. **`core/src/api/routes/resumes.py` (new)** — `POST /jobs/{job_id}/resumes` (multipart upload: multiple
   files or one `.zip`, expanded via `zip_upload.expand_zip_entries` and merged into the same
   accepted/rejected accounting; `parse_resume` enqueued once per accepted résumé, after the upload
   transaction commits), `GET /jobs/{job_id}/resumes` (list), `GET /resumes/{resume_id}` (redaction
   happens inside `resume_service.get_one`, never re-queried raw at the route), and the reverse-match
   subresource: `POST /resumes/{resume_id}/match-jobs` (existence-probe then enqueue `reverse_match_job`)
   and `GET /resumes/{resume_id}/match-results` (no redaction — ADR-012 §4).
4. **`core/src/api/routes/shortlist.py` (new)** — `POST /jobs/{job_id}/shortlist` (enqueue
   `shortlist_job`), `GET /jobs/{job_id}/shortlist/export` (csv / evidence-csv / json, `reveal` query
   param, `set_pii_key` run inside an open transaction before `export_rows`), `GET
   /jobs/{job_id}/shortlist` (list), `GET /shortlist/{entry_id}` (get one). The review-workflow
   decision/stage routes from hris are deliberately absent — hitting them is FastAPI's own unmatched-route
   404, never a 401/403.
5. **`core/src/services/zip_upload.py` (new)** — `expand_zip_entries`/`ZipRejected`: mirrors the Phase-3
   DOCX decompression-bomb defense (never trusts `ZipInfo.file_size`, streams and sums real decompressed
   bytes), plus path-traversal-entry rejection, a résumé-extension allowlist, and per-entry (10 MB) /
   total (100 MB) / entry-count (50) caps. A pure function — no `BlobStore`/DB parameter, so it cannot
   write anything on either the accept or the reject path.
6. **`core/src/services/jd_import_service.py` (new)** — `extract_jd_text`: pulls plain text out of an
   uploaded txt/json/pdf/docx for the `/jobs/jd-extract` pre-fill route.
7. **`core/src/api/main.py` (modified)** — registers the three Phase-6 routers, adds an
   `AppError` → JSON exception handler (`{"code", "message", **context}`), calls `log_auth_mode` in the
   lifespan, and parks the arq pool on `app.state.arq`.
8. **`core/src/models/pool.py` (modified — latent-bug fix)** — `PoolConnectionProxy[Record]` isn't
   subscriptable at runtime (generic only in the stubs); under `from __future__ import annotations` +
   FastAPI's `eval_str` signature introspection of `get_db` as a `Depends` sub-dependency, this crashed at
   route-registration time — latent since Phases 0–5 never exercised `Db` through a live FastAPI
   dependency graph. Fixed with a `TYPE_CHECKING`-gated `_ConnT` alias (ADR-012 "pool.py latent-bug fix").
9. **`core/src/services/{job_service,resume_service,shortlist_service}.py` (modified)** — extended with
   `create_job`/`get_job`/`list_jobs`/`transition_status` (jobs); `upload_resumes` (resume, now
   file-count-capped before any read); `get_reverse_match_result` (shortlist, the reverse-match read
   path).
10. **`core/src/settings.py` (modified)** — `api_key: str = ""` (the auth switch, ADR-012 §1).
11. **`core/src/errors.py` (modified)** — `FileRejectedError` (413, upload rejected before any body read).
12. **`core/requirements.txt` (modified)** — `python-multipart` added (required for multipart/form-data
    uploads); `fastapi`/`starlette`/`python-multipart` pinned exactly.

## Route map

| Method | Path | Purpose |
|---|---|---|
| POST | `/jobs` | Create a draft job, enqueue `parse_job` |
| GET | `/jobs` | List jobs (paginated, status filter) |
| GET | `/jobs/{id}` | Get one job |
| PATCH | `/jobs/{id}` | (schema-level update — see `JobUpdate`) |
| PATCH | `/jobs/{id}/status` | The only status-mutating route; forward-only, 409 on invalid transition |
| POST | `/jobs/jd-extract` | Pre-fill helper — extract JD text, no DB write |
| POST | `/jobs/{id}/resumes` | Upload résumés (multi-file or zip) |
| GET | `/jobs/{id}/resumes` | List résumés for a job |
| GET | `/jobs/{id}/shortlist` | List shortlist entries |
| PATCH | `/jobs/{id}/shortlist` | (schema-level — see `ShortlistEntry`) |
| GET | `/jobs/{id}/shortlist/export` | Export csv / evidence-csv / json, `reveal` param |
| GET | `/resumes/{id}` | Get one résumé (redacted under blind review) |
| POST | `/resumes/{id}/match-jobs` | Trigger reverse-match (enqueue `reverse_match_job`) |
| GET | `/resumes/{id}/match-results` | Read reverse-match result — NO redaction (ADR-012 §4) |
| GET | `/shortlist/{id}` | Get one shortlist entry |

## Locked human decisions this phase (see ADR-012 for full text)

1. Configurable auth switch — one settings flag, empty = disabled (loud startup warning), non-empty =
   fail-closed with constant-time comparison; optional `X-Actor-Name` (128-char cap) populates
   `created_by`/`uploaded_by`.
2. Upload = local multi-file + zip only; Taleo/manifest connector explicitly CUT and deferred to a future
   separate connectors feature.
3. `PATCH /jobs/{id}/status` (draft→open) is the only status-mutating route — forward-only, 409 on invalid
   transition. First code path to ever transition `jobs.status`; ADR-010 §4's `allowed_job_ids` note is
   revisited but NOT resolved (still `description_parsed IS NOT NULL`).
4. Reverse-match is a subresource of `routes/resumes.py`; explicitly NO redaction on the reverse-match
   read (the caller owns the résumé they matched — no third party to protect).
5. `POST /jobs/jd-extract` pre-fill helper included, no DB write.

## Carry-forwards now CLOSED

- **`JobOut.blind_review` fail-open (ADR-006 §4 note)** — `_row_to_jobout` now sets `blind_review`
  explicitly from the row on every construction path; reviewer mutation-proved both directions.
- **Redaction boundary at the HTTP layer (ADR-006 §4 / ADR-011)** — read/export routes route straight
  through to the already-redacting service functions; security byte-scanned actual serialized HTTP
  responses (not just service-layer return values) and confirmed no raw PII byte-sequence in a blind
  response.

## Reviewer, security, ranking-evals — verdicts

**Final verdict, HEAD `837de9e`:** reviewer **APPROVE** (six mutation obligations fired, including the
`blind_review` fail-open both-directions guard and the zip-bomb entry/total-cap guards); security **PASS**
(SEC-1 and SEC-2 closed on re-audit — see the two hardening RED→GREEN cycles above); ranking-evals **PASS**
(scoring code byte-unchanged: `stages.py`/`orchestrator.py` untouched by this phase; CI's `gates-all` will
re-measure `run_evals.py` live on the PR).

| # | Guard | Would fail if... | Test class |
|---|---|---|---|
| 1 | Non-ASCII `X-API-Key` returns 401, never a 500 | the UTF-8-byte-encode step before `compare_digest` was skipped | targeted unit test with a non-ASCII header value |
| 2 | Upload file-count cap checked before any file body is read | the ordering regressed to read-then-count | regression test asserting zero `UploadFile.read()` calls on a rejected batch |
| 3 | `X-Actor-Name` truncated at 128 chars, never overflows `created_by`/`uploaded_by` | the cap was removed or mis-sized | unit test with an oversized header |
| 4 | `JobOut.blind_review` reflects the row in both directions (blind stays blind; non-blind stays non-blind) | the explicit row-read was dropped, falling back to the schema's `False` default | mutation test on `_row_to_jobout` |
| 5 | A zip entry that decompresses past the per-entry or total cap is rejected before any accepted entry is written | the streaming cap check was removed or the declared `file_size` trusted instead | zip-bomb fixture test |
| 6 | An invalid job-status transition returns 409, not 200 or an unguarded write | `transition_status`'s forward-only graph check was bypassed | transition-matrix test |

## Final gate state — HEAD `837de9e`

- Offline: ruff / black / `mypy --strict` clean.
- **2156 unit tests @ 91.68% coverage.**
- **123 integration tests** vs real Postgres+Neo4j+Redis, including 12 new Phase-6 ASGI integration tests
  (real HTTP requests through the FastAPI app, not mocked route functions).
- **All three merge-blocking gates green:** reviewer APPROVE (guard table above), security PASS (SEC-1/
  SEC-2 closed on re-audit), ranking-evals PASS (no scoring code touched).

## Accepted-for-v1 residuals (see ADR-012 for full detail — not restated here)

- **SEC-3** — no `LIMIT`/`OFFSET` on shortlist list/export/reverse-match reads; bounded in practice by
  shortlist size.
- **SEC-5** — `detect_mime`'s `txt` catch-all — intentional, carried from Phase 3.
- **Blob-write-inside-transaction** — an upload blob is written inside the DB transaction; a rollback
  leaves a uuid-keyed orphan blob on disk (harmless wasted bytes, no orphaned enqueue).

## Carried forward, still unresolved

- **`score_education` ignores `jd.education.fields`** (ADR-009 §7, restated ADR-010 §5, ADR-011) — Phase 6
  touches no scoring code, so this is untouched again.
- **`reverse_match_job`'s `allowed_job_ids` filter** (ADR-010 §4) — still `description_parsed IS NOT
  NULL`, not `status = 'open'`, even though a status route now exists (ADR-012 §3) — the natural follow-up
  once someone touches `matching_tasks.py` again, not done in this routes-only phase.
- **`redacted_filename`'s `os.path.splitext` truncation-leak** (ADR-011 residual) — not addressed by this
  phase's upload validation; still a LOW residual.
- **No advisory lock on concurrent shortlist/reverse-match runs** (ADR-010 §1) — `POST
  /jobs/{id}/shortlist` and `POST /resumes/{id}/match-jobs` are the first user-facing routes that can
  trigger a regenerate; the lock question ADR-010 flagged as "revisit once Phase 6 ships a user-facing
  regenerate route" is now live and still unresolved.
