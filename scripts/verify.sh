#!/usr/bin/env bash
# The ONE way to verify work in this repo. Agents and humans both run this.
#
# Why this exists: there is no usable Python on the dev host (only the
# WindowsApps stub), so `make gates` cannot run natively. Every agent that
# needed to verify something therefore hand-wrote its own `docker run ...`
# command — and every hand-written variant was NARROWER than the real gate.
# Two recorded instances: one ran `mypy src` instead of `mypy src frontend`
# and let a frontend type error reach a PR; another ran `black --check
# frontend` only and left test files unformatted. A third (FU-5 slice 1) ran
# the unit suite only for a schema change the unit suite structurally cannot
# check, and shipped a missing column DEFAULT.
#
# The fix is not "remember to run the wider command". It is to make the
# narrow command impossible: this script runs the REAL Makefile targets
# inside the container, so the Makefile stays the single source of truth and
# the gate cannot drift from what CI runs.
#
# Usage:
#   scripts/verify.sh              # offline gates (ruff, black, mypy, unit, coverage)
#   scripts/verify.sh integration  # integration suite vs real Postgres/Neo4j/Redis
#   scripts/verify.sh all          # both — what CI runs
#
# Exit code is the gate's exit code. Non-zero means NOT DONE.
set -euo pipefail

MODE="${1:-offline}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$MODE" in
  offline)     TARGET="gates" ;;
  integration) TARGET="gates-integration" ;;
  all)         TARGET="gates-all" ;;
  *)
    echo "usage: scripts/verify.sh [offline|integration|all]" >&2
    exit 2
    ;;
esac

# Windows host: the repo lives at a drive path Docker needs verbatim, and Git
# Bash rewrites /repo -> C:/repo unless path conversion is disabled.
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  export MSYS_NO_PATHCONV=1
  export MSYS2_ARG_CONV_EXCL='*'
  MOUNT_SRC="$(cd "$REPO_ROOT" && pwd -W 2>/dev/null || echo "$REPO_ROOT")"
else
  MOUNT_SRC="$REPO_ROOT"
fi

# Mount the REPO ROOT, not core/. Several meta-tests resolve repo-root files
# via parents[3] (the root Makefile, .github/workflows/ci.yml, .claude/agents/*);
# a core-only mount resolves those to / and fails them spuriously.
DOCKER_ARGS=(
  --rm
  -v "${MOUNT_SRC}:/repo"
  -w /repo
  # Stale .pyc on the Windows bind mount has produced false GREENs before:
  # Windows' coarse mtime granularity let Python reuse bytecode for an edited
  # source file, so a mutant appeared to survive without ever executing.
  -e PYTHONDONTWRITEBYTECODE=1
)

# A `git worktree` checkout has a `.git` FILE pointing, by ABSOLUTE host path,
# into the main checkout's `.git/worktrees/<name>`. That path does not exist
# inside the container, so `git branch --show-current` (the branch-name gate)
# dies before a single test runs — which is how every "gated from the
# worktree" claim before 2026-09-23 was either a hand-rolled docker run or
# untrue. Mount the main checkout's `.git` READ-ONLY at a fixed path and tell
# git where its dirs are; the worktree's own `commondir` file is relative
# (`../..`), so it resolves inside the mount by itself. Read-only because the
# main checkout is usually the LIVE deployment's working tree.
SAFE_DIRS=("/repo")
if [[ -f "$REPO_ROOT/.git" ]]; then
  WT_GITDIR="$(sed -n 's/^gitdir: //p' "$REPO_ROOT/.git")"
  WT_NAME="$(basename "$WT_GITDIR")"
  MAIN_GIT="$(cd "$WT_GITDIR/../.." && { pwd -W 2>/dev/null || pwd; })"
  DOCKER_ARGS+=(
    -v "${MAIN_GIT}:/maingit:ro"
    -e "GIT_DIR=/maingit/worktrees/${WT_NAME}"
    -e GIT_WORK_TREE=/repo
  )
  SAFE_DIRS=("/repo" "/maingit" "/maingit/worktrees/${WT_NAME}")
  echo "▶ verify: git worktree '${WT_NAME}' — main .git mounted read-only"
fi

# Build a bash ARRAY LITERAL to splice into the container-side script below.
# The container script is itself a double-quoted `bash -lc "..."` string, so
# `${SAFE_DIRS[@]}` here would be expanded and word-split by THIS (host)
# shell before the container ever sees it — losing quoting for any entry
# with a space (a real risk: MAIN_GIT is a Windows path that can contain
# one). `printf %q` renders each entry as a shell-safe token; wrapping the
# lot in `(...)` makes the container's own `bash -lc` re-parse it as a proper
# array, so its `for d in "${SAFE_DIRS[@]}"` iterates whole paths, not words.
SAFE_DIRS_LITERAL="("
for d in "${SAFE_DIRS[@]}"; do
  SAFE_DIRS_LITERAL+="$(printf '%q' "$d") "
done
SAFE_DIRS_LITERAL+=")"

# The integration suite spawns real Postgres/Neo4j/Redis via testcontainers,
# which needs the host Docker socket reachable from inside this container.
if [[ "$MODE" != "offline" ]]; then
  DOCKER_ARGS+=(
    -v /var/run/docker.sock:/var/run/docker.sock
    --add-host host.docker.internal:host-gateway
    -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal
    -e TESTCONTAINERS_RYUK_DISABLED=true
  )
  EXTRA_APT="docker.io"
else
  EXTRA_APT=""
fi

echo "▶ verify: make ${TARGET} (in python:3.11-slim, repo root mounted)"
echo

# `make` and `git` are not in python:3.11-slim; the branch-name gate shells out
# to git. Install quietly, then hand off to the Makefile and let it be the
# authority on what the gate actually is.
docker run "${DOCKER_ARGS[@]}" python:3.11-slim bash -lc "
  set -euo pipefail
  apt-get update -qq >/dev/null 2>&1
  apt-get install -y -qq make git ${EXTRA_APT} >/dev/null 2>&1
  SAFE_DIRS=${SAFE_DIRS_LITERAL}
  for d in \"\${SAFE_DIRS[@]}\"; do git config --global --add safe.directory \"\$d\"; done
  find /repo/core -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
  pip install -q -r core/requirements.txt -r core/requirements-dev.txt
  make ${TARGET}
"
