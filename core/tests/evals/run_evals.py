"""Phase-4a ranking-evals harness -- STUB.

This script is what the `ranking-evals` merge-blocking gate
(`.claude/agents/ranking-evals.md`) will run once Phase 4c lands
`src.pipeline.matching.orchestrator`. Today it is expected to FAIL, and to
fail for exactly one reason: the orchestrator does not exist yet.

Do NOT read a green run of this script as "ranking works" -- it is a
scaffold. `core/tests/unit/test_evals_corpus.py` is what actually proves the
fixture corpus + thresholds.toml are well-formed right now.

Usage (once 4c lands):
    cd core && python tests/evals/run_evals.py

Computes, against `fixtures/` + `thresholds.toml`:
  - precision@k        -- fraction of the top-k shortlist tagged strong/borderline
  - evidence-verification-rate -- fraction of surfaced quotes that fuzzy-match
                          (>= thresholds.evidence.fuzz_threshold) their cited chunk
  - PII-leak check      -- candidate name/email/phone must never appear in
                          embedding text or exported output
  - determinism         -- repeated runs at temperature=0 produce identical
                          score_final / ranking order
"""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup: this file lives at core/tests/evals/run_evals.py. `src` is a
# top-level package rooted at `core/`, so make sure `core/` is importable
# whether this script is invoked as `python run_evals.py` (cwd == this dir),
# `python tests/evals/run_evals.py` (cwd == core/), or via an absolute path
# from anywhere.
# ---------------------------------------------------------------------------
_EVALS_DIR = Path(__file__).resolve().parent
_CORE_ROOT = _EVALS_DIR.parents[1]
if str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

FIXTURES_DIR = _EVALS_DIR / "fixtures"
THRESHOLDS_PATH = _EVALS_DIR / "thresholds.toml"

# ---------------------------------------------------------------------------
# The Phase 4c dependency. Guarded import: this is expected to fail today
# (ModuleNotFoundError) because src.pipeline.matching.orchestrator does not
# exist until Phase 4c ("4c - Matching engine" in docs/EXTRACTION_PLAN.md).
# We do NOT let this raise at import time so that a future test suite can
# still `import run_evals` and unit-test the fixture-loading helpers below
# without an orchestrator; `main()` is what enforces the hard failure.
# ---------------------------------------------------------------------------
_ORCHESTRATOR_IMPORT_ERROR: ModuleNotFoundError | None
try:
    from src.pipeline.matching.orchestrator import (  # type: ignore[import-not-found]
        run_match,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - exercised until 4c lands
    run_match = None  # type: ignore[assignment]
    _ORCHESTRATOR_IMPORT_ERROR = exc
else:  # pragma: no cover - not reachable until 4c lands
    _ORCHESTRATOR_IMPORT_ERROR = None

_NOT_IMPLEMENTED_MSG = (
    "ranking-evals: src.pipeline.matching.orchestrator is not implemented yet "
    "(Phase 4c of docs/EXTRACTION_PLAN.md -- 'Matching engine'). This is the "
    "expected RED state for Phase 4a (evals corpus). Once 4c lands the "
    "orchestrator (`run_match` or equivalent stage-1..4 entrypoint), wire it "
    "into `_run_corpus()` below and this script will compute precision@k, "
    "evidence-verification-rate, PII-leak, and determinism against "
    "thresholds.toml. This is NOT a fixture bug -- "
    "`pytest tests/unit/test_evals_corpus.py` proves the corpus itself is "
    "valid independently of the orchestrator."
)


@dataclass(frozen=True)
class ResumeFixture:
    """One labelled resume fixture loaded from fixtures/resumes/*.json."""

    resume_id: str
    tag: str
    path: Path
    parsed: dict[str, Any]


@dataclass(frozen=True)
class Corpus:
    job_id: str
    job_extracted: dict[str, Any]
    resumes: tuple[ResumeFixture, ...]


def load_thresholds() -> dict[str, Any]:
    """Parse thresholds.toml. Pure I/O + parsing -- no orchestrator needed."""
    with THRESHOLDS_PATH.open("rb") as fh:
        return tomllib.load(fh)


def load_corpus() -> Corpus:
    """Load the JD + labelled resumes from fixtures/. Pure I/O -- no
    orchestrator needed; this is what test_evals_corpus.py exercises today."""
    with (FIXTURES_DIR / "labels.json").open("r", encoding="utf-8") as fh:
        labels = json.load(fh)

    job_meta = labels["job"]
    with (FIXTURES_DIR / job_meta["fixture"]).open("r", encoding="utf-8") as fh:
        job_extracted = json.load(fh)

    resumes: list[ResumeFixture] = []
    for resume_id, entry in labels["resumes"].items():
        with (FIXTURES_DIR / entry["fixture"]).open("r", encoding="utf-8") as fh:
            parsed = json.load(fh)
        resumes.append(
            ResumeFixture(
                resume_id=resume_id,
                tag=entry["tag"],
                path=FIXTURES_DIR / entry["fixture"],
                parsed=parsed,
            )
        )

    return Corpus(
        job_id=job_meta["id"], job_extracted=job_extracted, resumes=tuple(resumes)
    )


def _run_corpus(corpus: Corpus, thresholds: dict[str, Any]) -> None:
    """Run the corpus through the (not-yet-built) orchestrator and compute
    precision@k / evidence-verification-rate / PII-leak / determinism.

    Deliberately unimplemented until Phase 4c. Reaching this function at all
    means the guarded import above unexpectedly succeeded -- which should
    only happen after `src.pipeline.matching.orchestrator` lands, at which
    point this stub must be replaced with the real scoring/verification
    logic (not just made to return True).
    """
    raise NotImplementedError(
        "run_evals._run_corpus is a Phase 4a stub. Wire src.pipeline.matching."
        "orchestrator.run_match into this function as part of Phase 4c before "
        "trusting a green run of this script."
    )


def main() -> int:
    if _ORCHESTRATOR_IMPORT_ERROR is not None:
        print(_NOT_IMPLEMENTED_MSG, file=sys.stderr)
        return 1

    thresholds = load_thresholds()
    corpus = load_corpus()
    _run_corpus(corpus, thresholds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
