#!/usr/bin/env bash
# Split ONE combined Taleo applicant-export PDF into per-applicant PDFs.
#
# The splitter itself lives at core/scripts/split_taleo_pdf.py so it is inside
# the worker image's bind mount (./core -> /app) and inside the lint/type
# gates. THIS file is only the plumbing: it mounts your input file and output
# directory into a throwaway worker container so you never have to think about
# container paths.
#
#   scripts/split-taleo.sh ./taleo_export.pdf --output ./split [--zip] [...]
#
# Everything after the output directory is passed through to the splitter
# (--zip, --heuristic, --ranges "1-2;3-5", --model, --dry-run).
#
# The export is real candidate PII. Keep both paths outside the repo, or under
# the gitignored fixtures/ — nothing this writes should ever be committed.
set -euo pipefail

usage() { echo "usage: $0 <export.pdf> --output <dir> [splitter args...]" >&2; exit 2; }

[ $# -ge 3 ] || usage
INPUT="$1"; shift
[ "$1" = "--output" ] || usage
OUTDIR="$2"; shift 2

[ -f "$INPUT" ] || { echo "no such file: $INPUT" >&2; exit 1; }
mkdir -p "$OUTDIR"

# Absolute paths — a bind mount cannot take a relative one.
IN_DIR="$(cd "$(dirname "$INPUT")" && pwd)"
IN_FILE="$(basename "$INPUT")"
OUT_DIR="$(cd "$OUTDIR" && pwd)"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# `run --rm`, not `exec`: this needs no running stack, and it must not leave a
# container holding a mount of a directory full of candidate PII.
exec docker compose run --rm --no-deps \
  -v "${IN_DIR}:/in:ro" -v "${OUT_DIR}:/out" \
  worker python scripts/split_taleo_pdf.py "/in/${IN_FILE}" --output /out "$@"
