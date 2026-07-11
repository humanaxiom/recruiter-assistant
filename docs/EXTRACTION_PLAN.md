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
| **1 · Storage** | Filesystem `BlobStore` replacing every MinIO call site | next |
| **2 · Schemas** | Port `resumes`, `matching` (minus review), `jobs` | not started |
| **3 · Ingest + parse** | `extract`/`chunk`, LLM client+cache, `parse_resume`/`parse_job`, cover-letter parse, **PII encryption on parse** | not started |
| **4 · Ranking engine** | `orchestrator` + `stages` (4-stage hybrid), `MatchWeights`, `shortlist_job`, `reverse_match_job` | not started |
| **5 · Persist + anonymize + export** | Trimmed `shortlist_service`, `redaction` (blind-default), csv/evidence-csv/json export with `reveal` | not started |
| **6 · API** | Routes: job create/parse, resume upload, shortlist generate/list/get/export, reverse-match; minimal auth | not started |
| **7 · Evals + viewer** | Ranking-quality fixtures (precision@k, evidence-verification rate); minimal Flask viewer | not started |

## Subagent structure

Template roles (planner · tester · coder · reviewer · security · docs) plus two domain additions:

- **`data-pipeline`** — coder variant carrying the ranking domain contract (4-stage algorithm, Neo4j Cypher, asyncpg jsonb, 768-d invariant, PII-never-in-embeddings). Drives phases 1–6.
- **`ranking-evals`** — merge-blocking quality gate for pipeline phases: runs a fixture corpus (known good/bad resumes vs a JD) and gates on precision@k + evidence-verification rate. Unit tests prove code runs; this proves ranking *quality*.

Per-phase flow: planner → tester (+ evals fixture) → data-pipeline coder (ReviewLoop) → reviewer + security + ranking-evals (all merge-blocking) → docs. Security enforces PIPEDA/FIPPA, offline-egress, and the PII-not-in-embeddings invariant on every diff.

## Current status & next step

**As of this writing — Phase 0 is complete and green. Phase 1 is next.**

Done:
- Repo created: **`github.com/humanaxiom/recruiter-assistant`** (private). Local `origin` repointed here. Frozen template stays at `adamsalah13/agent-harness-template`.
- This plan + the `data-pipeline` and `ranking-evals` subagents committed to `main`.
- The 4 decisions above are locked.
- **Phase 0 · Seed & infra — landed on `feat/phase-0-seed-infra`, pushed to origin.** Six commits (red → green → three review fixes → docs). What landed:
  - Template demo app removed; ranking-domain foundation in its place. Rebrand to `recruiter-assistant` across `pyproject.toml` / README / compose.
  - `docker-compose.yml`: pg/neo4j/redis/ollama, **no MinIO**, `./data` bind mount for the filesystem BlobStore.
  - `settings.py`: `llm_embedding_dim = 768` (single source of the contract), `storage_dir`, LLM base url, Neo4j creds.
  - **asyncpg idempotent startup DDL** (`init_schema`) for 5 tables (`jobs`, `resumes` (+PII BYTEA cols), `shortlist_entries`, `reverse_match_entries`, `outbox`); SQLAlchemy dropped from requirements.
  - **Neo4j bootstrap**: 4× 768-d cosine vector indexes (`resume_summary_idx`, `job_summary_idx`, `skill_emb_idx`, `chunk_emb_idx`) + skill-graph constraints, dimension derived from `settings.llm_embedding_dim`.
  - Three deliberate schema deviations from hris recorded in **ADR-004** (blind_review DEFAULT TRUE; nullable-TEXT actor cols; `score_final` unified to `DOUBLE PRECISION` with a 0–1 CHECK).
  - **Gates:** offline green — ruff / black / mypy --strict, 172 unit tests, coverage 88.79%. Integration green — 39 tests against real Postgres + Neo4j.

**Checklist reconciliation note (`core/src/gates/`):** the Phase 0 checklist said "keep `gates/`". The template demo's product-code gate-runner module (`core/src/gates/`, alongside `core/src/agents|memory` and `models/db.py`) **was deleted** — it was demo code the ranking domain replaces. "Keep gates" was (correctly) read as the **build** harness: `make gates`, CI, `.claude/`, and pre-commit are all intact. Don't mistake the deleted demo module for the still-live gate suite.

**Immediate next step — Phase 1 (Storage):** filesystem `BlobStore` (put/get/delete over `./data/resumes/{id}`) replacing every MinIO call site — `resume_service.py` put/remove and `resume_tasks._fetch_blob` get (see Appendix A, "MinIO → filesystem").

