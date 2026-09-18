#!/usr/bin/env bash
# Bring up an ISOLATED end-to-end / stress stack and drive it: the smoke
# suite, then a small functional-load run (tests/e2e/stress.py), then
# doctor.sh — all against the `recruiter-stress` compose project (28xxx
# ports, CAS off), never the developer's normal dev stack.
#
# Usage:
#   scripts/e2e.sh                 # real GPU-peer LLM (LLM_BASE_URL default)
#   E2E_LLM=stub scripts/e2e.sh    # offline stub model (docker-compose.stress.yml
#                                  # `llmstub` service, profile "stub")
#
# Generates a stress-only .env on first run (fresh secrets, 28xxx ports, CAS
# off) if one is not already present. Refuses to start if any of its ports
# are already bound, AND refuses to start if the checkout's .env is not
# actually the isolated 28xxx one (e.g. a pilot/dev .env reused by mistake) —
# this must never collide with, or point at, a developer's or pilot's normal
# stack.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Always the isolated project — `docker compose -p recruiter-stress ...` —
# never the default one, so this can never reset a developer's dev stack.
PROJECT="recruiter-stress"
ENV_FILE="$REPO_ROOT/.env"

E2E_LLM="${E2E_LLM:-real}"
PROFILE_ARGS=()
if [[ "$E2E_LLM" == "stub" ]]; then
  PROFILE_ARGS=(--profile stub)
fi

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

COMPOSE=(docker compose -f "$WIN_ROOT/docker-compose.yml" -f "$WIN_ROOT/docker-compose.stress.yml" -p "$PROJECT")

