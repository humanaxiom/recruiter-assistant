# Agent Harness v2 — developer interface
.PHONY: up down gates gates-fast migrate logs

up:               ## Start the full stack (Ollama must be running on host)
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

# ── Gates: THE non-negotiable suite ────────────────────────────────────────
gates:            ## Full gate suite (what agents and CI run)
	cd core && ruff check src tests
	cd core && black --check src tests
	cd core && mypy src --strict
	cd core && pytest tests/unit --cov=src --cov-fail-under=80 --timeout=120 -q
	cd core && pytest tests/integration --timeout=300 -q
	@echo "✅ ALL GATES GREEN"

gates-fast:       ## Pre-commit subset (no integration)
	cd core && ruff check src tests && black --check src tests
	cd core && mypy src --strict
	cd core && pytest tests/unit -q --timeout=120

# ── Migrations ─────────────────────────────────────────────────────────────
migrate:          ## Postgres (alembic) + Neo4j (cypher)
	docker compose exec api alembic upgrade head
	docker compose exec neo4j cypher-shell -u neo4j -p harnesspass \
		-f /migrations/001_init.cypher || \
		cat core/db/migrations/001_init.cypher | \
		docker compose exec -T neo4j cypher-shell -u neo4j -p harnesspass
