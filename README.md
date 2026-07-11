# recruiter-assistant

> Local-first, evidence-backed resume ranking. Upload a job and a stack of resumes; get a ranked shortlist where every claim is a quote verified against the candidate's own document. Inference runs on **Ollama on the host** — no candidate data ever leaves the machine.

Ported from the resume-ranking feature of an internal HRIS onto the offline-first agent harness. The review workflow, JD-Harmonizer, and cloud object storage were dropped; anonymization, the 4-stage ranking engine, shortlists, reverse-match, and exports were kept. See [docs/EXTRACTION_PLAN.md](docs/EXTRACTION_PLAN.md) for the full keep/cut boundary and the phased build.

---

## What it does

- **Blind by default** — candidate name / email / phone are redacted in the viewer and excluded from embeddings; reveal is opt-in and audited (decision 4).
- **Evidence-backed** — the LLM produces per-requirement evidence, then an anti-fabrication pass fuzzy-matches every quote (≥ 0.85) against its cited resume chunk; unverifiable quotes are blanked.
- **Hybrid ranking** — Neo4j vector recall + a structured skill/experience/education/seniority score + evidence completeness + motivation.
- **Offline** — all model calls go through an OpenAI-compatible client pointed at Ollama on `host.docker.internal:11434`. No cloud endpoint exists anywhere in the code or compose.

---

## System architecture

```mermaid
graph TB
    subgraph Metal["Bare Metal (Host)"]
        OL[Ollama<br/>:11434 /v1<br/>gpt-oss:20b · nomic-embed-text]
    end

    subgraph Docker["Docker Compose"]
        subgraph AppTier["App Tier"]
            API[FastAPI<br/>:8000]
            WK[arq Worker<br/>parse · rank · reverse-match]
            FE[Flask Viewer<br/>:5000 · read-only]
        end
        subgraph DataTier["Data Tier"]
            PG[(PostgreSQL 16<br/>asyncpg · pgcrypto PII)]
            NEO[(Neo4j 5<br/>skill graph + 768-d vector)]
            RD[(Redis 7<br/>arq broker + embed cache)]
            BLOB[["./data<br/>filesystem BlobStore"]]
        end
    end

    FE -->|REST| API
    API --> PG
    API --> NEO
    API -->|enqueue| RD
    API --> BLOB
    WK -->|dequeue| RD
    WK --> PG
    WK --> NEO
    WK --> BLOB
    API & WK -->|host.docker.internal:11434| OL

    style Metal fill:#2D3436,color:#fff
    style AppTier fill:#1F6FEB,color:#fff
    style DataTier fill:#F59F00,color:#fff
```

**Store responsibilities**

| Store | Role |
|---|---|
| **PostgreSQL** (raw asyncpg) | Transactional data: jobs, resumes, shortlist / reverse-match entries, outbox. Candidate PII is encrypted at rest with pgcrypto. |
| **Neo4j** | Skill / experience graph + four 768-d cosine vector indexes for coarse recall and skill-graph scoring. |
| **Redis** | arq task broker + embedding cache. No domain data. |
| **`./data` BlobStore** | Original resume + cover-letter files on the local filesystem — MinIO was dropped (see ADR-004). |

There is **no migration framework**: `init_schema` (Postgres) and `bootstrap_neo4j_schema` (Neo4j) run idempotently on every API/worker boot.

---

## Data model (Postgres)

```mermaid
erDiagram
    jobs ||--o{ resumes : "has"
    jobs ||--o{ shortlist_entries : "ranks"
    jobs ||--o{ reverse_match_entries : "scored against"
    resumes ||--o{ shortlist_entries : "appears in"
    resumes ||--o{ reverse_match_entries : "matched to"

    jobs {
        uuid id PK
        text title
        text description_raw
        jsonb description_parsed
        job_status status
        bool blind_review "DEFAULT TRUE"
        text created_by "nullable actor label"
    }
    resumes {
        uuid id PK
        uuid job_id FK
        text blob_key "filesystem BlobStore"
        bytea candidate_name "pgcrypto"
        bytea candidate_email "pgcrypto"
        bytea candidate_phone "pgcrypto"
        text candidate_email_hash "plaintext sha256"
        bytea cover_letter_text "pgcrypto"
        resume_status status
        bool consent_acknowledged "no default"
    }
    shortlist_entries {
        uuid id PK
        int rank "CHECK > 0"
        float8 score_final "CHECK 0..1"
        jsonb evidence
    }
    reverse_match_entries {
        uuid id PK
        int rank "CHECK > 0"
        float8 score_final "CHECK 0..1"
        jsonb score_breakdown
    }
    outbox {
        bigserial id PK
        text aggregate
        uuid aggregate_id "polymorphic, no FK"
        jsonb payload
    }
```

Five tables (`jobs`, `resumes`, `shortlist_entries`, `reverse_match_entries`, `outbox`). The four PII columns (`candidate_name` / `candidate_email` / `candidate_phone` / `cover_letter_text`) are `BYTEA` encrypted via `pgp_sym_encrypt` under the `app.pii_key` GUC; only `candidate_email_hash` is plaintext, and only so subject-access requests can find a candidate. Full column list and the three deliberate deviations from the source schema are in [ADR-004](docs/adr/004-phase-0-storage-schema-embedding-contract.md).

