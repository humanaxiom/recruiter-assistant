#!/usr/bin/env bash
# Load/concurrency run against the ISOLATED `recruiter-stress` compose
# project (never the developer's normal dev stack). Brings the stack up (or
# reconciles it) itself, in the requested LLM mode, rather than assuming it
# is already up in the right mode — `docker compose up -d` is idempotent, so
# running this against an already-up stack in the SAME mode is a no-op.
#
# Usage:
#   USERS=5 RESUMES_PER_USER=3 scripts/stress.sh          # stub LLM (default)
#   STRESS_LLM=real STRESS_CONFIRM_REAL=1 USERS=3 RESUMES_PER_USER=5 \
#     scripts/stress.sh                                    # real tailnet LLM
#   WORKERS=3 scripts/stress.sh                             # scale worker (stub only)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Always the isolated project — `docker compose -p recruiter-stress ...` —
# never the default one, so this can never reset a developer's dev stack.
PROJECT="recruiter-stress"

USERS="${USERS:-1}"
RESUMES_PER_USER="${RESUMES_PER_USER:-3}"
STRESS_LLM="${STRESS_LLM:-stub}"

# Windows/Git Bash: `$REPO_ROOT` (from plain `pwd`) is an MSYS path
# (`/tmp/...` when the checkout lives under the Windows TEMP dir, `/c/...`
# elsewhere) that Docker Desktop — and `docker compose -f <path>` itself —
# resolve wrongly: the runner container sees an empty/wrong directory and
# `python -m tests.e2e.stress` fails with `No module named 'tests'`. Same fix
# as scripts/verify.sh: disable MSYS path conversion and use the
# Windows-native path (`pwd -W`) for EVERY path handed to `docker`/`docker
# compose` — both `-f <compose file>` and `-v <mount source>` — never the
# plain `pwd`/`$REPO_ROOT` one. `$REPO_ROOT` itself stays MSYS-native, since
# bash's own file tests (`-f`, `cat`, `sed`, heredocs) resolve it correctly
# through the MSYS runtime; only arguments to WINDOWS-NATIVE executables need
# the `-W` form.
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  export MSYS_NO_PATHCONV=1
  export MSYS2_ARG_CONV_EXCL='*'
  WIN_ROOT="$(cd "$REPO_ROOT" && pwd -W 2>/dev/null || echo "$REPO_ROOT")"
else
  WIN_ROOT="$REPO_ROOT"
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

COMPOSE=(docker compose -f "$WIN_ROOT/docker-compose.yml" -f "$WIN_ROOT/docker-compose.stress.yml" -p "$PROJECT")

_env_var() {
  # Read KEY= from the checkout's .env, falling back to $2 if absent/unset.
  local key="$1" default="$2" file="$REPO_ROOT/.env"
  if [[ -f "$file" ]]; then
    local val
    val="$(grep -E "^${key}=" "$file" | tail -1 | cut -d= -f2-)"
    if [[ -n "$val" ]]; then
      printf '%s' "$val"
      return
    fi
  fi
  printf '%s' "$default"
}

API_PORT="$(_env_var API_PORT 28800)"

_wait_health() {
  local label="$1" url="$2" deadline=$((SECONDS + 180))
  echo "▶ stress: waiting for $label ($url)"
  until curl -fsS "$url" >/dev/null 2>&1; do
    if (( SECONDS > deadline )); then
      echo "🔴 stress: $label never became healthy at $url" >&2
      exit 1
    fi
    sleep 3
  done
}

if [[ "$STRESS_LLM" == "stub" ]]; then
  echo "▶ stress: bringing up the stress stack with the OFFLINE stub LLM"
  scale_args=()
  if [[ -n "${WORKERS:-}" ]]; then
    scale_args=(--scale "worker=${WORKERS}")
  fi
  LLM_BASE_URL="http://llmstub:8000/v1" "${COMPOSE[@]}" --profile stub up -d "${scale_args[@]}"
  _wait_health "api /health" "http://127.0.0.1:${API_PORT}/health"
  _wait_health "llmstub /health" "http://127.0.0.1:28900/health"
else
  echo "▶ stress: bringing up the stress stack with the REAL tailnet LLM (.env value)"
  "${COMPOSE[@]}" up -d
  _wait_health "api /health" "http://127.0.0.1:${API_PORT}/health"
fi

# Evidence, not a claim: print exactly what the api container itself sees.
echo -n "▶ stress: api container's LLM_BASE_URL = "
"${COMPOSE[@]}" exec -T api sh -c 'echo $LLM_BASE_URL'

echo "▶ stress: USERS=$USERS RESUMES_PER_USER=$RESUMES_PER_USER STRESS_LLM=$STRESS_LLM"

FIXTURES_DIR="${FIXTURES_DIR:-$REPO_ROOT/fixtures}"
FIXTURES_MOUNT=()
if [[ ! -d "$REPO_ROOT/fixtures" ]]; then
  if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
    FIXTURES_MOUNT_SRC="$(cd "$FIXTURES_DIR" && pwd -W 2>/dev/null || echo "$FIXTURES_DIR")"
  else
    FIXTURES_MOUNT_SRC="$FIXTURES_DIR"
  fi
  FIXTURES_MOUNT=(-v "${FIXTURES_MOUNT_SRC}:/repo/fixtures")
fi

status=0
docker run --rm \
  --network "${PROJECT}_default" \
  -v "${WIN_ROOT}:/repo" \
  "${FIXTURES_MOUNT[@]}" \
  -w /repo/core \
  -e USERS="$USERS" \
  -e RESUMES_PER_USER="$RESUMES_PER_USER" \
  -e FRONTEND=http://frontend:5000 \
  -e PYTHONPATH=/repo/core \
  "${PROJECT}-api" \
  python -m tests.e2e.stress || status=$?

cat "$REPO_ROOT/report/report.md" 2>/dev/null || true

if [[ "$status" -ne 0 ]]; then
  echo "🔴 stress: run FAILED (exit $status) — see report.md above" >&2
fi
exit "$status"
