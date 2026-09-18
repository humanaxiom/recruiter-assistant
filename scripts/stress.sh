#!/usr/bin/env bash
# Load/concurrency run against the ISOLATED `recruiter-stress` compose
# project (never the developer's normal dev stack). Assumes the stack is
# already up (run `scripts/e2e.sh` first, or bring it up by hand with
# `docker compose -f docker-compose.yml -f docker-compose.stress.yml
# -p recruiter-stress up -d`).
#
# Usage:
#   USERS=5 RESUMES_PER_USER=3 scripts/stress.sh          # stub LLM (default)
#   STRESS_LLM=real STRESS_CONFIRM_REAL=1 USERS=3 RESUMES_PER_USER=5 \
#     scripts/stress.sh                                    # real tailnet LLM
#   WORKERS=3 scripts/stress.sh                             # scale worker (stub only)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="recruiter-stress"

USERS="${USERS:-1}"
RESUMES_PER_USER="${RESUMES_PER_USER:-3}"
STRESS_LLM="${STRESS_LLM:-stub}"

if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  export MSYS_NO_PATHCONV=1
  export MSYS2_ARG_CONV_EXCL='*'
fi

if [[ "$STRESS_LLM" == "real" ]]; then
  if [[ "${STRESS_CONFIRM_REAL:-0}" != "1" ]]; then
    echo "🔴 stress: STRESS_LLM=real needs STRESS_CONFIRM_REAL=1 — this hits the" >&2
    echo "   shared tailnet GPU peer for real, not the offline stub." >&2
    exit 1
  fi
  total=$(( USERS * RESUMES_PER_USER ))
  if (( total > 30 )); then
    echo "🔴 stress: USERS*RESUMES_PER_USER=$total exceeds the real-LLM cap of 30" >&2
    exit 1
  fi
fi

if [[ -n "${WORKERS:-}" && "$STRESS_LLM" != "stub" ]]; then
  echo "🔴 stress: WORKERS=N (worker scaling) is only supported in stub mode" >&2
  exit 1
fi

COMPOSE=(docker compose -f "$REPO_ROOT/docker-compose.yml" -f "$REPO_ROOT/docker-compose.stress.yml" -p "$PROJECT")

if [[ -n "${WORKERS:-}" ]]; then
  echo "▶ stress: scaling worker to $WORKERS replicas (stub mode)"
  "${COMPOSE[@]}" --profile stub up -d --scale "worker=${WORKERS}"
fi

echo "▶ stress: USERS=$USERS RESUMES_PER_USER=$RESUMES_PER_USER STRESS_LLM=$STRESS_LLM"

FIXTURES_DIR="${FIXTURES_DIR:-$REPO_ROOT/fixtures}"
FIXTURES_MOUNT=()
if [[ ! -d "$REPO_ROOT/fixtures" ]]; then
  FIXTURES_MOUNT=(-v "${FIXTURES_DIR}:/repo/fixtures")
fi

status=0
docker run --rm \
  --network "${PROJECT}_default" \
  -v "$REPO_ROOT:/repo" \
  "${FIXTURES_MOUNT[@]}" \
  -w /repo/core \
  -e USERS="$USERS" \
  -e RESUMES_PER_USER="$RESUMES_PER_USER" \
  -e FRONTEND=http://frontend:5000 \
  -e PYTHONPATH=/repo/core \
  "${PROJECT}-api" \
  python -m tests.e2e.stress || status=$?

cat "$REPO_ROOT/report/report.md" 2>/dev/null || true
exit "$status"