---

## Neo4j schema

`bootstrap_neo4j_schema` creates 5 node uniqueness constraints (`Job`, `Resume`, `Skill`, `Company`, `Institution`) and four 768-d cosine vector indexes (`resume_summary_idx`, `job_summary_idx`, `skill_emb_idx`, `chunk_emb_idx`). The `768` is read from `settings.llm_embedding_dim` — one number, one place — so the indexes can never drift from the embedding model.

`ResumeChunk` deliberately has **no** uniqueness constraint on `id`: chunk ids (`c_001`, `c_002`, …) are deterministic per-resume and intentionally collide across resumes. A stale `chunk_id_unique` constraint is actively dropped on bootstrap; re-adding it silently caps every shortlist at one candidate.

---

## Ranking algorithm

Four stages (ported near-verbatim; live from Phase 4):

1. **Coarse recall** — Neo4j `resume_summary_idx` vector query, scoped to the job, 3× oversample → k = 50.
2. **Structured score** — `0.40·skill + 0.25·exp + 0.10·edu + 0.15·seniority + 0.10·vector` over the skill graph, with ontology partial-credit, years/recency weighting, and a must-have-miss penalty.
3. **Evidence** — LLM per-requirement evidence, then anti-fabrication verify (quotes fuzzy-matched ≥ 0.85 against the cited chunk or blanked).
4. **Combine + rank** — `0.6·structured + 0.3·evidence_completeness + 0.1·motivation` → `shortlist_entries` (Postgres) + `SHORTLISTED` edges (Neo4j).

Embeddings **exclude** name/email/phone by construction. 768-d `nomic-embed-text`, cosine — matching the Neo4j indexes.

---

## Status & roadmap

**Phase 0 (seed & infra) is complete.** What is live today: `docker compose up` brings up the stack, Postgres + Neo4j schema come up idempotently on boot, and the API serves `/health`. There are **no ranking or upload routes yet** — those land in later phases.

| Phase | Deliverable | State |
|---|---|---|
| **0 · Seed & infra** | Compose, settings (768-d contract), asyncpg startup DDL, Neo4j bootstrap | **done** |
| 1 · Storage | Filesystem `BlobStore` | pending |
| 2 · Schemas | `resumes`, `matching`, `jobs` pydantic schemas | pending |
| 3 · Ingest + parse | extract/chunk, LLM client+cache, PII encryption on parse | pending |
| 4 · Ranking engine | orchestrator + 4-stage hybrid, reverse-match | pending |
| 5 · Persist + anonymize + export | shortlist service, redaction, csv/json export with `reveal` | pending |
| 6 · API | job/resume/shortlist/reverse-match routes, minimal auth | pending |
| 7 · Evals + viewer | precision@k / evidence-verification fixtures, Flask viewer | pending |

Full plan: [docs/EXTRACTION_PLAN.md](docs/EXTRACTION_PLAN.md). Architecture decisions: [docs/adr/](docs/adr/).

---

## Quick start

```bash
# 0. Prereq on host: Ollama running on metal with the two models pulled
ollama serve &
ollama pull gpt-oss:20b nomic-embed-text

# 1. Configure — PII_KEY is REQUIRED (32 random bytes, base64)
cp .env.example .env
#   set PII_KEY, e.g.:  openssl rand -base64 32

# 2. Bring up the stack
docker compose up -d          # postgres, neo4j, redis, api, worker, frontend
# Postgres tables + Neo4j vector indexes are created on API startup —
# no separate migration step.

# 3. Check the API
curl localhost:8000/health    # -> {"status":"ok"}
```

`PII_KEY` protects every encrypted candidate column. Losing it makes those columns unrecoverable; never commit it.

---

## Gates

`make gates` is the **offline** default — no Docker required, green on a fresh clone:

1. `ruff check` — lint
2. `black --check` — format
3. `mypy --strict` — types (no unjustified `# type: ignore`)
4. `pytest tests/unit` — unit tests (drivers mocked; no live services)
5. Coverage ≥ **80%**
6. Branch name matches `agent/<task-id>-<slug>` or `feat|fix|chore/<slug>`

`make gates-integration` runs the testcontainers suite (real Postgres + Neo4j) and needs a Docker socket. CI runs `make gates-all`.

---

## Repository layout

```
recruiter-assistant/
├── core/
│   ├── src/
│   │   ├── api/         # FastAPI app (Phase 0: /health + lifespan)
│   │   ├── models/      # asyncpg pool + idempotent startup DDL
│   │   ├── worker/      # arq worker + Neo4j bootstrap
│   │   └── settings.py  # single source of truth (pydantic-settings)
│   ├── frontend/        # Flask viewer (Phase 0: stub)
│   └── tests/{unit,integration}/
├── CLAUDE.md            # Claude Code instruction layer (auto-read)
├── .claude/            # build subagents + commands
├── docs/{adr,}          # ADRs + the extraction plan
├── docker-compose.yml   # pg · neo4j · redis · api · worker · frontend
├── Makefile             # gates + stack controls
└── .env.example
```
