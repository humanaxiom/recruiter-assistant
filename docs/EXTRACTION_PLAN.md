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
| &nbsp;&nbsp;**4a · Evals corpus** | `core/tests/evals/` labelled resumes-vs-JD fixtures + `thresholds.toml` (precision@k, evidence-verification-rate, PII-leak, determinism) — **zero product code**; built first so the matching engine's first green build is falsifiable | 🔄 in progress |
| &nbsp;&nbsp;**4b · Graph projection** | Outbox drainer `project_to_graph` (job+resume → Neo4j; **must NOT project `parsed.candidate` or log payload**; chunk-text preview read from `resumes.parsed`, NOT the outbox — ADR-007 stripped it) + Neo4j skill-graph half of `skill_normalize` (+ `categories.yaml`) | not started |
| &nbsp;&nbsp;**4c · Matching engine** | `stages` (pure scoring fns) + `orchestrator` (stage 1–4) + `MatchWeights` settings wiring (`weights_from_settings`) + `shortlist_evidence_v1`/`_v2` prompts — opus-tier; first real `ranking-evals` gate | not started |
| &nbsp;&nbsp;**4d · Shortlist + reverse-match jobs** | `shortlist_job`, `reverse_match_job` arq tasks + write-only `persist_shortlist`/`persist_reverse_match` + `match_resume_to_jobs` + worker wiring. (list/get/export → Phase 5) | not started |
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

**As of this writing — Phases 0–3 are merged to `main`, CI green. Phase 4 (Ranking engine) is 🔄 IN
PROGRESS, split into 4 gated sub-phases** (planner pass 2026-07-12). Sub-phase **4a (evals corpus)** is
active on branch `feat/phase-4a-ranking-evals-corpus`; 4b→4c→4d follow, each on its own branch/PR with
the full reviewer/security/ranking-evals gate before the next starts. The split mirrors Phases 0–3's
one-phase-per-PR cadence — Phase 4 is larger than Phase 3 (which took 4 audit rounds even scoped
tighter), and 4b/4c carry the security-sensitive PII-boundary + scoring-correctness surface, so
isolating them keeps each diff auditable.

**Phase-4 decisions adopted from the planner pass** (recommended defaults; reversible):
- **Chunk-text preview source (required deviation, Risk #1):** hris's `_resume_projection_tx` reads
  `chunk.text` off the outbox payload, but ADR-007 stripped `chunks[].text` from the outbox in Phase 3.
  4b MUST instead read chunk text from `resumes.parsed` (Postgres) inside the projection — a verbatim
  port would `KeyError`/write empty `ResumeChunk.text_preview`. Evidence citations (stage 3) likewise
  source chunks from `resumes.parsed`, never the outbox.
- **Reverse-match evidence depth** `match_reverse_evidence_k > 0` — recruiter-assistant runs reverse
  match only on the async `reverse_match_job` path (no synchronous endpoint to protect), so port hris's
  worker-path default, not its sync-timeout `k=0`.
- **`skill_category_task` (LLM category backfill cron) deferred** out of Phase 4 — `_ensure_categories`'
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
