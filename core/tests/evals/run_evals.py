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
    gold_recall_min = 1.0       -- (round-8 addition, R2 closure) fraction of
                                   labels.json's `gold_evidence` anchors that
                                   must come back `status="met"` with a
                                   SURVIVING verified quote from the FULL
                                   extract-then-verify pipeline. Every other
                                   key in this section gates the VERIFY half
                                   only (an extractor that never finds real
                                   evidence in the first place passes all of
                                   them vacuously, short of the
                                   min_completeness_in_topk floor). Gates the
                                   extractor's RECALL, not just the verifier's
                                   precision.
  [adversarial]
    must_not_surface_in_topk    -- r09 (the keyword-stuffer) must never appear
                                   in the top-k. Same for every fixture flagged
                                   must_not_surface_in_topk in labels.json. r09
                                   is structurally top-tier on ALL FIVE
                                   structured sub-scores -- skill, experience,
                                   seniority (its most-recent title is the JD
                                   title VERBATIM, so the cosine is 1.0 by
                                   arithmetic), education (a JD-allowed BSc) and
                                   vector (every JD skill in the embedded
                                   summary) -- so ONLY the evidence verifier can
                                   reject it. Scoring 0.6*structured + 0.3*0 +
                                   0.1*0 ~= 0.597, it lands just BELOW THE STRONG
                                   TIER (near-tied with r04, so its exact rank
                                   moves between builds and is NOT gated) and
                                   ~0.19 clear of the k=5 cutoff -- not below
                                   every weak fixture. That is arithmetic: do not
                                   "fix" it by re-tagging r09 or moving a
                                   threshold, and do not re-pin an exact rank.
  [ordering_controls]
    enforce = true              -- for every pair below, the live ranker must
    pairs = [...]                  satisfy BOTH halves: rank(higher) <
    min_score_gap = 1e-6           rank(lower), AND score_final(higher) -
                                   score_final(lower) >= min_score_gap. Each pair
                                   is identical in every scoring-relevant field
                                   except one dimension (education / overqual /
                                   motivation / skill_missing_must / recency --
                                   the last two added round 8, see
                                   fixtures/labels.json and thresholds.toml for
                                   their rationale), so a ranker blind to that
                                   dimension fails -- but ONLY IF THE GAP IS
                                   CHECKED TOO (round-6 finding F5): the overqual
                                   and motivation twins have BYTE-IDENTICAL
                                   embedding input (_build_summary_text reads
                                   neither total_years_experience nor
                                   cover_letter_chunks), so a blind engine ties
                                   them EXACTLY and the stable sort decides the
                                   pair arbitrarily -- a motivation-blind engine
                                   PASSED the rank-only assertion. min_score_gap
                                   is an anti-tie epsilon: > 0 (else the tie
                                   passes) and far below the smallest correct-
                                   engine gap (0.0120 -- the overqual pair's, and
                                   the ONLY one of the three original pairs that
                                   is arithmetic rather than measured -- the two
                                   round-8 pairs' gaps (0.144 each) are larger
                                   still; see thresholds.toml).
                                   REVIEW OBLIGATION, NOT A GATE (round-7 M-3,
                                   extended round 8): the three original
                                   blind-engine mutations (education = 0,
                                   overqual_ratio = 99, motivation = 0) plus
                                   weights.skill = 0 and a recency-decay-disabled
                                   mutation are what prove these pairs gate their
                                   dimension at all, and each must FAIL on BOTH
                                   input orders -- but nothing in this file or
                                   thresholds.toml can run them, because they
                                   need the engine with MUTATED MatchWeights.
                                   They belong in 4c's own test suite, and 4c's
                                   reviewer must check for them. Do not read this
                                   key as enforcing them. A SIXTH mutation,
                                   must_have_miss_penalty: 0.5 -> 1.0, is a
                                   review obligation of a DIFFERENT shape (round
                                   8): it cannot be expressed as a pairwise
                                   rank+gap check at all (removing the penalty
                                   only shrinks, never reverses, the
                                   skill_missing_must pair's gap -- an algebraic
                                   property of the mean+multiplicative-penalty
                                   formula, not a fixture gap). It must be
                                   verified numerically on r18 in isolation
                                   instead; see fixtures/labels.json's
                                   skill_missing_must rationale and
                                   core/tests/evals/README_4c_twins.md.
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