_port_free() {
  ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "▶ e2e: generating a fresh $ENV_FILE for the stress checkout"
  cat > "$ENV_FILE" <<EOF
POSTGRES_DSN=postgresql://app:app@postgres:5432/recruiter
POSTGRES_POOL_MIN=2
POSTGRES_POOL_MAX=10
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=recruiterpass
REDIS_URL=redis://redis:6379/0
API_PORT=28800
FRONTEND_PORT=28500
POSTGRES_PORT=28432
REDIS_PORT=28379
NEO4J_HTTP_PORT=28474
NEO4J_BOLT_PORT=28687
LLM_BASE_URL=${LLM_BASE_URL:-http://100.88.247.106:11434/v1}
LLM_TIMEOUT_S=900
LLM_MODEL_GENERATION=gpt-oss:20b
LLM_MODEL_EMBEDDING=nomic-embed-text
LLM_EMBEDDING_DIM=768
STORAGE_DIR=/data
PII_KEY=$(openssl rand -base64 32)
SKILL_HASH_SALT=$(openssl rand -base64 32)
BLIND_REVIEW_DEFAULT=false
API_KEY_ADMIN=$(openssl rand -base64 32)
API_KEY_RECRUITER=$(openssl rand -base64 32)
API_KEY_HIRING_MANAGER=$(openssl rand -base64 32)
API_KEY_AUDITOR=$(openssl rand -base64 32)
COVERAGE_THRESHOLD=80
API_BASE_URL=http://api:8000
FLASK_SECRET_KEY=$(openssl rand -base64 32)
CAS_ENABLED=false
CAS_SERVER_URL=https://cas.sfu.ca/cas
CAS_SERVICE_BASE_URL=http://localhost:28800
CAS_FRONTEND_BASE_URL=http://localhost:28500
DEFAULT_ADMIN_CAS_USERNAME=stress
TRUST_PROXY_HEADERS=false
PROXY_HOPS=1
TALEO_ENABLED=false
TALEO_BASE_URL=https://tre.tbe.taleo.net
TALEO_ORG=SIMOFRAS
TALEO_CWS=37
TALEO_REQUEST_DELAY_S=1.0
TALEO_TIMEOUT_S=20.0
TALEO_MAX_PAGES=20
EOF
fi

_env_var() {
  # Read KEY= from the checkout's .env, falling back to $2 if absent/unset.
  local key="$1" default="$2"
  if [[ -f "$ENV_FILE" ]]; then
    local val
    val="$(grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2-)"
    if [[ -n "$val" ]]; then
      printf '%s' "$val"
      return
    fi
  fi
  printf '%s' "$default"
}

# HARD-FAIL unless every port this checkout's .env actually configures is in
# the isolated 28xxx block. Never inferred/hardcoded — read back from
# $ENV_FILE, because a pilot/dev .env reused by mistake here would otherwise
# bring this stack up on the LIVE ports (security finding, 2026-09-17).
for var in API_PORT FRONTEND_PORT POSTGRES_PORT REDIS_PORT NEO4J_HTTP_PORT NEO4J_BOLT_PORT; do
  val="$(_env_var "$var" "")"
  if [[ ! "$val" =~ ^28[0-9]{3}$ ]]; then
    echo "🔴 e2e: $ENV_FILE's $var=$val is not in the isolated 28000-28999 block." >&2
    echo "   Refusing to run — this must never be the pilot/dev .env. Delete" >&2
    echo "   $ENV_FILE and re-run to generate a fresh isolated one." >&2
    exit 1
  fi
done

API_PORT="$(_env_var API_PORT 28800)"
FRONTEND_PORT="$(_env_var FRONTEND_PORT 28500)"
POSTGRES_PORT="$(_env_var POSTGRES_PORT 28432)"
REDIS_PORT="$(_env_var REDIS_PORT 28379)"
NEO4J_HTTP_PORT="$(_env_var NEO4J_HTTP_PORT 28474)"
NEO4J_BOLT_PORT="$(_env_var NEO4J_BOLT_PORT 28687)"

for port in "$API_PORT" "$FRONTEND_PORT" "$POSTGRES_PORT" "$REDIS_PORT" "$NEO4J_HTTP_PORT" "$NEO4J_BOLT_PORT" 28900; do
  if ! _port_free "$port"; then
    echo "🔴 e2e: port $port is already in use — stop whatever owns it first" >&2
    exit 1
  fi
done

# Stub mode overrides LLM_BASE_URL as PROCESS ENV on the compose invocation
# only (shell env wins over .env for compose variable interpolation) — NEVER
# by editing .env in place. Editing .env was the earlier defect here: run
# against the pilot checkout, stub mode would point the LIVE stack at a
# nonexistent llmstub host with no way back short of hand-editing the file.
echo "▶ e2e: docker compose -p $PROJECT up -d --build (LLM mode: $E2E_LLM)"
if [[ "$E2E_LLM" == "stub" ]]; then
  LLM_BASE_URL="http://llmstub:8000/v1" "${COMPOSE[@]}" "${PROFILE_ARGS[@]}" up -d --build
else
  "${COMPOSE[@]}" "${PROFILE_ARGS[@]}" up -d --build
fi

echo "▶ e2e: waiting for /health on the API port"
deadline=$((SECONDS + 180))
until curl -fsS "http://127.0.0.1:${API_PORT}/health" >/dev/null 2>&1; do
  if (( SECONDS > deadline )); then
    echo "🔴 e2e: API never became healthy" >&2
    exit 1
  fi
  sleep 3
done

if [[ "$E2E_LLM" == "stub" ]]; then
  echo "▶ e2e: waiting for llmstub /health"
  deadline=$((SECONDS + 180))
  until curl -fsS "http://127.0.0.1:28900/health" >/dev/null 2>&1; do
    if (( SECONDS > deadline )); then
      echo "🔴 e2e: llmstub never became healthy" >&2
      exit 1
    fi
    sleep 3
  done
fi

# Evidence, not a claim: print exactly what the api container itself sees.
echo -n "▶ e2e: api container's LLM_BASE_URL = "
"${COMPOSE[@]}" exec -T api sh -c 'echo $LLM_BASE_URL'

echo "▶ e2e: running the smoke suite against the stress stack"
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
SMOKE_NETWORK="${SMOKE_NETWORK:-${PROJECT}_default}" \
SMOKE_IMAGE="${SMOKE_IMAGE:-${PROJECT}-api}" \
  "$REPO_ROOT/scripts/smoke.sh"

echo "▶ e2e: running a small functional-load pass (tests/e2e/stress.py)"
status=0
docker run --rm \
  --network "${PROJECT}_default" \
  -v "${WIN_ROOT}:/repo" \
  "${FIXTURES_MOUNT[@]}" \
  -w /repo/core \
  -e USERS=1 \
  -e RESUMES_PER_USER=3 \
  -e FRONTEND=http://frontend:5000 \
  -e PYTHONPATH=/repo/core \
  "${PROJECT}-api" \
  python -m tests.e2e.stress || status=$?

echo "▶ e2e: report"
cat "$REPO_ROOT/report/report.md" 2>/dev/null || echo "(no report.md written)"

if [[ "$status" -ne 0 ]]; then
  echo "🔴 e2e: functional-load run FAILED (exit $status) — see report.md above" >&2
  exit "$status"
fi

echo "▶ e2e: running doctor.sh against the stress stack"
COMPOSE_PROJECT_NAME="$PROJECT" \
COMPOSE_FILE="docker-compose.yml:docker-compose.stress.yml" \
  "$REPO_ROOT/scripts/doctor.sh"
