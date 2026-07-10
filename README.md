# 🧰 AI Agent Harness v2 — Offline-First

> Python core (FastAPI + Neo4j + Postgres + arq + Flask) driven by AI subagents through **Claude Code**. Inference runs on **Ollama on bare metal**; everything else runs in Docker.

---

## Why this stack

One Python core, one instruction layer. Claude Code reads `CLAUDE.md` and the subagents in `.claude/agents/` at the repo root, pointing at the same code, gates, and CI. Inference stays local on Ollama — no cloud API calls at runtime.

---

## System Architecture

```mermaid
graph TB
    subgraph Metal["🔩 Bare Metal (Host)"]
        OL[Ollama<br/>:11434]
    end

    subgraph Docker["🐳 Docker Compose"]
        subgraph AppTier["App Tier"]
            API[FastAPI<br/>:8000]
            FE[Flask Frontend<br/>:5000]
            WK[arq Worker<br/>async tasks]
        end
        subgraph DataTier["Data Tier"]
            PG[(PostgreSQL<br/>transactions)]
            NEO[(Neo4j<br/>graph + vector)]
            RD[(Redis<br/>arq queue)]
        end
    end

    FE -->|REST| API
    API --> PG
    API --> NEO
    API -->|enqueue| RD
    WK -->|dequeue| RD
    WK --> PG
    WK --> NEO
    API & WK -->|host.docker.internal:11434| OL

    style Metal fill:#2D3436,color:#fff
    style AppTier fill:#1F6FEB,color:#fff
    style DataTier fill:#F59F00,color:#fff
```

**Data responsibilities**

| Store | Role |
|---|---|
| **PostgreSQL** | Transactional data: task records, run ledger, audit log, users |
| **Neo4j** | Agent memory graph + vector indexes (embeddings via Ollama `nomic-embed-text`) |
| **Redis** | arq task queue + result backend |

---

## Agent Memory Graph (Neo4j)

```mermaid
graph LR
    T[Task] -->|DECOMPOSED_INTO| S[Subtask]
    S -->|EXECUTED_BY| A[Agent]
    S -->|PRODUCED| AR[Artifact]
    AR -->|EMBEDDED_AS| V[Vector Index<br/>artifact_embeddings]
    R[Run] -->|OF_TASK| T
    R -->|GATE_RESULT| G[GateResult]
    A -->|LEARNED| N[Note]
```

Vector index `artifact_embeddings` (768-dim, cosine) enables semantic retrieval of prior agent outputs — agents query "have we solved something like this before?" before implementing.

---

## Subagent Roster

The pipeline is **planner → tester → coder(loop) → reviewer + security → docs**, backed by the shared Python core:

| Subagent | Python class (`core/src/agents/`) | Claude Code (`.claude/agents/`) |
|---|---|---|
| Planner | `PlannerAgent` — JSON plan, TDD-order validated | `planner.md` |
| Tester | `TesterAgent` — failing tests only, tests/ allowlist | `tester.md` |
| Coder | `CoderAgent` in `ReviewLoop` — iterate ≤5 then escalate | `coder.md` |
| Reviewer | `ReviewerAgent` — severity findings, merge-blocking | `reviewer.md` |
| Security | `SecurityAgent` — injection/secrets/traversal/egress audit | `security.md` |
| Docs | `DocsAgent` — ADR + Mermaid, docs/ allowlist | `docs.md` |

The **Orchestrator** (`core/src/agents/orchestrator.py`) resolves subtask dependencies topologically, runs the coder inside the iterate-until-green loop, and hard-blocks the pipeline on reviewer rejection or security failure. Runs execute async via arq (`run_pipeline` job) and land lineage in Neo4j.

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant P as Planner
    participant T as Tester
    participant C as Coder+ReviewLoop
    participant R as Reviewer
    participant S as Security
    participant D as Docs

    O->>P: task spec
    P-->>O: validated plan (tester<coder enforced)
    O->>T: write failing tests
    T-->>O: RED confirmed
    O->>C: implement
    loop until gates green (max 5)
        C->>C: fix exact failures
    end
    C-->>O: GREEN
    par merge-blocking checks
        O->>R: review diff
        O->>S: security audit
    end
    R-->>O: APPROVED
    S-->>O: PASS
    O->>D: ADR + diagrams + README
    D-->>O: done → PR