import hashlib
import json
import re
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


# ---------------------------------------------------------------------------
# Phase 4c: the offline, deterministic corpus engine.
#
# run_evals runs with NO live Postgres/Neo4j/Ollama (it must exit 0 in a bare
# `python:3.11-slim`), so it reproduces the live orchestrator's 4-stage
# arithmetic against the fixtures directly, using the SAME pure stage functions
# the DB path uses (src.pipeline.matching.stages) plus the SAME skill
# normalisation / family ontology (src.pipeline.skills / skills_graph) and the
# SAME PII redaction the embed boundary uses (src.worker.resume_tasks). Only the
# two I/O surfaces the script can't have offline are stood in for
# deterministically:
#
#   * the embedder -> a deterministic bag-of-words hashing embedder
#     (`_embed`): byte-identical text embeds to an identical vector (so the
#     byte-identical twins tie EXACTLY, as the corpus's min_score_gap design
#     requires) and a one-token edit moves the vector only slightly (so the
#     education / skill_missing_must residuals stay dominated by their real
#     sub-score gaps, never inverted the way a crc32-random embedder would).
#   * the stage-3 LLM extractor -> `_extract_evidence`: a faithful extractor
#     quotes a requirement's evidence from a resume CHUNK that actually contains
#     the skill term (deterministic vocab scan). A keyword-stuffer whose chunks
#     carry only filler (r09) yields no quote for any requirement -> its
#     evidence completeness is 0 by construction, exactly the mechanism the
#     anti-fabrication backstop relies on. The verifier (stages.verify_evidence,
#     rapidfuzz partial_ratio at 0.85) is the REAL one.
#
# Determinism (thresholds.toml [determinism]): the embedder carries no RNG and
# `today_year` is pinned (`_EVAL_TODAY_YEAR`), so two runs are bit-identical.
# Cache state: cold / none (in-process, no Redis) — so the determinism check
# compares the engine to itself, never a warm cache to itself.
# ---------------------------------------------------------------------------

_EVAL_TODAY_YEAR = 2026  # pinned "now" — the corpus's recent bucket is 2026
_EMBED_DIM = 768
_NON_MATCHABLE_FAMILIES = ("other", "domain")


def _embed(text: str) -> list[float]:
    """Deterministic bag-of-words hashing embedder (768-d).

    Semantically SMOOTH — a one-token edit changes exactly one bucket, so twins
    that differ by a single skill/degree token stay near-parallel (unlike a
    crc32-of-the-whole-string embedder, which would make a one-token edit
    orthogonal and invert the small vector residuals the twin pairs depend on
    being dominated). Byte-identical text -> identical vector.
    """
    vec = [0.0] * _EMBED_DIM
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        vec[hash_token(tok) % _EMBED_DIM] += 1.0
    return vec


def hash_token(tok: str) -> int:
    """Stable, process-independent token hash (Python's ``hash`` is salted per
    process, which would break the determinism guarantee across runs)."""
    return int.from_bytes(hashlib.sha1(tok.encode("utf-8")).digest()[:8], "big")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _seniority_subscore(jd_title: str, recent_title: str, floor: float) -> float:
    """Cosine(jd.title, most-recent role title), rescaled [floor, 1] -> [0, 1] —
    the same shape orchestrator.py computes with a live embedder. r09's title is
    the JD title verbatim, so cosine(x, x) = 1.0 -> seniority 1.0 by arithmetic."""
    cos = _cosine(_embed(jd_title), _embed(recent_title))
    return max(0.0, min(1.0, (cos - floor) / (1.0 - floor)))


