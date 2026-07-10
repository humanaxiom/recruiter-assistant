# CLAUDE.md — Agent Harness v2 (Offline-First / Python / FastAPI / Neo4j / Postgres)

Read automatically by Claude Code every session. This governs ALL work in this repo.

---

## Stack (do not deviate)

- **Python 3.11+**, FastAPI (API), Flask (frontend), arq + Redis (async queue)
- **Postgres** = transactional data (SQLAlchemy async + Alembic)
- **Neo4j** = agent graph memory + vector indexes (768-dim, cosine, `nomic-embed-text`)
- **Ollama on host metal** at `host.docker.internal:11434/v1` — NEVER add cloud API calls
- Everything except Ollama runs in Docker (`docker compose up -d`)

## Non-negotiable gates — run before EVERY commit

```bash
make gates        # full suite
make gates-fast   # pre-commit subset
```

Gates: ruff · black · mypy --strict · pytest unit · pytest integration (testcontainers) · coverage ≥ 80% · branch-name. **A single red gate = the work is not done. Iterate until all green — do not report success, do not open a PR, do not stop.**

## Git workflow — mandatory

1. NEVER commit to `main`
2. Branch: `git checkout -b agent/<task-id>-<slug>` (or `feat|fix|chore/<slug>`)
3. Commit sequence tells the TDD story: `red: failing tests` → `green: implementation` → `refactor/docs`
4. Open PR only when `make gates` is fully green locally

## TDD order — mandatory

1. Write failing tests FIRST (`tests/unit/`, `tests/integration/`)
2. Run tests, confirm RED
3. Implement minimally until GREEN
4. Refactor with gates green
5. Update `docs/adr/` if architecture changed; update Mermaid diagrams in README/docs

## Review-iterate loop

When gates fail, read the failure output, fix ONLY what failed, re-run `make gates`. Max 5 self-iterations; if still red, STOP and present the failure report to the human with your analysis — never silently weaken a test or lower the coverage bar to get green.

## Code rules

- Full type hints; `mypy --strict` clean; no unjustified `# type: ignore`
- Async everywhere (SQLAlchemy async, neo4j async driver, httpx)
- Config only via `src/settings.py` (pydantic-settings) — never `os.environ` scattered in code
- Postgres for anything transactional/relational; Neo4j only for graph relationships and vector retrieval; Redis only as arq broker
- All model calls go through the OpenAI-compatible client with `base_url=settings.ollama_base_url`
- Never modify test files to make implementation pass (only if a test is provably wrong, and say so)

## Slash commands (`.claude/commands/`)

| Command | Purpose |
|---|---|
| `/gates` | Run full gate suite, report results table |
| `/review-loop <task>` | Run iterate-until-green cycle on current branch |
| `/memory-query <text>` | Vector-search Neo4j for similar prior artifacts before implementing |

## Before implementing anything new

1. `curl localhost:8000/memory/similar?q=<task>` — check if similar work exists in graph memory
2. Read the relevant ADRs in `docs/adr/`
3. Check `docker compose ps` — stack must be healthy for integration tests