```

---

## Review-Iterate Loop (non-negotiable gates)

```mermaid
stateDiagram-v2
    [*] --> Branch : git checkout -b agent/&lt;task-id&gt;-&lt;slug&gt;
    Branch --> Tests : TestAgent writes failing tests
    Tests --> Red : gates run — must be RED
    Red --> Implement : CoderAgent
    Implement --> Gates : run all gates
    Gates --> Review : ALL GREEN
    Gates --> Implement : any RED (max 5 iterations)
    Implement --> Escalate : 5 failures → human
    Review --> Docs : ReviewAgent approves
    Review --> Implement : review findings
    Docs --> PR : open PR to main
    PR --> [*] : CI green + human merge
```

**Gates (all must pass, enforced locally by `make gates` and in CI):**

1. `ruff check` — lint
2. `black --check` — format
3. `mypy --strict` — types (no unjustified `# type: ignore`)
4. `pytest tests/unit` — unit tests
5. `pytest tests/integration` — integration (real Postgres + Neo4j + Redis via testcontainers)
6. Coverage ≥ **80%**
7. Branch name matches `agent/<task-id>-<slug>` or `feat|fix|chore/<slug>`

---

## Git Branch Workflow

```mermaid
gitGraph
    commit id: "main"
    branch agent/T42-rate-limiter
    commit id: "red: failing tests"
    commit id: "green: implementation"
    commit id: "refactor + docs"
    checkout main
    merge agent/T42-rate-limiter tag: "CI green"
```

- Agents only ever commit to `agent/*` branches
- `main` is protected: PR + green CI + human approval required
- Pre-commit hooks run ruff/black/mypy/fast-tests before any commit lands

---

## Repository Layout

```
agent-harness-v2/
├── core/                          # THE shared application
│   ├── src/
│   │   ├── api/                   # FastAPI app + routes
│   │   ├── agents/                # BaseAgent, Planner, Coder, Tester, Reviewer
│   │   ├── memory/                # Neo4j graph memory + vector retrieval
│   │   ├── models/                # SQLAlchemy (Postgres) + Pydantic schemas
│   │   ├── worker/                # arq task queue workers
│   │   └── gates/                 # Gate runner + review loop
│   ├── frontend/                  # Flask app (dashboard for runs/gates)
│   ├── db/migrations/             # Alembic (Postgres) + Cypher (Neo4j)
│   ├── tests/{unit,integration,e2e}/
│   └── scripts/
├── CLAUDE.md                      # Claude Code instruction layer (auto-read)
├── .claude/                       # agents/ + commands/ + settings.json
├── docs/{adr,diagrams}/
├── .github/workflows/ci.yml
├── docker-compose.yml
├── Makefile
└── .pre-commit-config.yaml
```

---

## Quick Start

```bash
# 0. Prereq on host: Ollama running on metal
ollama serve &
ollama pull qwen2.5-coder:14b nomic-embed-text

# 1. Bring up the stack
docker compose up -d          # postgres, neo4j, redis, api, worker, frontend

# 2. Run migrations
make migrate                  # alembic upgrade head + cypher migrations

# 3. Run the full gate suite (what agents run every iteration)
make gates

# 4. Open the dashboard
open http://localhost:5000

# 5. Drive development with Claude Code
claude                        # reads CLAUDE.md + .claude/ automatically
```

Claude Code auto-loads `CLAUDE.md` and the six subagents in `.claude/agents/` from the repo root — no extra wiring. Confirm with `/agents`.

---

## Ollama Connectivity from Docker

Containers reach the host's Ollama via `host.docker.internal:11434` (mapped in compose with `extra_hosts` for Linux). All agent code takes `OLLAMA_BASE_URL` — no cloud API is required at runtime. The OpenAI-compatible endpoint (`/v1`) is used so agent code is portable if you later swap to vLLM.

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434/v1` | Local inference |
| `AGENT_MODEL` | `qwen2.5-coder:14b` | Coding model |
| `EMBED_MODEL` | `nomic-embed-text` | Embeddings for Neo4j vectors |
| `DATABASE_URL` | `postgresql+asyncpg://app:app@postgres:5432/harness` | Postgres |
| `NEO4J_URI` | `bolt://neo4j:7687` | Neo4j |
| `REDIS_URL` | `redis://redis:6379/0` | arq queue |
| `MAX_REVIEW_ITERATIONS` | `5` | Review-loop cap before escalation |
| `COVERAGE_THRESHOLD` | `80` | CI/gate coverage minimum |
