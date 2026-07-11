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

| Phase | Deliverable |
|---|---|
| **0 · Seed & infra** | Repo from template; compose (pg/neo4j/redis/ollama, no minio) + `data/` volume; settings (768-d, storage dir); DDL + Neo4j bootstrap on startup |
| **1 · Storage** | Filesystem `BlobStore` replacing every MinIO call site |
| **2 · Schemas** | Port `resumes`, `matching` (minus review), `jobs` |
| **3 · Ingest + parse** | `extract`/`chunk`, LLM client+cache, `parse_resume`/`parse_job`, cover-letter parse, **PII encryption on parse** |
| **4 · Ranking engine** | `orchestrator` + `stages` (4-stage hybrid), `MatchWeights`, `shortlist_job`, `reverse_match_job` |
| **5 · Persist + anonymize + export** | Trimmed `shortlist_service`, `redaction` (blind-default), csv/evidence-csv/json export with `reveal` |
| **6 · API** | Routes: job create/parse, resume upload, shortlist generate/list/get/export, reverse-match; minimal auth |
| **7 · Evals + viewer** | Ranking-quality fixtures (precision@k, evidence-verification rate); minimal Flask viewer |

## Subagent structure

Template roles (planner · tester · coder · reviewer · security · docs) plus two domain additions:

- **`data-pipeline`** — coder variant carrying the ranking domain contract (4-stage algorithm, Neo4j Cypher, asyncpg jsonb, 768-d invariant, PII-never-in-embeddings). Drives phases 1–6.
- **`ranking-evals`** — merge-blocking quality gate for pipeline phases: runs a fixture corpus (known good/bad resumes vs a JD) and gates on precision@k + evidence-verification rate. Unit tests prove code runs; this proves ranking *quality*.

Per-phase flow: planner → tester (+ evals fixture) → data-pipeline coder (ReviewLoop) → reviewer + security + ranking-evals (all merge-blocking) → docs. Security enforces PIPEDA/FIPPA, offline-egress, and the PII-not-in-embeddings invariant on every diff.

## Open item

**Repo:** this working copy currently tracks `agent-harness-template`. Before Phase 0 commits, create the `recruiter-assistant` repo from the template and repoint `origin` so the real project has its own history.