def _run_corpus(corpus: Corpus, thresholds: dict[str, Any]) -> None:
    """Run the corpus through the live stage functions and assert every
    thresholds.toml gate. Raises AssertionError on any violation (so
    ``main()`` returns non-zero); returns None on a fully green corpus."""
    # Imports are local so the module still imports (and its fixture-loading
    # helpers stay unit-testable) even in the pre-4c import-guard state above.
    from src.pipeline.matching.orchestrator import RankInput, run_match
    from src.schemas.matching import DEFAULT_WEIGHTS, MatchWeights

    weights = DEFAULT_WEIGHTS
    scored = _score_corpus(corpus, weights)
    ranked = run_match(
        [
            RankInput(
                resume_id=rid,
                structured=s.structured,
                breakdown=s.breakdown,
                evidence=s.evidence,
            )
            for rid, s in scored.items()
        ],
        weights,
    )
    by_id = {m.resume_id: m for m in ranked}
    tag_by_id = {r.resume_id: r.tag for r in corpus.resumes}
    labels = _labels()

    # ── precision@k ────────────────────────────────────────────────────────
    k = int(thresholds["precision_at_k"]["k"])
    min_precision = float(thresholds["precision_at_k"]["min_precision"])
    topk = sorted(ranked, key=lambda m: m.rank)[:k]
    good = sum(1 for m in topk if tag_by_id[m.resume_id] in {"strong", "borderline"})
    precision = good / len(topk)
    assert precision >= min_precision, (
        f"precision@{k}={precision:.3f} < {min_precision}: top-k = "
        f"{[(m.resume_id, tag_by_id[m.resume_id]) for m in topk]}"
    )

    # ── adversarial / weak backstop: must_not_surface_in_topk ──────────────
    topk_ids = {m.resume_id for m in topk}
    for rid, entry in labels["resumes"].items():
        if entry.get("must_not_surface_in_topk"):
            assert rid not in topk_ids, (
                f"{rid} is flagged must_not_surface_in_topk but appears in the "
                f"top-{k} (rank {by_id[rid].rank}, score {by_id[rid].score_final:.4f})"
            )

    # ── evidence: verification_rate + min_completeness_in_topk ─────────────
    verify_fuzz = float(thresholds["evidence"]["fuzz_threshold"])
    assert (
        verify_fuzz == MatchWeights().evidence_verify_fuzz
    ), "thresholds.toml fuzz_threshold drifted from MatchWeights.evidence_verify_fuzz"
    for m in topk:
        surviving = [
            r
            for r in (m.evidence.requirements if m.evidence else [])
            if r.status == "met" and r.evidence
        ]
        assert surviving, (
            f"min_completeness_in_topk: top-k entry {m.resume_id} carries no "
            "verified evidence quote"
        )
    # Every SURFACED quote across the whole ranking must verify against its
    # cited chunk (verification_rate_min = 1.0). Surfaced == survived the
    # verifier: non-empty evidence, full stop. Conditioning on a citation as
    # well used to exclude uncited quotes from this check entirely.
    for rid, s in scored.items():
        chunks_by_id = s.chunks_by_id
        for req in s.evidence.requirements if s.evidence else []:
            if req.evidence:
                assert any(
                    _partial_ratio(req.evidence, chunks_by_id.get(cid, ""))
                    >= verify_fuzz
                    for cid in req.evidence_chunk_ids
                ), f"{rid}: surfaced quote does not verify against its cited chunk"

    # ── negative_evidence_must_fail ────────────────────────────────────────
    if thresholds["evidence"]["negative_evidence_must_fail"]:
        _assert_fabrications_are_scrubbed(scored, weights)

    # ── gold_recall_min ────────────────────────────────────────────────────
    gold_recall_min = float(thresholds["evidence"]["gold_recall_min"])
    _assert_gold_recall(scored, gold_recall_min, verify_fuzz)

    # ── ordering_controls ──────────────────────────────────────────────────
    if thresholds["ordering_controls"]["enforce"]:
        min_gap = float(thresholds["ordering_controls"]["min_score_gap"])
        for pair in thresholds["ordering_controls"]["pairs"]:
            hi, lo = pair["higher_id"], pair["lower_id"]
            hi_m, lo_m = by_id[hi], by_id[lo]
            assert hi_m.rank < lo_m.rank, (
                f"ordering_controls[{pair['dimension']}]: rank({hi})={hi_m.rank} "
                f"is not above rank({lo})={lo_m.rank}"
            )
            gap = hi_m.score_final - lo_m.score_final
            assert gap >= min_gap, (
                f"ordering_controls[{pair['dimension']}]: gap {gap:.6e} < "
                f"min_score_gap {min_gap:.6e}"
            )

    # ── must_have_miss_penalty is WIRED (review obligation on r18 alone) ───
    _assert_must_have_penalty_fires_on_r18(corpus)

    # ── pii leak-check ─────────────────────────────────────────────────────
    if thresholds["pii"]["leak_check"]:
        _assert_no_pii_leak(corpus, scored, topk_ids)

    # ── determinism (two runs, pinned seed / cold cache) ───────────────────
    _assert_deterministic(corpus, weights, ranked)