Two requirements carried forward from Phase 0's security gate — **Phase 1 acceptance criteria, must land in `BlobStore`'s first commit:**
1. **Path-traversal rejection.** `blob_key` / `cover_letter_blob_key` are unvalidated `TEXT` and `storage_dir` is a bind-mounted root, so `BlobStore` must reject `..`, absolute paths, and symlinks that escape `storage_dir` — from its first commit, not as a follow-up.
2. **STRICT PII-key GUC read.** When wiring `settings.pii_key` into the `app.pii_key` GUC, use `current_setting('app.pii_key')` **without** `missing_ok=true`. A `missing_ok` read of an unset key yields NULL → NULL ciphertext → silent data loss; fail loud instead.

Then Phases 2–7 per the table above.

## Appendix A — hris source file map (minimal port set)

Exact paths in `C:\repos\hris` to port from (avoids re-exploring). Everything here is confirmed free of review/JD coupling except where noted.

**Schemas** (`packages/schemas/src/schemas/`): `resumes.py`, `matching.py` (drop review types: `PipelineStage`, `DispositionReason`, `ShortlistDecision*`, `StageTransition*`; drop `ShortlistEntry.current_decision/current_stage`), `jobs.py` (`Skill`, `JDExtracted`).

**Pipeline — port near-verbatim** (`packages/pipeline/src/pipeline/`): `config.py`; `llm/{client.py, cache.py}`; `parsing/{extract.py, chunk.py}`; `matching/{orchestrator.py, stages.py, config.py}`. (Do NOT copy `pipeline/bank/`, `pipeline/quality/`, `pipeline/sources/` — those are JD-Harmonizer.)

**Prompts** (`packages/prompts/.../templates/`): `resume_core_v1`, `resume_skills_v2`, `shortlist_evidence_v1` (+`_v2`), `cover_letter_v1` (+`_v2`) — each `.system.j2` + `.user.j2`.

**Worker** (`apps/worker/src/worker/`): `resume_tasks.py` (`parse_resume`, `project_to_graph`, `_build_summary_text` — the PII-exclusion point), `tasks.py` (`parse_job`), `shortlist_task.py`, `reverse_match_task.py` (v1 includes reverse match), `skill_normalize.py`, `skill_category_task.py`, `neo4j_bootstrap.py` (4 vector indexes, 768-d cosine: `resume_summary_idx`, `job_summary_idx`, `skill_emb_idx`, `chunk_emb_idx`; nodes Job/Resume/ResumeChunk/Skill/Company/Institution; rels HAS_CHUNK/HAS_SKILL/REQUIRES/SHORTLISTED), `main.py` + `config.py` (prune to ranking jobs only).

**API services** (`apps/api/src/api/services/`): `pii.py` (pgcrypto `pgp_sym_encrypt`, session key `app.pii_key`), `redaction.py` (`redact_text`, `pseudonym` — blind default ON), `resume_service.py` (upload/store/encrypt — **replace MinIO calls with BlobStore**), `match_service.py`, `shortlist_service.py` (**trim**: keep `persist_shortlist`/`list_for_job`/`get_one`/`export_rows`; drop `record_decision`/`transition_stage` and strip the `shortlist_decisions`/`stage_transitions` sub-selects & joins from read/export SQL).

**API routes** (`apps/api/src/api/routes/`): `resumes.py`, `shortlist.py` (**trim**: keep generate/list/get/export; drop `/decision`, `/stage`, the `collab_service` import, and review CSV columns), `jobs.py` (minus the JD-comments block; keep `jd_import_service.extract_jd_text` — used by plain job creation), reverse-match route.

**Infra glue**: `apps/api/src/api/minio_client.py` → **replace** with `storage/BlobStore`; `apps/api/src/api/{arq.py, db/session.py, config.py}` → adapt to template settings. Skill data: `infra/skills/{aliases.yaml, categories.yaml}`.

**Postgres DDL reference** (`apps/api/migrations/versions/` — read the raw SQL, re-express as startup DDL): `0002` jobs+outbox, `0003` resumes+PII (BYTEA name/email/phone, `candidate_email_hash`), `0004` `shortlist_entries` (split from `shortlist_decisions`), `0013` cover-letter cols, `0015` `reverse_match_entries`.

**MinIO → filesystem**: MinIO is used only in `resume_service.py` (put/remove) and `resume_tasks.py` `_fetch_blob` (get). Replace with `BlobStore.{put,get,delete}` over `./data/resumes/{id}`. The `resume-previews` bucket is unused — ignore it.

**Contract invariants (enforce in review/evals):** 768-d `nomic-embed-text` cosine everywhere (must match Neo4j indexes); embeddings exclude name/email/phone; evidence quotes verified ≥0.85 against cited chunk or blanked; weights only via `MatchWeights` from settings.
