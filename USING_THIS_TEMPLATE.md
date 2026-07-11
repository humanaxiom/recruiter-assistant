# Using This Template

> **Golden template** — an offline-first Python agent harness (FastAPI · Neo4j · Postgres · Redis · arq · Flask) driven by **Claude Code** subagents. Frozen in a known-green state. Start every new project from here.

This document is the **source of truth for how the template works today**. It supersedes the historical framing in [README.md](README.md) and [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) wherever they disagree — those describe the original multi-harness design; the sections below record how it has since diverged. When in doubt, trust this file.

---

## 1. What you get

A single Python core (`core/`) plus the Claude Code instruction layer (`CLAUDE.md` + `.claude/`) at the repo root. Claude Code auto-loads both, so the six subagents (planner · tester · coder · reviewer · security · docs) are registered the moment you open the repo.

Verified green out of the box (offline suite, no Docker):

```
ruff · black · mypy --strict · unit tests · coverage 97% (bar: 80%)
```

The stack is **offline-first**: all model calls go to Ollama on the host (`host.docker.internal:11434/v1`). No cloud API is used at runtime by design.

---

## 2. Start a new project from it

1. On GitHub, click **“Use this template” → Create a new repository** (this repo is marked as a template). That gives you a clean copy with no shared history.
2. Clone your new repo and open it in Claude Code.
3. Rename the project surface:
   - `core/pyproject.toml` → `name = "<your-project>-core"`
   - Repo `README.md` title / description
   - `docker-compose.yml` container/db names if you want them project-specific
4. Bring the stack up and confirm the offline gates:
   ```bash
   docker compose up -d          # postgres, neo4j, redis, api, worker, frontend
   make gates                    # offline suite — must be green before you build
   ```
5. Start building. Drive work through the subagent pipeline (Section 5).

> The schema (Postgres tables + Neo4j vector index) is created **automatically on API startup** — there is no migration step to run first.

---

## 3. How this template diverges from the original guide

The original [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) and [README.md](README.md) described a three-harness monorepo (Claude Code / Codex / Copilot) with Alembic migrations and a single combined gate command. This template has moved on:

| Area | Original guide | This template |
|---|---|---|
| Harness | Claude / Codex / Copilot, selected via `make use-*` symlinks | **Claude Code only** — `CLAUDE.md` + `.claude/` live at the repo root, auto-loaded |
| Migrations | `make migrate` → `alembic upgrade head` + Cypher | **No migration framework.** `init_schema` (Postgres) + `GraphMemory.ensure_schema` (Neo4j) run idempotently on startup |
| Gates | one `make gates` incl. integration (needed Docker) | **Split:** `make gates` = offline (no Docker); `make gates-integration` / `make gates-all` = Docker-backed |
| Branch gate | claimed by `make gates`, actually absent | `make gates` runs the branch-name check first |
| Output parsing | regex that truncated Mermaid / nested fences | fence-aware `parsing.py` (`extract_file_blocks`, `extract_json`) |
| Pipeline errors | uncaught exceptions crashed the run | `try/except` + `CycleError` guard → structured escalation |
| Coder | not a `BaseAgent`; artifacts never persisted | extends `BaseAgent`, records artifacts to Neo4j, reviewed against a real `git diff` |
| TDD check | validated list position | validates the `depends_on` dependency graph |
| Coverage | aspirational (<45% real) | genuinely 97% |

If you touch `README.md` / `DEVELOPER_GUIDE.md`, keep them consistent with this file or delete the stale parts — don't let three documents drift apart again.

---

## 4. The gate suite (non-negotiable)

```bash
make gates              # OFFLINE default — branch-name · ruff · black · mypy --strict · unit · coverage ≥ 80
make gates-integration  # integration tests — requires a running Docker socket (testcontainers)
make gates-all          # everything CI runs
make gates-fast         # pre-commit subset (no coverage, no integration)
```

Rules that do not bend:
- A single red gate = the work is not done. Iterate on that exact failure; never weaken a test or lower the bar to force green.
- Coverage floor is `COVERAGE_THRESHOLD` (settings/env, default 80); it is also passed to `--cov-fail-under` in the Makefile and CI. Keep them in sync.
- Branch naming: `(agent|feat|fix|chore)/<slug>`; never commit to `main`.

Enforced in three places: the pre-commit hook (`gates-fast` + branch-name), the ReviewLoop (feeds failures back to the coder, ≤5 iterations then escalates), and CI (`.github/workflows/ci.yml`, runs `gates-all`).

---

## 5. How Claude Code drives development

Open the repo and prompt, for a non-trivial task:

```
Use the planner subagent to plan this task, then execute the full pipeline through docs.
```

Flow: **planner → tester → coder (inside the ReviewLoop) → reviewer + security → docs.** The pipeline halts if the reviewer rejects or security fails. Each subagent is defined in `.claude/agents/*.md`; the Python implementations live in `core/src/agents/`.

Slash commands (`.claude/commands/`): `/gates`, `/review-loop <task>`, `/memory-query <text>`.

`.claude/settings.json` blocks commits to `main` (PreToolUse hook) and auto-runs `ruff --fix` after every write. These hooks shell out to `bash`, so on Windows you need Git Bash on PATH.

---

## 6. Prerequisites & verifying locally

- **Docker + Compose** — the stack, and the integration gate (testcontainers).
- **Ollama on the host** — `ollama serve`, then `ollama pull qwen2.5-coder:14b nomic-embed-text`.
- **Python 3.11 + `make`** — only if you want to run `make gates` natively. If you don't have a local interpreter, run the offline gates in a throwaway container:
  ```bash
  docker run --rm -v "$PWD:/w" -w /w/core python:3.11-slim bash -lc \
    "pip install -q -r requirements.txt -r requirements-dev.txt && \
     ruff check src tests && black --check src tests && mypy src --strict && \
     pytest tests/unit --cov=src --cov-fail-under=80 -q"
  ```

---

## 7. Deliberately deferred

These are conscious omissions, not oversights — revisit them when the real project needs them:

- **No migration framework.** Tables/index are created on startup. Add Alembic once you have a schema-change history worth versioning (see [ADR-002](docs/adr/002-neo4j-memory-postgres-ledger.md)).
- **The integration gate needs Docker** and is not part of `make gates`; it runs in CI and via `make gates-integration`.
- **No end-to-end LLM run is baked in.** The pipeline is unit-tested with the model client mocked; exercising it against a live Ollama is a first-task activity, not a template guarantee.
- **The Flask dashboard is minimal** — it enqueues tasks and shows lineage; there is no `/tasks` list endpoint yet.

---

## 8. Repository layout

```
agent-harness-template/
├── core/                       # THE application
│   ├── src/
│   │   ├── api/                # FastAPI app + routes (schema bootstrap on startup)
│   │   ├── agents/             # BaseAgent, planner, tester, coder, reviewer, security, docs, parsing
│   │   ├── memory/             # Neo4j graph memory + vector retrieval + ensure_schema
│   │   ├── models/             # SQLAlchemy models + init_schema
│   │   ├── worker/             # arq task queue workers
│   │   └── gates/              # gate runner + review loop
│   ├── frontend/               # Flask dashboard (config via src/settings.py)
│   └── tests/{unit,integration}/
├── CLAUDE.md                   # Claude Code instruction layer (auto-read)
├── .claude/                    # agents/ · commands/ · settings.json
├── docs/adr/                   # architecture decision records
├── .github/workflows/ci.yml    # runs gates-all
├── docker-compose.yml · Makefile · .pre-commit-config.yaml
└── USING_THIS_TEMPLATE.md      # this file — the current source of truth
```