@dataclass(frozen=True)
class _Scored:
    """Everything a single resume contributes to the ranking + the audits."""

    structured: float
    breakdown: Any
    evidence: Any
    chunks_by_id: dict[str, str]
    vec_raw: float


def _labels() -> dict[str, Any]:
    with (FIXTURES_DIR / "labels.json").open(encoding="utf-8") as fh:
        result: dict[str, Any] = json.load(fh)
        return result


def _partial_ratio(needle: str, haystack: str) -> float:
    from rapidfuzz import fuzz

    if not needle:
        return 0.0
    return float(fuzz.partial_ratio(needle.lower().strip(), haystack.lower())) / 100.0


def _jd_view(corpus: Corpus) -> dict[str, Any]:
    """Materialise the JD once: canonical required/nice skills + min_years,
    education level, title, and the JD's own summary embedding text."""
    from src.pipeline.skills import _basic_normalise, build_summary_text
    from src.schemas.jobs import JDExtracted

    jd = JDExtracted.model_validate(corpus.job_extracted)
    required = [
        (s.name, _basic_normalise(s.name), s.min_years) for s in jd.required_skills
    ]
    nice = [(s.name, _basic_normalise(s.name)) for s in jd.nice_to_have_skills]
    return {
        "title": jd.title,
        "min_years": jd.min_years_experience or None,
        "edu_min_level": jd.education.min_level if jd.education else None,
        "required": required,
        "nice": nice,
        "summary_text": build_summary_text(jd),
    }


def _candidate_families(canonicals: set[str]) -> set[str]:
    from src.pipeline.skills_graph import categories_for

    fams: set[str] = set()
    for c in canonicals:
        for f in categories_for(c):
            if f not in _NON_MATCHABLE_FAMILIES:
                fams.add(f)
    return fams


def _skill_rows_for(parsed: dict[str, Any], jd: dict[str, Any]) -> list[Any]:
    """Reproduce _stage2_skill_rows against fixture JSON: direct match ->
    ontology_weight 1.0; shared curated family -> 0.5; else 0.0. REQUIRES-only
    (blocker #5: nice-to-haves never feed this)."""
    from src.pipeline.matching.stages import _SkillRowFromCypher
    from src.pipeline.skills import _basic_normalise
    from src.pipeline.skills_graph import categories_for

    cand: dict[str, dict[str, Any]] = {}
    for s in parsed.get("skills", []):
        canon = _basic_normalise(s["name"])
        if canon:
            cand.setdefault(canon, s)
    cand_fams = _candidate_families(set(cand))

    rows: list[Any] = []
    for _name, canon, min_years in jd["required"]:
        if canon in cand:
            s = cand[canon]
            rows.append(
                _SkillRowFromCypher(
                    skill=canon,
                    req_years=min_years,
                    is_must_have=True,
                    years=s.get("years"),
                    last_used_year=s.get("last_used_year"),
                    ontology_weight=1.0,
                )
            )
            continue
        req_fams = {
            f for f in categories_for(canon) if f not in _NON_MATCHABLE_FAMILIES
        }
        weight = 0.5 if req_fams & cand_fams else 0.0
        rows.append(
            _SkillRowFromCypher(
                skill=canon,
                req_years=min_years,
                is_must_have=True,
                years=None,
                last_used_year=None,
                ontology_weight=weight,
            )
        )
    return rows


