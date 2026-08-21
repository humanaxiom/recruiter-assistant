#!/usr/bin/env bash
# Ask the RUNNING deployment whether its invariants actually hold.
#
# `scripts/verify.sh` proves the CODE is correct. This proves the DATA is —
# and those are different claims. The defect that forced this module (ROADMAP
# A7 (20)) was a fix that shipped correct, passed every gate, and had never
# applied to a single row thirteen days later, because the write only fires on
# new data and every row predated it. No test can see that: a fixture is always
# freshly built and always well-formed.
#
# Runs INSIDE the api container, which already has the code, the dependencies
# and network reach to Postgres and Neo4j — the same "never hand-write the
# command" discipline as verify.sh.
#
# Exit code: 1 if any check FAILED (including a datastore it could not reach —
# that is never a clean bill of health), else 0.
set -euo pipefail

SERVICE="${DOCTOR_SERVICE:-api}"
CONTAINER="$(docker compose ps -q "$SERVICE" 2>/dev/null || true)"

if [[ -z "$CONTAINER" ]]; then
  echo "🔴 doctor: the '$SERVICE' container is not running — start the stack first" >&2
  echo "   docker compose up -d" >&2
  exit 1
fi

echo "▶ doctor: checking the live deployment via the '$SERVICE' container"
echo
docker exec "$CONTAINER" python -m src.doctor
