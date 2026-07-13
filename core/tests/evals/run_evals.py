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

THE THRESHOLD KEY SET IS A THREE-WAY CONTRACT between `thresholds.toml`, this
docstring, and `.claude/agents/ranking-evals.md`. Every key below is read
literally, and `core/tests/unit/test_evals_corpus.py::
test_every_threshold_key_is_enumerated_by_both_consumers` PARSES this docstring
(a section is a 2-space-indented `[name]`; a key is a 4-space-indented token)
and fails if the toml grows or loses a key without both consumers being updated
in the same change. Keep the indentation shape. (That drift already happened
once: the toml grew `[adversarial]` and `[evidence].min_completeness_in_topk`
and neither consumer enumerated them -- so a 4c coder wiring this harness from
this docstring would have built a gate that a naive pure-vector ranker passes.
And until round 4 the contract test the comments named did not exist at all.)

Computes, against `fixtures/` + `thresholds.toml` -- EVERY key, none optional:

  [precision_at_k]
    k = 5                       -- shortlist window
    min_precision = 1.0         -- ALL of the top-k must be tagged strong/
                                   borderline in labels.json. Exactly 1.0: a
                                   lower floor admits a weak/adversarial
                                   fixture into the top-k and contradicts
                                   [adversarial].must_not_surface_in_topk.
  [evidence]
    verification_rate_min = 1.0 -- fraction of SURFACED quotes that fuzzy-match
                                   (>= fuzz_threshold, rapidfuzz partial_ratio
                                   or token_set_ratio -- NOT fuzz.ratio, which
                                   scores this corpus's own gold anchors at
                                   0.648/0.796 and so can never reach 1.0)
                                   against their cited chunk. Any unverifiable
                                   quote reaching output is a hard fail
                                   (anti-fabrication invariant).
    fuzz_threshold = 0.85       -- == MatchWeights.evidence_verify_fuzz.
    min_completeness_in_topk    -- = 1.0, PINNED. Fraction of top-k entries
                                   carrying >= 1 VERIFIED quote. Stops
                                   verification_rate_min passing vacuously over
                                   an empty quote set.
    negative_evidence_must_fail -- labels.json's `negative_evidence` quotes are
                                   FABRICATED and MUST score below
                                   fuzz_threshold against their cited chunk.
                                   Without these, verification_rate_min = 1.0 is
                                   satisfiable by a verifier that always returns
                                   True. Beware the measure: fuzz.WRatio scores
                                   r02's fabricated anchor at 0.855 >= 0.85, and
                                   partial_token_set_ratio returns 1.000 on 2 of
                                   the 4 negatives.
  [adversarial]
    must_not_surface_in_topk    -- r09 (the keyword-stuffer) must never appear
                                   in the top-k. Same for every fixture flagged
                                   must_not_surface_in_topk in labels.json. r09
                                   is structurally top-tier on ALL FIVE
                                   structured sub-scores -- skill, experience,
                                   seniority, education (a JD-allowed BSc) and
                                   vector (every JD skill in the embedded
                                   summary) -- so ONLY the evidence verifier can
                                   reject it. It ranks ~11, ADJACENT to the
                                   borderline tier and not below every weak
                                   fixture: 0.6*structured + 0.3*0 + 0.1*0 is
                                   arithmetic. Do not "fix" that by re-tagging
                                   r09 or moving a threshold.
  [ordering_controls]
    enforce = true              -- for every pair below, the live ranker must
    pairs = [...]                  place higher_id STRICTLY above lower_id:
                                   rank(higher) < rank(lower). Each pair is
                                   identical in every scoring-relevant field
                                   except one dimension (education / overqual /
                                   motivation), so a ranker blind to that
                                   dimension fails. These are the corpus's most
                                   discriminating assertions.
  [pii]
    leak_check = true           -- no fixture's candidate name/email/phone may
                                   appear in embedding input or exported output.
    allow_structured_fields     -- ADR-007 N1: structured experience/education
    structured_fields_surface      free text may carry identity ONLY on the
                                   surface named here (`outbox_at_rest`).
    embedding_input_pii_free    -- embedding input must contain no name/email/
    exported_output_pii_free       phone REGARDLESS of the originating field
                                   (ADR-007 §7-F1). A bullet-derived chunk is
                                   NOT exempt: r12's c_003 is byte-identical to
                                   its bullet text.
  [determinism]
    temperature = 0.0           -- pinned for the eval runs.
    max_rank_delta = 0          -- ZERO tolerance: ranking ORDER must be
                                   identical across two runs.
    max_score_delta = 1e-9      -- score_final gets an epsilon, not exact
                                   equality. 4c REQUIREMENT: pin `seed` on the
                                   eval path and state the embedding-cache state
                                   (cold vs warm) across the two runs -- with a
                                   warm Redis cache this check compares the
                                   cache to itself, not the model to itself.
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
    run_match = None
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


def _fixture_path(relative: str) -> Path:
    """Resolve a labels.json `fixture` value inside FIXTURES_DIR.

    Defense in depth (test-only surface, low severity): the value comes from a
    JSON file, and in pathlib an ABSOLUTE right-hand side silently REPLACES the
    left-hand side (``Path("/a") / "/etc/passwd" == Path("/etc/passwd")``), so
    a plain join would happily read anything on disk. Resolve and confine.
    """
    root = FIXTURES_DIR.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(
            f"labels.json fixture path {relative!r} resolves to {candidate}, "
            f"which is outside the fixtures directory {root}"
        )
    return candidate


def load_corpus() -> Corpus:
    """Load the JD + labelled resumes from fixtures/. Pure I/O -- no
    orchestrator needed; this is what test_evals_corpus.py exercises today."""
    with (FIXTURES_DIR / "labels.json").open("r", encoding="utf-8") as fh:
        labels = json.load(fh)

    job_meta = labels["job"]
    with _fixture_path(job_meta["fixture"]).open("r", encoding="utf-8") as fh:
        job_extracted = json.load(fh)

    resumes: list[ResumeFixture] = []
    for resume_id, entry in labels["resumes"].items():
        path = _fixture_path(entry["fixture"])
        with path.open("r", encoding="utf-8") as fh:
            parsed = json.load(fh)
        resumes.append(
            ResumeFixture(
                resume_id=resume_id,
                tag=entry["tag"],
                path=path,
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