def _extract_evidence(
    parsed: dict[str, Any], jd: dict[str, Any]
) -> tuple[Any, dict[str, str]]:
    """Deterministic stand-in for the stage-3 LLM extractor + the REAL verifier.

    For each requirement (required + nice-to-have — blocker #5) find a resume
    CHUNK that actually contains the skill term (vocab scan) and quote it; a
    keyword-stuffer whose chunks carry only filler yields no quote and the
    requirement comes back missing. Then run the real anti-fabrication verifier.
    """
    from src.pipeline.matching.stages import verify_evidence
    from src.pipeline.skills import match_skills_in_text
    from src.schemas.matching import (
        CoverLetterEvidence,
        EvidenceObject,
        RequirementEvidence,
    )

    chunks = parsed.get("chunks") or []
    chunks_by_id = {c["id"]: c["text"] for c in chunks if c.get("id")}
    # canonical skill -> first chunk that mentions it
    chunk_for_canon: dict[str, tuple[str, str]] = {}
    for c in chunks:
        found = match_skills_in_text(c.get("text", ""))
        for canon in found:
            chunk_for_canon.setdefault(canon, (c["id"], c["text"]))

    reqs: list[Any] = []
    for name, canon, _my in jd["required"]:
        hit = chunk_for_canon.get(canon)
        if hit:
            reqs.append(
                RequirementEvidence(
                    requirement=name,
                    status="met",
                    evidence=hit[1],
                    evidence_chunk_ids=[hit[0]],
                    confidence=0.9,
                )
            )
        else:
            reqs.append(RequirementEvidence(requirement=name, status="missing"))
    for name, canon in jd["nice"]:
        hit = chunk_for_canon.get(canon)
        if hit:
            reqs.append(
                RequirementEvidence(
                    requirement=name,
                    status="met",
                    evidence=hit[1],
                    evidence_chunk_ids=[hit[0]],
                    confidence=0.9,
                )
            )
        else:
            reqs.append(RequirementEvidence(requirement=name, status="missing"))

    cover: list[Any] = []
    cl_chunks = parsed.get("cover_letter_chunks") or []
    for c in cl_chunks:
        chunks_by_id[c["id"]] = c["text"]
    if cl_chunks:
        first = cl_chunks[0]
        cover.append(
            CoverLetterEvidence(
                theme="motivation",
                evidence=first["text"],
                evidence_chunk_ids=[first["id"]],
                confidence=0.9,
            )
        )
    evidence = EvidenceObject(requirements=reqs, cover_letter_evidence=cover)
    verified = verify_evidence(evidence, chunks_by_id)
    return verified, chunks_by_id


def _breakdown_for(
    parsed: dict[str, Any], jd: dict[str, Any], vec_normalised: float, weights: Any
) -> tuple[float, Any]:
    from src.pipeline.matching.orchestrator import (
        _level_from_degree,
        _most_recent_title,
    )
    from src.pipeline.matching.stages import (
        is_senior_candidate,
        score_education,
        score_experience,
        score_skill_breakdown,
    )
    from src.schemas.matching import ScoreBreakdown

    total_years = parsed.get("total_years_experience")
    senior = is_senior_candidate(total_years, jd["min_years"], weights=weights)
    rows = _skill_rows_for(parsed, jd)
    skill_overall, contribs = score_skill_breakdown(
        rows, weights=weights, senior=senior, today_year=_EVAL_TODAY_YEAR
    )
    exp = score_experience(total_years, jd["min_years"], weights=weights)
    levels = [_level_from_degree(e.get("degree")) for e in parsed.get("education", [])]
    edu = score_education(levels, jd["edu_min_level"], weights=weights)
    recent = _most_recent_title(parsed)
    seniority = (
        _seniority_subscore(jd["title"], recent, weights.seniority_floor)
        if recent
        else 0.0
    )
    structured = (
        weights.skill * skill_overall
        + weights.experience * exp
        + weights.education * edu
        + weights.seniority * seniority
        + weights.vector * vec_normalised
    )
    breakdown = ScoreBreakdown(
        skill=skill_overall,
        experience=exp,
        education=edu,
        seniority=seniority,
        vector=vec_normalised,
        structured=structured,
        implied_experience=any(c.reason == "implied-experience" for c in contribs),
        skill_contributions=contribs,
    )
    return structured, breakdown


def _score_corpus(corpus: Corpus, weights: Any) -> dict[str, _Scored]:
    """Full stages 1-4 (minus the final combine) per resume, against fixtures."""
    from src.pipeline.matching.stages import normalise_vector_scores
    from src.schemas.resumes import ResumeParsed
    from src.worker.resume_tasks import _build_summary_text, _redact_candidate_pii

    jd = _jd_view(corpus)
    jd_emb = _embed(jd["summary_text"])

    # Stage-1 vector score = cosine(JD summary, redacted resume summary), then
    # min-max normalised across the pool (stages.normalise_vector_scores).
    vec_raw: dict[str, float] = {}
    for r in corpus.resumes:
        rp = ResumeParsed.model_validate(r.parsed)
        summary_text = _redact_candidate_pii(_build_summary_text(rp), rp.candidate)
        vec_raw[r.resume_id] = _cosine(jd_emb, _embed(summary_text))
    ids = [r.resume_id for r in corpus.resumes]
    vec_norm = dict(
        zip(ids, normalise_vector_scores([vec_raw[i] for i in ids]), strict=True)
    )

    out: dict[str, _Scored] = {}
    for r in corpus.resumes:
        structured, breakdown = _breakdown_for(
            r.parsed, jd, vec_norm[r.resume_id], weights
        )
        evidence, chunks_by_id = _extract_evidence(r.parsed, jd)
        out[r.resume_id] = _Scored(
            structured=structured,
            breakdown=breakdown,
            evidence=evidence,
            chunks_by_id=chunks_by_id,
            vec_raw=vec_raw[r.resume_id],
        )
    return out


def _assert_fabrications_are_scrubbed(scored: dict[str, _Scored], weights: Any) -> None:
    """Every labels.json negative_evidence anchor is a FABRICATION and must be
    blanked by the verifier (score < evidence_verify_fuzz against its chunk)."""
    from src.pipeline.matching.stages import verify_evidence
    from src.schemas.matching import EvidenceObject, RequirementEvidence

    labels = _labels()
    n = 0
    for rid, entry in labels["resumes"].items():
        neg = entry.get("negative_evidence", {})
        chunks_by_id = scored[rid].chunks_by_id
        for chunk_id, fabricated in neg.items():
            assert chunk_id in chunks_by_id, f"{rid}: negative chunk {chunk_id} missing"
            ev = EvidenceObject(
                requirements=[
                    RequirementEvidence(
                        requirement="fabrication probe",
                        status="met",
                        evidence=fabricated,
                        evidence_chunk_ids=[chunk_id],
                        confidence=0.9,
                    )
                ]
            )
            cleaned = verify_evidence(ev, chunks_by_id, weights=weights).requirements[0]
            assert cleaned.evidence == "" and cleaned.status == "missing", (
                f"{rid}: fabricated quote against {chunk_id} was NOT scrubbed "
                f"(evidence={cleaned.evidence!r})"
            )
            n += 1
    assert n == 4, f"expected 4 negative_evidence fabrications, ran {n}"


def _assert_gold_recall(
    scored: dict[str, _Scored], gold_recall_min: float, verify_fuzz: float
) -> None:
    """Every labels.json gold_evidence anchor must come back met+verified from
    the FULL extract-then-verify pipeline (extractor recall, not just verifier
    precision)."""
    labels = _labels()
    total = 0
    recovered = 0
    for rid, entry in labels["resumes"].items():
        gold = entry.get("gold_evidence", {})
        if not gold:
            continue
        s = scored[rid]
        by_req = {
            r.requirement: r for r in (s.evidence.requirements if s.evidence else [])
        }
        for skill_name in gold:
            total += 1
            req = by_req.get(skill_name)
            if (
                req is not None
                and req.status == "met"
                and req.evidence
                and any(
                    _partial_ratio(req.evidence, s.chunks_by_id.get(cid, ""))
                    >= verify_fuzz
                    for cid in req.evidence_chunk_ids
                )
            ):
                recovered += 1
    assert total > 0, "no gold_evidence anchors found in labels.json"
    recall = recovered / total
    assert (
        recall >= gold_recall_min
    ), f"gold_recall={recall:.3f} ({recovered}/{total}) < {gold_recall_min}"


def _assert_must_have_penalty_fires_on_r18(corpus: Corpus) -> None:
    """Single-candidate review obligation (README_4c_twins.md §4): r18's
    score_final at must_have_miss_penalty=0.5 must be measurably lower than at
    =1.0 — the mutation the pairwise ordering mechanism provably CANNOT gate."""
    from src.pipeline.matching.orchestrator import RankInput, run_match
    from src.schemas.matching import MatchWeights

    jd = _jd_view(corpus)
    r18 = next(
        r for r in corpus.resumes if r.resume_id == "r18_casey_rivera_missing_must_have"
    )

    def _final(penalty: float) -> float:
        # relief must be >= penalty for the MatchWeights validator; r18 is not
        # senior (6 < 5*1.5) so relief never fires and its value is inert here.
        w = MatchWeights(
            must_have_miss_penalty=penalty,
            implied_experience_relief=max(penalty, 0.75),
        )
        structured, breakdown = _breakdown_for(r18.parsed, jd, 0.5, w)
        evidence, _ = _extract_evidence(r18.parsed, jd)
        ranked = run_match(
            [
                RankInput(
                    resume_id="r18",
                    structured=structured,
                    breakdown=breakdown,
                    evidence=evidence,
                )
            ],
            w,
        )
        return ranked[0].score_final

    lo = _final(0.5)
    hi = _final(1.0)
    assert lo < hi - 0.02, (
        f"must_have_miss_penalty is not wired: score_final(0.5)={lo:.4f} is not "
        f"measurably below score_final(1.0)={hi:.4f}"
    )


def _assert_no_pii_leak(
    corpus: Corpus, scored: dict[str, _Scored], topk_ids: set[str]
) -> None:
    """embedding_input_pii_free (every fixture) + exported_output_pii_free
    (top-k evidence). Uses the SAME redaction the embed boundary uses; the r12/
    r17 self-dox controls exercise it."""
    from src.schemas.resumes import ResumeParsed
    from src.worker.resume_tasks import _build_summary_text, _redact_candidate_pii

    for r in corpus.resumes:
        rp = ResumeParsed.model_validate(r.parsed)
        cand = rp.candidate
        markers = _pii_markers(r.parsed.get("candidate", {}))
        # embedding input: redacted summary + every redacted chunk.
        embed_inputs = [_redact_candidate_pii(_build_summary_text(rp), cand)]
        embed_inputs += [
            _redact_candidate_pii(c.get("text", ""), cand)
            for c in r.parsed.get("chunks", [])
        ]
        for text in embed_inputs:
            low = text.lower()
            for marker in markers:
                assert (
                    marker not in low
                ), f"{r.resume_id}: PII marker {marker!r} leaked into embedding input"
    # exported output: the surfaced evidence quotes of the top-k entries.
    for rid in topk_ids:
        markers = _pii_markers(
            next(r.parsed for r in corpus.resumes if r.resume_id == rid).get(
                "candidate", {}
            )
        )
        for req in scored[rid].evidence.requirements if scored[rid].evidence else []:
            low = (req.evidence or "").lower()
            for marker in markers:
                assert (
                    marker not in low
                ), f"{rid}: PII marker {marker!r} leaked into exported evidence"


def _pii_markers(candidate: dict[str, Any]) -> list[str]:
    markers: list[str] = []
    name = (candidate.get("name") or "").strip().lower()
    if name:
        markers.append(name)
    email = (candidate.get("email") or "").strip().lower()
    if email:
        markers.append(email)
    phone = (candidate.get("phone") or "").strip()
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) >= 7:
            markers.append(digits)
    return markers


def _assert_deterministic(corpus: Corpus, weights: Any, first: list[Any]) -> None:
    """thresholds.toml [determinism]: max_rank_delta = 0 (order identical) and
    max_score_delta = 1e-9. Deterministic embedder + pinned today_year -> the
    second run is bit-identical to the first (cache state: cold / none)."""
    from src.pipeline.matching.orchestrator import RankInput, run_match

    scored = _score_corpus(corpus, weights)
    second = run_match(
        [
            RankInput(
                resume_id=rid,
                structured=s.structured,
                breakdown=s.breakdown,
                evidence=s.evidence,
            )
            for rid, s in scored.items()
        ],
        weights,
    )
    order1 = [m.resume_id for m in sorted(first, key=lambda m: m.rank)]
    order2 = [m.resume_id for m in sorted(second, key=lambda m: m.rank)]
    assert order1 == order2, "determinism: ranking order changed between two runs"
    s1 = {m.resume_id: m.score_final for m in first}
    for m in second:
        assert (
            abs(m.score_final - s1[m.resume_id]) <= 1e-9
        ), f"determinism: score_final for {m.resume_id} moved > 1e-9"


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
