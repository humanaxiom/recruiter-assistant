"""Corpus-integrity guard for the Phase-4a ranking-evals fixtures.

``core/tests/evals/`` holds a labelled resumes-vs-JD corpus consumed by the
``ranking-evals`` merge-blocking gate (``.claude/agents/ranking-evals.md``)
and, once Phase 4c lands ``src.pipeline.matching.orchestrator``, by
``core/tests/evals/run_evals.py``. This module does NOT exercise the ranking
algorithm (there is none yet) -- it proves the corpus itself is well-formed
and stays well-formed as it's edited:

* every resume fixture validates against ``ResumeParsed``,
* the JD fixture validates against ``JDExtracted``,
* ``thresholds.toml`` parses and carries the sections/keys both the gate and
  ``run_evals.py`` read, and its ``fuzz_threshold`` never drifts from
  ``MatchWeights.evidence_verify_fuzz`` (the single source of truth),
* the label manifest (``labels.json``) and the fixture files agree on the set
  of resume ids in both directions,
* every ``evidence_chunk_ids`` / bullet ``chunk_id`` reference resolves to a
  real chunk in that same resume (no dangling citations),
* the adversarial keyword-stuffer fixture's claimed skills have NO textual
  support in their cited chunks (the fabrication trap a correct evidence
  verifier must catch), while every non-adversarial fixture's claimed
  JD-relevant skills DO have textual support (so the trap is genuinely a
  trap, not just an artifact of sparse fixtures),
* no fixture contains any email address, phone number or *candidate name*
  outside the synthetic markers -- every fixture uses ``*@example.test`` /
  ``555-01xx`` and a ``candidate.name`` drawn from a fixed, obviously-fake
  allowlist. See "PII scanner scope" below for what this does and does NOT
  claim: it is narrower than "nothing resembling real PII".

--- Phase-4a strengthening (adequacy-review round 1) additions ---

* ``[adversarial].must_not_surface_in_topk`` / ``[evidence].min_completeness_
  in_topk`` parse in thresholds.toml, and every 'weak'/'adversarial' fixture
  (including r09, the keyword-stuffer) is flagged
  ``must_not_surface_in_topk: true`` in labels.json,
* a strong-skills-but-stale-experience fixture (r10) whose recency-relevant
  skills sit in the mid/old MatchWeights recency bucket while still being
  textually grounded,
* a strong-skills fixture with a non-CS bachelor's (r11), exercising
  ``MatchWeights.education_partial``,
* the r04 borderline fixture now carries a non-empty ``cover_letter_chunks``,
  making the 0.1 motivation weight live,
* every labelled resume carries an ``expected_rank_band`` consistent with its
  tag, and the per-tag bands are strictly ordered/non-overlapping,
* a self-dox fixture (r12) whose own name is inside a structured
  ``experience[].bullets[].text`` AND in ``candidate.name`` -- the positive
  control for the ADR-007 N1-allowed-vs-embedding-leak distinction,
* an overqualified fixture (r13, ``total_years_experience`` >=
  ``MatchWeights.overqual_ratio`` x the JD's ``min_years_experience``),
* ``gold_evidence`` anchors (skill -> exact cited-chunk substring) for two
  strong fixtures, feeding 4c's future fuzz-boundary test.

--- Phase-4a strengthening (round 2: GAP 1 + GAP 2) additions ---

GAP 1 -- ``expected_rank_band`` feasibility. Round 1 shipped a bug: the
'strong' band was the STATIC range ``[1, 3]`` while the corpus had 5 'strong'
fixtures -- no correct ranker could ever satisfy that (5 candidates cannot
fit in a 3-wide window). ``TAG_RANK_BANDS`` below is now computed FROM the
corpus's actual tier population counts, so it is feasible by construction.
``test_expected_rank_bands_fit_tier_populations`` independently re-derives
feasibility from ``labels.json``'s tag counts on every run (it does NOT just
trust ``TAG_RANK_BANDS``) via a Hall's-condition check over every contiguous
rank window. This is the guard that would have caught the round-1 bug. (Round
4 split the shared weak/adversarial band -- see TAG_RANK_BANDS.)

GAP 2 -- matched-pair ``ordering_controls``. r11 (education), r13 (overqual),
and r04 (motivation) each only moved score WITHIN a tier (~0.03), so a
ranker that ignored those dimensions still passed every tier-level
invariant. r14/r15/r16 are TWIN fixtures of r11/r13/r04 respectively --
each identical to its partner in every scoring-relevant field EXCEPT the one
target dimension, so that dimension is the SOLE differentiator:

* r14 (education twin of r11): identical skills/chunks/experience/years,
  CS/allowed-field bachelor instead of Mechanical Engineering.
* r15 (overqual twin of r13): identical skills/chunks/experience/education,
  ``total_years_experience`` = 6 (ratio 1.2, not overqualified) instead of
  14 (ratio 2.8, overqualified).
* r16 (motivation twin of r04): identical skills/chunks/experience/education/
  years, NO ``cover_letter_chunks`` instead of a populated one.

``labels.json["ordering_controls"]`` records, per pair, which member a
correct ranker must place strictly higher and why. The per-pair
``test_r1{4,5,6}_*_twin_is_identical_to_*_except_*`` tests below are the
Phase-4a-side guard: they assert the twins are genuinely "identical except
X", which is what makes the eventual Phase-4c ``rank(higher) < rank(lower)``
assertion trustworthy rather than confounded. The ordering assertion itself
needs the live ranker and is NOT implemented here.

--- Phase-4a FALSIFIABILITY hardening (round 3: A-H) ---

Three opus-tier gate audits proved the merged corpus could NOT fail a bad 4c
engine: every finding below was demonstrated by a mutation that left the
suite green. Each is now guarded:

* **A** ``precision_at_k`` is PINNED to the exact contract (``k = 5``,
  ``min_precision = 1.0``). ``min_precision = 0.8`` at k=5 tolerated exactly
  one bad entry in the top-5 -- i.e. an engine that ranked r09 (the
  keyword-stuffer the metric exists to catch) at rank 5 still PASSED -- and
  it contradicted both the file's own prose and
  ``[adversarial].must_not_surface_in_topk``. A range check alone
  (``0 < min_precision <= 1``) let a ``0.8 -> 0.2`` mutation stay green.
* **B** the adversarial bait's POTENCY is asserted: r09 must be structurally
  top-tier on every non-evidence signal (claims every required + nice-to-have
  skill, every ``years`` clears the JD's ``min_years``, every
  ``last_used_year`` sits in the ``recency_recent`` bucket, and its total
  years clears the JD minimum without tripping ``overqual_ratio``). Only
  EVIDENCE verification may reject it. A defanged bait (single ungrounded
  ``Python``, ``years: 1``, ``last_used_year: 2005``) is rejected by any
  scorer, so the fabrication trap silently stops trapping.
* **C** ``thresholds.toml``'s key set is a CONTRACT between three consumers
  (the toml, ``.claude/agents/ranking-evals.md``, and
  ``tests/evals/run_evals.py``); ``[ordering_controls]`` now exists as a
  real, machine-readable key (it was prose in ``labels.json`` only, so
  nothing forced 4c to implement the matched-pair assertion) and is pinned
  against ``labels.json`` so the two cannot drift.
* **D** the r11/r14 education twins now share a BYTE-IDENTICAL chunk list.
  Relaxing to *cited*-chunk equality left the degree-narrating ``c_005``
  differing between them -- and every chunk is embedded and searched by
  stage-3 evidence retrieval, so r14 could out-score r11 through the
  evidence path (0.3) even with ``education_partial`` a total no-op, which is
  exactly what the pair exists to detect.
* **E** the PII scanners are ALLOWLISTS, not blocklists: every email-shaped
  match in a fixture must be ``@example.test`` and every phone-shaped match
  must normalise into the reserved-for-fiction ``555-01xx`` block. The old
  6-domain blocklist passed ``<user>@<real-university>.ca`` /
  ``<name>@<real-employer>.com`` (i.e. every corporate/university/ISP domain
  -- the exact surface a real person's data would enter through), and the old
  phone regex missed ``(604) 555-1212`` and bare 10-digit numbers. NB the
  placeholders: round-4 finding B4 was that documenting this fix with LIVE
  addresses put a real institutional address and a plausible corporate one
  into the guard's own source file -- the one file whose invariant bans
  exactly those strings. r12 additionally pins the
  candidate name in ``chunks[].text`` (the §7-F1 embed-boundary surface that
  is SCRUBBED), not only in ``experience[].bullets[].text`` (the ADR-007 N1
  surface that is PERMITTED at rest); and r17 is the format-divergent
  (ADR-007 F1-R) positive control -- name-in-``summary``, line-broken name,
  reflowed phone, bare email local-part.
* **F** determinism: ranking-ORDER stability is the zero-tolerance invariant;
  ``score_final`` gets an epsilon. ``max_score_delta = 0.0`` would flake or
  lie (no ``seed`` is passed to Ollama, and the Redis embed cache makes a
  warm second run compare the cache to itself, not the model to itself).
* **G** ``negative_evidence``: at least one FABRICATED quote per relevant
  fixture that MUST fail verification below ``fuzz_threshold``. Every
  ``gold_evidence`` anchor is an exact substring (verifies at 1.0), so
  ``verification_rate_min = 1.0`` was satisfiable by a verifier that returns
  ``True`` unconditionally.
* **H** ``run_evals.py`` is EXECUTED here (its "cannot go green before 4c"
  honesty is now gated, not merely structural) and its ``load_corpus()``
  path-join is confined to ``FIXTURES_DIR``.

--- Phase-4a FALSIFIABILITY hardening (round 4: B1-B6 / N1-N6) ---

Round 3 shipped the guards; a second three-gate audit proved several of them
still asserted things nothing checked. Round 4:

* **B1** the ``[adversarial]`` arm was INERT. r09 held a sub-bachelor
  ``Diploma, General Studies``, which fails the JD's ``min_level:
  bachelors`` on its own -- so a MatchWeights-faithful engine with a
  **no-op evidence verifier** still dropped r09 to rank 8 (outside the k=5
  window) and PASSED ``must_not_surface_in_topk`` + ``precision@5 = 1.0``.
  The education sub-score alone (0.10 x 0.6 = 0.06 of ``score_final``)
  exceeded the 0.0485 gap to the top-5 cutoff. The round-3 potency test
  asserted 3 of MatchWeights' 5 structured sub-scores (skill/experience/
  seniority) and OMITTED the two on which r09 was weak (education, vector),
  while its own docstring, ``thresholds.toml`` and the agent doc all claimed
  "only the EVIDENCE verifier may reject it". r09 now holds a JD-allowed
  ``BSc Computer Science``, and the potency test asserts education AND the
  vector sub-score's embedded input. With the bait repaired, the no-op-
  verifier engine puts r09 at rank 2 -> precision@5 = 0.80 -> the adversarial
  arm FAILS, which is the whole point of the fixture.
* **B2** the "three-way key-set contract" was enforced in ZERO directions
  against the two consumer DOCS: only ``thresholds.toml`` <-> the
  ``_THRESHOLD_KEYS`` literal in this file was machine-checked, and the test
  the comments named (``test_every_threshold_key_is_enumerated_by_both_
  consumers``) did not exist. Deleting the ``[ordering_controls]`` block from
  BOTH consumer docs left the suite green. That test now exists and reads
  both docs.
* **B3** ``[evidence].min_completeness_in_topk`` was the last unpinned
  numeric threshold (range-checked only); ``1.0 -> 0.01`` stayed green. It is
  the key whose job is to stop ``verification_rate_min = 1.0`` passing
  vacuously, so it is now pinned exactly.
* **B5** the PII scanner enumerated fixture FILENAMES (``resumes/*.json`` +
  a hardcoded JD path + labels.json), so a NEW non-resume fixture was never
  scanned -- and 4b/4d are about to add exactly that. It now globs the
  fixtures directory.
* **B6** the email scanner required ``local@domain`` CONTIGUOUS, while the
  corpus's whole thesis (r17 / ADR-007 F1-R) is that FORMAT-DIVERGENT
  identifiers are the leak class that matters: ``<user> @<domain>`` with one
  space was not flagged. Whitespace is now allowed around the ``@`` and
  stripped before the allowlist check, and ``_PHONE_SHAPED_RE``'s separator
  class grew the unicode dashes and ``/`` a real PDF paste carries.

PII scanner scope (accepted residuals, round 4 / finding N6) -- what these
scanners do NOT claim:

* ``FAKE_NAMES`` constrains ``candidate.name`` only. A real THIRD PARTY's
  name in free text ("Worked with <real person> at <real company>") passes
  every scanner. Closing that needs NER; it is deliberately out of scope.
* The threat model is an ACCIDENTAL paste of real data (e.g. from the hris
  source repo), not a malicious insider. Deliberate-evasion classes --
  homoglyph domains, ``[at]``/``(dot)`` obfuscation, base64 -- are accepted
  residuals.
* The phone scanner is NANP-shaped. ``+44 20 7946 0958`` /
  ``+33 6 12 34 56 78`` are not flagged.
* ``_ALLOWED_EMAIL_DOMAIN`` is checked with ``endswith("@example.test")``, so
  a legitimate SUBDOMAIN (``casey@mail.example.test``) is flagged. Over-strict
  and fails safe; relax deliberately if a fixture ever needs a subdomain.

This test suite is expected to PASS today -- it needs only the Phase 2
schemas, which are already merged.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
from difflib import SequenceMatcher
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from src.schemas.jobs import JDExtracted
from src.schemas.matching import DEFAULT_WEIGHTS
from src.schemas.resumes import ResumeParsed

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
FIXTURES_DIR = EVALS_DIR / "fixtures"
RESUMES_DIR = FIXTURES_DIR / "resumes"
LABELS_PATH = FIXTURES_DIR / "labels.json"
THRESHOLDS_PATH = EVALS_DIR / "thresholds.toml"

# The two OTHER consumers of thresholds.toml's key set (finding B2). The agent
# doc lives outside `core/`, so its existence is asserted before it is parsed --
# a bad path would otherwise make the contract test pass VACUOUSLY.
REPO_ROOT = EVALS_DIR.parents[2]
AGENT_DOC_PATH = REPO_ROOT / ".claude" / "agents" / "ranking-evals.md"
RUN_EVALS_PATH = EVALS_DIR / "run_evals.py"

ALLOWED_TAGS = {"strong", "borderline", "weak", "adversarial"}

# The obviously-synthetic name allowlist -- every fixture's candidate.name
# must be exactly one of these. New fixtures must add their fake name here;
# this is the "falsifiable" guard against an accidentally-real name sneaking
# into the corpus.
FAKE_NAMES = {
    "Casey Rivera",
    "Jordan Kim",
    "Avery Thompson",
    "Morgan Lee",
    "Taylor Reed",
    "Drew Patel",
    "Alex Nguyen",
    "Riley Chen",
    "Sam Ortiz",
    "Jamie Okafor",
    "Skyler Brooks",
    "Reese Dawson",
    "Quinn Delgado",
    "Devon Ashworth",
    "Cameron Whitfield",
    "Rowan Castillo",
    "Harper Nakamura",
}

_EMAIL_RE = re.compile(r"^[a-z]+\.[a-z]+@example\.test$")
# The reserved-for-fiction NANP block is `(XXX) 555-0100`..`555-0199`: the
# AREA code is irrelevant to the reservation, so an optional area-code prefix
# is allowed (r17 carries one, because its ADR-007 F1-R "reflowed phone"
# control needs whitespace BETWEEN groups to diverge on). The 555-01xx
# exchange+line is still pinned exactly.
_PHONE_RE = re.compile(r"^(?:\d{3}[ -])?555[ -]01\d{2}$")

# ── PII scanners: ALLOWLISTS, not blocklists (findings E1 / E2 / B6 / N5) ──
#
# The merged corpus scanned for six consumer email domains and for a 3-3-4
# phone shape behind a dead lookbehind. Planting a real institutional address
# (`<user>@<real-university>.ca`) and a plausible corporate one
# (`<name>@<real-employer>.com`) in chunk free text left all 226 corpus tests
# green -- every corporate/university/ISP domain passed the blocklist, which is
# the exact surface through which a real person's data from the hris source
# repo would enter -- as did `(604) 555-1212` and a bare 10-digit `6045551212`.
# A blocklist can never enumerate that surface, so both scanners are INVERTED:
# every email-shaped / phone-shaped match found anywhere in a fixture must be
# one of the synthetic markers.
#
# Round-4 finding B6: the email pattern required `local@domain` CONTIGUOUS,
# which is exactly backwards for a corpus whose thesis (r17 / ADR-007 F1-R) is
# that FORMAT-DIVERGENT identifiers are the leak class that matters -- a leak
# reflowed by a PDF extractor into `<user> @<domain>` was not flagged at all.
# Whitespace (including a line break) is now allowed around the `@` and
# stripped out of the match before the allowlist check, the same way
# `_PHONE_SHAPED_RE` already tolerated separators.
_EMAIL_SHAPED_RE = re.compile(r"[A-Za-z0-9._%+-]+\s*@\s*[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# NOTE (accepted, deliberate): this is `endswith`, so a legitimate SUBDOMAIN of
# the reserved domain (`casey@mail.example.test`) is FLAGGED. Over-strict, and
# it fails safe; relax deliberately if a fixture ever needs a subdomain.
_ALLOWED_EMAIL_DOMAIN = "@example.test"

# Separator class for phone-shaped matches (round-4 finding N5). ASCII space /
# dot / hyphen is not enough: a number pasted out of a real PDF plausibly
# carries a unicode hyphen or dash (U+2010..U+2015) or a slash, and
# `604–555–1212` must be caught exactly like `604-555-1212`.
_PHONE_SEP = r"[\s./‐‑‒–—―-]"

# Anything phone-shaped: an optional country code, an area code (bare or
# parenthesised), then a 3-4 split with ANY (incl. empty) separator -- so a
# bare 10-digit run matches too -- OR the local 7-digit form the fixtures use.
_PHONE_SHAPED_RE = re.compile(
    rf"(?<!\d)(?:\+?1{_PHONE_SEP}*)?(?:\(\d{{3}}\)|\d{{3}}){_PHONE_SEP}*\d{{3}}"
    rf"{_PHONE_SEP}*\d{{4}}(?!\d)"
    rf"|(?<!\d)\d{{3}}{_PHONE_SEP}\d{{4}}(?!\d)"
)
# ...and with every non-digit stripped, it must land in the reserved 555-01xx
# block, with or without an (any) area code / leading country code.
_ALLOWED_PHONE_DIGITS_RE = re.compile(r"(?:1)?(?:\d{3})?55501\d{2}")

# JD-relevant skill name -> a short lowercase substring that must appear in a
# cited chunk's text as proof the claim is textually grounded. Covers every
# required_skill AND nice_to_have_skill name used anywhere in the corpus
# (see fixtures/jd_backend_data_engineer.json). Keyed by skill.name.lower().
SKILL_EVIDENCE_MARKERS: dict[str, str] = {
    "python": "python",
    "postgresql": "postgresql",
    "apache airflow": "airflow",
    "docker": "docker",
    "rest api design": "rest api",
    "kubernetes": "kubernetes",
    "kafka": "kafka",
    "terraform": "terraform",
}

# The fixture corpus's baseline "today" for recency-bucket math -- every
# fixture's "recent" skills are stamped last_used_year=2026 (see r01..r13);
# r10's stale skills deliberately sit years behind this.
CURRENT_YEAR = 2026


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        result: dict[str, Any] = json.load(fh)
    return result


def _load_thresholds() -> dict[str, Any]:
    with THRESHOLDS_PATH.open("rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)
    return data


def _string_values(node: object) -> list[str]:
    """Every string anywhere in a decoded JSON document."""
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            out.extend(_string_values(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_string_values(value))
    return out


def _scan_texts(path: Path) -> list[str]:
    """Every text surface of a fixture the PII scanners must cover: the raw
    file source AND every decoded string value.

    Both are needed. The raw source catches anything outside a JSON string
    (and is what a `grep` of the repo would see); the decoded values are the
    only place a JSON-escaped newline (`\\n` in source) is a REAL newline, so
    a format-divergent leak like a line-broken phone number is invisible to a
    raw-source scan alone (see r17, the ADR-007 F1-R control)."""
    raw = path.read_text(encoding="utf-8")
    texts = [raw]
    texts.extend(_string_values(json.loads(raw)))
    return texts


def _best_partial_ratio(needle: str, haystack: str) -> float:
    """A conservative, stdlib-only stand-in for rapidfuzz's ``partial_ratio``
    (which the 4c verifier will use against ``evidence_verify_fuzz``): the max
    SequenceMatcher ratio over every ``len(needle)``-wide window of the
    haystack. Case-insensitive.

    DIRECTION OF THE APPROXIMATION (round-4 finding N3 -- record it, because
    whoever tightens the anchors later needs it). It differs from rapidfuzz on
    two independent axes, and they point OPPOSITE ways:

    * window-vs-full-string: taking the best ``len(needle)``-wide window is
      LENIENT relative to a plain full-string ratio. That leniency is
      deliberate -- a fabricated quote that cannot clear ``fuzz_threshold``
      even under best-window matching will not be verified by any reasonable
      verifier, so the negative-evidence guard is not trivially satisfiable.
    * similarity measure: ``SequenceMatcher.ratio()`` is Ratcliff-Obershelp
      (``2*M/T`` over matching blocks), which is always **<=** rapidfuzz's
      LCS/indel-based ratio. On this axis the stand-in is STRICTER, i.e. it
      scores a near-miss LOWER than rapidfuzz would.

    Net: the corpus's negative anchors were cross-checked against real
    rapidfuzz and clear the bar under BOTH measures (worst negative scores
    0.454 under a rapidfuzz-equivalent LCS ratio vs 0.349 under this stand-in;
    0.396 of headroom to the 0.85 threshold, minimum gold-vs-negative margin
    0.392). If a future anchor is tightened until its margin is thin, re-check
    it against real rapidfuzz ``partial_ratio`` -- do NOT trust this stand-in
    alone at the boundary."""
    a, b = needle.lower(), haystack.lower()
    if len(a) > len(b):
        a, b = b, a
    if not a:
        return 0.0
    return max(
        SequenceMatcher(None, a, b[i : i + len(a)]).ratio()
        for i in range(len(b) - len(a) + 1)
    )


def _import_run_evals() -> ModuleType:
    """Import ``tests/evals/run_evals.py`` by path.

    ``tests/evals`` is not a package (no ``__init__.py``) -- it is a script
    directory the ranking-evals gate invokes directly -- so it is loaded from
    its file location rather than imported by dotted name."""
    name = "run_evals_under_test"
    path = EVALS_DIR / "run_evals.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # run_evals uses `from __future__ import annotations` + @dataclass, and
    # dataclasses resolves string annotations via sys.modules[cls.__module__]
    # -- so the module must be registered BEFORE exec_module, or the class
    # bodies raise AttributeError on a None module.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_labels() -> dict[str, Any]:
    return _load_json(LABELS_PATH)


def _load_resume_raw(resume_id: str) -> dict[str, Any]:
    """Raw (un-validated) JSON dict for a resume fixture, keyed by manifest
    id. Used by the round-2 "identical except X" twin-pair integrity tests,
    which need to compare fixture JSON field-for-field rather than through
    ResumeParsed (which drops/normalises some data)."""
    labels = _load_labels()
    fixture = labels["resumes"][resume_id]["fixture"]
    return _load_json(FIXTURES_DIR / fixture)


def _resume_ids_from_labels() -> list[str]:
    return sorted(_load_labels()["resumes"].keys())


def _resume_ids_from_fixture_files() -> list[str]:
    return sorted(p.stem for p in RESUMES_DIR.glob("*.json"))


def _resume_ids_with_gold_evidence() -> list[str]:
    labels = _load_labels()
    return sorted(
        resume_id
        for resume_id, entry in labels["resumes"].items()
        if entry.get("gold_evidence")
    )


def _ordering_controls() -> list[dict[str, str]]:
    labels = _load_labels()
    controls: list[dict[str, str]] = labels.get("ordering_controls", [])
    return controls


def _tag_populations() -> dict[str, int]:
    """Count fixtures per tag, fresh from labels.json every call. Used both
    to compute the canonical TAG_RANK_BANDS below AND, independently, by
    test_expected_rank_bands_fit_tier_populations to verify those bands are
    actually feasible -- the two must never be conflated into "the test just
    checks itself"."""
    labels = _load_labels()
    counts: dict[str, int] = {tag: 0 for tag in ALLOWED_TAGS}
    for entry in labels["resumes"].values():
        counts[entry["tag"]] += 1
    return counts


# ── Directory / manifest sanity ──────────────────────────────────────────


def test_fixtures_dir_exists() -> None:
    assert FIXTURES_DIR.is_dir()
    assert RESUMES_DIR.is_dir()


def test_labels_manifest_exists_and_parses() -> None:
    labels = _load_labels()
    assert "job" in labels
    assert "resumes" in labels
    assert isinstance(labels["resumes"], dict)
    assert len(labels["resumes"]) >= 8  # ~8-10 per the corpus spec


def test_jd_fixture_referenced_by_manifest_exists() -> None:
    labels = _load_labels()
    jd_path = FIXTURES_DIR / labels["job"]["fixture"]
    assert jd_path.is_file()


# ── JD fixture validates against JDExtracted ─────────────────────────────


def test_jd_fixture_validates_against_jdextracted() -> None:
    labels = _load_labels()
    jd_path = FIXTURES_DIR / labels["job"]["fixture"]
    jd = JDExtracted.model_validate(_load_json(jd_path))
    assert jd.title
    assert len(jd.required_skills) >= 3, "a realistic role needs several must-haves"


def test_jd_fixture_has_min_years_per_required_skill() -> None:
    labels = _load_labels()
    jd_path = FIXTURES_DIR / labels["job"]["fixture"]
    jd = JDExtracted.model_validate(_load_json(jd_path))
    assert all(s.min_years is not None for s in jd.required_skills)


def test_jd_fixture_has_nice_to_have_skills() -> None:
    labels = _load_labels()
    jd_path = FIXTURES_DIR / labels["job"]["fixture"]
    jd = JDExtracted.model_validate(_load_json(jd_path))
    assert len(jd.nice_to_have_skills) >= 1


def test_jd_fixture_has_education_requirement() -> None:
    labels = _load_labels()
    jd_path = FIXTURES_DIR / labels["job"]["fixture"]
    jd = JDExtracted.model_validate(_load_json(jd_path))
    assert jd.education is not None
    assert jd.education.min_level is not None


# ── thresholds.toml shape ────────────────────────────────────────────────


def test_thresholds_toml_parses() -> None:
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert isinstance(data, dict)


# The FULL key contract. thresholds.toml, .claude/agents/ranking-evals.md and
# tests/evals/run_evals.py's docstring must all enumerate exactly these keys
# (finding C1: the toml grew `[adversarial]` and `min_completeness_in_topk`
# and neither consumer noticed -- a 4c coder wiring run_evals.py from its own
# docstring would have built a gate a pure-vector ranker passes). Adding a key
# here without updating BOTH consumer docs is the drift this list exists to
# stop; `test_every_threshold_key_is_enumerated_by_both_consumers` enforces it.
_THRESHOLD_KEYS: list[tuple[str, str]] = [
    ("precision_at_k", "k"),
    ("precision_at_k", "min_precision"),
    ("evidence", "verification_rate_min"),
    ("evidence", "fuzz_threshold"),
    ("evidence", "min_completeness_in_topk"),
    ("evidence", "negative_evidence_must_fail"),
    ("adversarial", "must_not_surface_in_topk"),
    ("ordering_controls", "enforce"),
    ("ordering_controls", "pairs"),
    ("pii", "leak_check"),
    ("pii", "allow_structured_fields"),
    ("pii", "structured_fields_surface"),
    ("pii", "embedding_input_pii_free"),
    ("pii", "exported_output_pii_free"),
    ("determinism", "temperature"),
    ("determinism", "max_rank_delta"),
    ("determinism", "max_score_delta"),
]


@pytest.mark.parametrize("section, key", _THRESHOLD_KEYS)
def test_thresholds_toml_has_required_section_key(section: str, key: str) -> None:
    data = _load_thresholds()
    assert section in data, f"missing [{section}] section in thresholds.toml"
    assert key in data[section], f"missing {section}.{key} in thresholds.toml"


def test_thresholds_toml_has_no_key_outside_the_enumerated_contract() -> None:
    """The reverse direction of the same contract: a key that exists in the
    toml but is not in ``_THRESHOLD_KEYS`` is a key neither consumer doc
    knows about. Fail here and add it in all three places at once."""
    data = _load_thresholds()
    actual = {
        (section, key)
        for section, body in data.items()
        if isinstance(body, dict)
        for key in body
    }
    assert actual == set(_THRESHOLD_KEYS), (
        f"thresholds.toml key set drifted from the enumerated contract: "
        f"extra={sorted(actual - set(_THRESHOLD_KEYS))} "
        f"missing={sorted(set(_THRESHOLD_KEYS) - actual)} -- update "
        f"thresholds.toml, .claude/agents/ranking-evals.md AND "
        f"tests/evals/run_evals.py together"
    )


def _threshold_keys_enumerated_by_the_agent_doc() -> set[tuple[str, str]]:
    """Every ``(section, key)`` the ranking-evals agent doc's threshold table
    enumerates. The table gives each key its OWN row, formatted
    ``| `[section] key` | what it gates |`` -- one row per key precisely so
    this parse can be an exact set comparison rather than a substring sniff."""
    text = AGENT_DOC_PATH.read_text(encoding="utf-8")
    found: set[tuple[str, str]] = set()
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        for section, key in re.findall(r"`\[([a-z_]+)\]\s+([a-z_]+)`", line):
            found.add((section, key))
    return found


def _threshold_keys_enumerated_by_run_evals() -> set[tuple[str, str]]:
    """Every ``(section, key)`` ``run_evals.py``'s module docstring
    enumerates, parsed out of its "Computes, against ..." block: a section is
    a line indented 2 spaces holding ``[name]``; a key is a line indented
    EXACTLY 4 spaces starting with the key token (prose continuation lines are
    indented far deeper, so they cannot be mistaken for keys)."""
    doc = _import_run_evals().__doc__ or ""
    found: set[tuple[str, str]] = set()
    section: str | None = None
    started = False
    for line in doc.splitlines():
        if line.startswith("Computes, against"):
            started = True
            continue
        if not started:
            continue
        section_match = re.fullmatch(r" {2}\[([a-z_]+)\]", line.rstrip())
        if section_match is not None:
            section = section_match.group(1)
            continue
        key_match = re.match(r" {4}([a-z_]+)\b", line)
        if key_match is not None and section is not None:
            found.add((section, key_match.group(1)))
    return found


def test_every_threshold_key_is_enumerated_by_both_consumers() -> None:
    """Finding B2. ``thresholds.toml``, ``.claude/agents/ranking-evals.md``
    and ``tests/evals/run_evals.py``'s docstring all assert -- in prose -- that
    their key sets are a THREE-WAY contract that cannot drift. It was not
    enforced in ANY direction against the two consumer DOCS: only
    ``thresholds.toml`` <-> ``_THRESHOLD_KEYS`` (a literal in this very file)
    was machine-checked, and the test the comments named --
    ``test_every_threshold_key_is_enumerated_by_both_consumers`` -- did not
    exist anywhere in the repo.

    Mutations that stayed GREEN before this test existed: deleting the
    ``[ordering_controls]`` block from run_evals.py's docstring AND the row
    from the agent doc's table; deleting ``[adversarial]`` +
    ``min_completeness_in_topk`` from the docstring; adding a new toml key to
    both the toml and ``_THRESHOLD_KEYS`` with both consumer docs left stale.
    A 4c coder wiring the harness from a stale docstring would have built a
    gate a naive pure-vector ranker passes.

    The comparison is set EQUALITY in both directions, so a doc that grows a
    key the toml does not have fails too.
    """
    assert AGENT_DOC_PATH.is_file(), (
        f"the ranking-evals agent doc is missing at {AGENT_DOC_PATH} -- without "
        f"this existence check the contract below would pass VACUOUSLY on a bad "
        f"path (an empty doc enumerates nothing, and so would match an empty "
        f"expectation)"
    )
    assert RUN_EVALS_PATH.is_file(), f"run_evals.py is missing at {RUN_EVALS_PATH}"

    expected = set(_THRESHOLD_KEYS)
    assert expected, "the contract must not be empty (guards against a vacuous pass)"

    agent_doc = _threshold_keys_enumerated_by_the_agent_doc()
    assert agent_doc == expected, (
        f".claude/agents/ranking-evals.md's threshold table drifted from "
        f"thresholds.toml: missing={sorted(expected - agent_doc)} "
        f"extra={sorted(agent_doc - expected)} -- every key needs its own "
        f"`| `[section] key` |` row"
    )

    run_evals_doc = _threshold_keys_enumerated_by_run_evals()
    assert run_evals_doc == expected, (
        f"tests/evals/run_evals.py's docstring drifted from thresholds.toml: "
        f"missing={sorted(expected - run_evals_doc)} "
        f"extra={sorted(run_evals_doc - expected)}"
    )


# precision@k is an EXACT contract, not a range (finding A1/A2). At k=5,
# min_precision=0.8 tolerates exactly one bad entry in the top-5 -- so an
# engine that ranks r09 (the keyword-stuffer this metric exists to catch) at
# rank 5 PASSES; with 11 good / 6 bad fixtures even a uniformly random ranker
# clears 0.8 roughly half the time. It also contradicts the toml's own prose
# ("none may be 'weak' or 'adversarial'" == 1.0) and
# [adversarial].must_not_surface_in_topk. The old range check
# (0.0 < min_precision <= 1.0) let a 0.8 -> 0.2 mutation stay green, which
# CLAUDE.md forbids. CHANGING EITHER NUMBER BELOW NEEDS A HUMAN.
_PRECISION_K = 5
_MIN_PRECISION = 1.0


def test_thresholds_precision_at_k_is_pinned_to_its_exact_contract() -> None:
    data = _load_thresholds()
    assert data["precision_at_k"]["k"] == _PRECISION_K, (
        "k is pinned: the corpus's tier populations and expected_rank_bands "
        "are built around a 5-wide shortlist window"
    )
    assert data["precision_at_k"]["min_precision"] == _MIN_PRECISION, (
        "min_precision must be exactly 1.0 -- any lower value admits a "
        "'weak'/'adversarial' fixture into the top-k and directly contradicts "
        "[adversarial].must_not_surface_in_topk"
    )


def test_thresholds_evidence_verification_rate_is_perfect() -> None:
    """Anti-fabrication invariant: no threshold below 1.0 is acceptable."""
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert data["evidence"]["verification_rate_min"] == 1.0


def test_thresholds_fuzz_threshold_matches_matchweights_single_source_of_truth() -> (
    None
):
    """thresholds.toml documents fuzz_threshold as a copy of
    MatchWeights.evidence_verify_fuzz for readability without importing
    src.* -- but the two values must never diverge."""
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert data["evidence"]["fuzz_threshold"] == DEFAULT_WEIGHTS.evidence_verify_fuzz


# Determinism is a two-part contract (finding F1). The zero-tolerance half is
# ranking-ORDER stability -- which is what ranking-evals.md actually names --
# NOT bit-identical floats: src/pipeline/llm/client.py passes only
# temperature/num_predict to Ollama (no `seed`), and greedy decode is not
# bit-stable across batch/kv-cache splits, so `max_score_delta = 0.0` would
# either flake or (worse) pass vacuously, because src/pipeline/llm/cache.py
# caches embeddings by text hash and a warm-Redis second run compares the
# CACHE to itself rather than the model to itself. score_final therefore gets
# an epsilon, and "pin `seed`, and specify the cache state across the two
# runs" is carried forward as an explicit 4c requirement (docs/
# EXTRACTION_PLAN.md). CHANGING THESE NEEDS A HUMAN.
_MAX_RANK_DELTA = 0
_MAX_SCORE_DELTA = 1e-9


def test_thresholds_determinism_pins_zero_temperature_and_zero_rank_drift() -> None:
    data = _load_thresholds()
    assert data["determinism"]["temperature"] == 0.0
    assert data["determinism"]["max_rank_delta"] == _MAX_RANK_DELTA, (
        "ranking-order stability is the zero-tolerance determinism invariant: "
        "no candidate may move rank between two identical runs"
    )


def test_thresholds_determinism_score_delta_is_a_nonzero_epsilon() -> None:
    """An EXACT 0.0 float tolerance is not honourable against a seed-less
    local model (see the comment above): it is either a flake or a vacuous
    pass off the embedding cache. Pin the epsilon instead so a real drift
    (not a float ulp) still fails."""
    data = _load_thresholds()
    delta = data["determinism"]["max_score_delta"]
    assert delta == _MAX_SCORE_DELTA
    assert 0.0 < delta <= 1e-6, "score tolerance must be a tight, non-zero epsilon"


def test_thresholds_pii_leak_check_is_enabled() -> None:
    data = _load_thresholds()
    assert data["pii"]["leak_check"] is True


def test_thresholds_pii_structured_field_exemption_is_scoped_to_one_surface() -> None:
    """Finding E4. ADR-007 N1 exempts structured experience/education free
    text (e.g. a self-doxxing achievement bullet) from scrubbing ONLY in the
    outbox / at-rest payload, and states that candidate identifiers are
    separately scrubbed from ALL embeddings. The merged toml dropped that
    qualifier while declaring its keys "read literally by both consumers", so
    a 4c implementer could exempt bullet-DERIVED text from the leak scan and
    ride a real candidate name into a Neo4j vector (PIPEDA/FIPPA-relevant).
    r12's chunk c_003 is byte-identical to its bullet text, so this is
    reachable, not theoretical. The exemption must therefore be surface-
    qualified, and the two PII-free surfaces must be pinned True."""
    data = _load_thresholds()
    assert data["pii"]["allow_structured_fields"] is True
    assert data["pii"]["structured_fields_surface"] == "outbox_at_rest", (
        "the N1 exemption is scoped to the outbox/at-rest payload ONLY -- it "
        "is not a licence to skip the leak scan on embedding input or export"
    )
    assert data["pii"]["embedding_input_pii_free"] is True, (
        "embedding input must contain no name/email/phone REGARDLESS of the "
        "originating field (ADR-007 §7-F1)"
    )
    assert data["pii"]["exported_output_pii_free"] is True


def test_thresholds_adversarial_must_not_surface_in_topk_is_enabled() -> None:
    """The backstop invariant (Phase-4a strengthening item 1) must be a hard
    True -- a False/soft value would defeat the whole point of the flag."""
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert data["adversarial"]["must_not_surface_in_topk"] is True


# Finding B3: this was the LAST unpinned numeric threshold -- range-checked
# only (`0.0 < v <= 1.0`), so `1.0 -> 0.2` and `1.0 -> 0.01` both stayed
# GREEN. It is exactly the defect class of the original min_precision finding,
# left on the one key whose job is to stop `verification_rate_min = 1.0`
# passing VACUOUSLY over an empty surfaced-quote set. Every other value in the
# file is pinned exactly; so is this one now. CHANGING IT NEEDS A HUMAN.
_MIN_COMPLETENESS_IN_TOPK = 1.0


def test_thresholds_min_completeness_in_topk_is_pinned_exactly() -> None:
    data = _load_thresholds()
    value = data["evidence"]["min_completeness_in_topk"]
    assert value == _MIN_COMPLETENESS_IN_TOPK, (
        "min_completeness_in_topk must be exactly 1.0 -- it is the recall half "
        "of the evidence contract: EVERY top-k entry must carry >= 1 VERIFIED "
        "quote. Any lower value lets an engine surface a shortlist entry with "
        "zero verified evidence and still report verification_rate = 1.0 over "
        "the (empty) set of quotes it did surface"
    )
    assert 0.0 < value <= 1.0


def test_thresholds_negative_evidence_must_fail_is_enabled() -> None:
    """Finding G1. Every gold_evidence anchor is an exact substring, so it
    verifies at 1.0 -- meaning verification_rate_min = 1.0 is satisfiable
    today by a verifier that returns True unconditionally. The corpus's
    ``negative_evidence`` quotes (fabricated, must score BELOW fuzz_threshold
    against their cited chunk) are what make the anti-fabrication invariant
    falsifiable, and this key is what forces 4c to run them."""
    data = _load_thresholds()
    assert data["evidence"]["negative_evidence_must_fail"] is True


def test_thresholds_ordering_controls_are_enforced() -> None:
    """Finding C2. The matched-pair ordering controls (r14/r11 education,
    r15/r13 overqualification, r16/r04 motivation) are the corpus's most
    discriminating artifact and existed ONLY as prose in labels.json -- there
    was no thresholds.toml key for run_evals.py to read, so nothing forced 4c
    to implement the pairwise assertion at all."""
    data = _load_thresholds()
    assert data["ordering_controls"]["enforce"] is True


def test_thresholds_ordering_control_pairs_match_labels_json_exactly() -> None:
    """The toml is the machine-readable half and labels.json carries the
    rationale; they must never drift apart."""
    data = _load_thresholds()
    toml_pairs = {
        (p["dimension"], p["higher_id"], p["lower_id"])
        for p in data["ordering_controls"]["pairs"]
    }
    label_pairs = {
        (c["dimension"], c["higher_id"], c["lower_id"]) for c in _ordering_controls()
    }
    assert toml_pairs == label_pairs, (
        f"thresholds.toml [ordering_controls].pairs {sorted(toml_pairs)} != "
        f"labels.json ordering_controls {sorted(label_pairs)}"
    )
    assert {d for d, _, _ in toml_pairs} == _EXPECTED_ORDERING_CONTROL_DIMENSIONS


# ── Label manifest <-> fixture files agree in both directions ────────────


def test_every_label_manifest_id_has_a_fixture_file() -> None:
    labels = _load_labels()
    for resume_id, entry in labels["resumes"].items():
        fixture_path = FIXTURES_DIR / entry["fixture"]
        assert fixture_path.is_file(), f"{resume_id}: {fixture_path} missing"
        assert fixture_path.stem == resume_id, (
            f"{resume_id}: manifest fixture filename stem "
            f"{fixture_path.stem!r} != manifest key {resume_id!r}"
        )


def test_every_fixture_file_has_a_label_manifest_entry() -> None:
    manifest_ids = set(_resume_ids_from_labels())
    fixture_ids = set(_resume_ids_from_fixture_files())
    assert (
        fixture_ids <= manifest_ids
    ), f"orphan fixture(s) with no label: {fixture_ids - manifest_ids}"
    assert (
        manifest_ids <= fixture_ids
    ), f"manifest entries with no fixture file: {manifest_ids - fixture_ids}"


def test_all_manifest_tags_are_within_the_allowed_set() -> None:
    labels = _load_labels()
    for resume_id, entry in labels["resumes"].items():
        assert entry["tag"] in ALLOWED_TAGS, f"{resume_id}: bad tag {entry['tag']!r}"


@pytest.mark.parametrize("tag", sorted(ALLOWED_TAGS))
def test_every_tag_category_is_represented_at_least_once(tag: str) -> None:
    labels = _load_labels()
    tags_present = {entry["tag"] for entry in labels["resumes"].values()}
    assert tag in tags_present, f"corpus has no fixture tagged {tag!r}"


def test_corpus_has_at_least_one_adversarial_keyword_stuffer() -> None:
    labels = _load_labels()
    adversarial = [
        (rid, e) for rid, e in labels["resumes"].items() if e["tag"] == "adversarial"
    ]
    assert len(adversarial) >= 1
    for _, entry in adversarial:
        assert entry.get("adversarial_type"), "adversarial fixtures must document why"


# ── Every resume fixture validates against ResumeParsed ─────────────────


@pytest.mark.parametrize("resume_id", _resume_ids_from_fixture_files())
def test_resume_fixture_validates_against_resumeparsed(resume_id: str) -> None:
    path = RESUMES_DIR / f"{resume_id}.json"
    parsed = ResumeParsed.model_validate(_load_json(path))
    assert parsed.chunks, f"{resume_id}: fixture must be pre-chunked"
    assert all(
        re.fullmatch(r"c_\d{3}", c.id) for c in parsed.chunks
    ), f"{resume_id}: chunk ids must be one-based c_NNN tokens"


@pytest.mark.parametrize("resume_id", _resume_ids_from_fixture_files())
def test_resume_chunk_ids_are_contiguous_and_one_based(resume_id: str) -> None:
    """Finding N4. Contiguity was CLAIMED ("one-based c_NNN tokens") but only
    the FORMAT was enforced -- ``re.fullmatch(r"c_\\d{3}")`` is happy with
    ``c_001, c_002, c_004``. The fixtures are contiguous today, but the
    round-3 change set DELETED a mid-list chunk from two fixtures (the r11/r14
    education twins), and the next such deletion would leave a hole with no
    gate: a dangling ``evidence_chunk_ids`` reference is caught, but a chunk
    list with a hole silently changes what stage-3 evidence retrieval sees."""
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{resume_id}.json"))
    ids = [c.id for c in parsed.chunks]
    expected = [f"c_{i:03d}" for i in range(1, len(ids) + 1)]
    assert ids == expected, (
        f"{resume_id}: chunk ids {ids} are not a contiguous one-based run "
        f"{expected} -- renumber the chunks (and every evidence_chunk_ids / "
        f"bullet chunk_id citing them) rather than leaving a hole"
    )


@pytest.mark.parametrize("resume_id", _resume_ids_from_fixture_files())
def test_resume_fixture_rejects_when_mutated_to_drop_required_field(
    resume_id: str,
) -> None:
    """Mutation check: corrupting a chunk's required ``text`` field must fail
    validation -- proves the ResumeParsed check above is actually exercising
    pydantic validation, not just re-serializing already-trusted JSON."""
    payload = _load_json(RESUMES_DIR / f"{resume_id}.json")
    assert payload["chunks"], resume_id
    mutated = json.loads(json.dumps(payload))
    del mutated["chunks"][0]["text"]  # text has no default -> must fail
    with pytest.raises(ValidationError):
        ResumeParsed.model_validate(mutated)


@pytest.mark.parametrize("resume_id", _resume_ids_from_fixture_files())
def test_resume_evidence_chunk_ids_resolve_to_real_chunks(resume_id: str) -> None:
    path = RESUMES_DIR / f"{resume_id}.json"
    parsed = ResumeParsed.model_validate(_load_json(path))
    chunk_ids = {c.id for c in parsed.chunks}
    for skill in parsed.skills:
        for cid in skill.evidence_chunk_ids:
            assert (
                cid in chunk_ids
            ), f"{resume_id}: skill {skill.name!r} cites unknown chunk {cid!r}"
    for exp in parsed.experience:
        for bullet in exp.bullets:
            if bullet.chunk_id is not None:
                assert (
                    bullet.chunk_id in chunk_ids
                ), f"{resume_id}: bullet cites unknown chunk {bullet.chunk_id!r}"


# ── The fabrication trap: adversarial fixture has no real evidence ───────


def _chunk_text_by_id(parsed: ResumeParsed) -> dict[str, str]:
    return {c.id: c.text for c in parsed.chunks}


def _cited_text(parsed: ResumeParsed, chunk_ids: list[str]) -> str:
    by_id = _chunk_text_by_id(parsed)
    return " ".join(by_id[cid] for cid in chunk_ids if cid in by_id).lower()


@pytest.mark.parametrize("resume_id", _resume_ids_from_labels())
def test_jd_relevant_skill_claims_match_their_tag_evidence_property(
    resume_id: str,
) -> None:
    """The core falsifiable property of this corpus:

    - non-adversarial fixtures: every JD-relevant skill claim is textually
      grounded in its cited chunk(s) (genuine evidence, however thin) --
      fixtures with no JD-relevant skill claims at all (e.g. an honestly
      unrelated 'weak' candidate) are fine; there's simply nothing to check.
    - the adversarial fixture: claims >= 1 JD-relevant skill (it must
      actually be a keyword-stuffer to test anything), and every one of
      those claims is a bare keyword with NO textual support in its cited
      chunk(s) -- the fabrication trap a correct anti-fabrication evidence
      verifier (fuzzy-match >= MatchWeights.evidence_verify_fuzz) must
      catch, so this resume must never surface in a top-k shortlist.
    """
    labels = _load_labels()
    entry = labels["resumes"][resume_id]
    parsed = ResumeParsed.model_validate(_load_json(FIXTURES_DIR / entry["fixture"]))

    relevant = [s for s in parsed.skills if s.name.lower() in SKILL_EVIDENCE_MARKERS]
    if entry["tag"] == "adversarial":
        assert relevant, (
            f"{resume_id}: adversarial fixture must claim >= 1 JD-relevant "
            f"skill to actually be a keyword-stuffer"
        )

    for skill in relevant:
        marker = SKILL_EVIDENCE_MARKERS[skill.name.lower()]
        cited = _cited_text(parsed, skill.evidence_chunk_ids)
        grounded = marker in cited
        if entry["tag"] == "adversarial":
            assert not grounded, (
                f"{resume_id}: adversarial fixture's {skill.name!r} claim IS "
                f"textually grounded in its cited chunk -- this breaks the "
                f"fabrication trap the corpus is supposed to test. Either the "
                f"chunk text or the label is wrong."
            )
        else:
            assert grounded, (
                f"{resume_id} ({entry['tag']}): {skill.name!r} claim has NO "
                f"textual support ({marker!r} not found) in its cited chunk -- "
                f"a non-adversarial fixture's claims must be genuinely "
                f"evidenced, or a real evidence verifier would (correctly) "
                f"blank this quote too."
            )


# ── PII: synthetic markers only, no real-looking leakage ─────────────────


@pytest.mark.parametrize("resume_id", _resume_ids_from_fixture_files())
def test_candidate_name_is_from_the_fake_name_allowlist(resume_id: str) -> None:
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{resume_id}.json"))
    assert parsed.candidate.name in FAKE_NAMES, (
        f"{resume_id}: candidate name {parsed.candidate.name!r} is not in the "
        f"reviewed synthetic-name allowlist -- add it to FAKE_NAMES only if "
        f"it is obviously fake, or fix the fixture"
    )


@pytest.mark.parametrize("resume_id", _resume_ids_from_fixture_files())
def test_candidate_email_matches_the_synthetic_test_domain(resume_id: str) -> None:
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{resume_id}.json"))
    email = parsed.candidate.email
    assert email is not None
    assert _EMAIL_RE.match(email), (
        f"{resume_id}: email {email!r} must match {{first}}.{{last}}@example.test "
        f"(RFC 2606 reserved 'test' TLD -- unroutable, obviously synthetic)"
    )


@pytest.mark.parametrize("resume_id", _resume_ids_from_fixture_files())
def test_candidate_phone_is_in_the_reserved_fake_range(resume_id: str) -> None:
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{resume_id}.json"))
    phone = parsed.candidate.phone
    assert phone is not None
    assert _PHONE_RE.match(phone), (
        f"{resume_id}: phone {phone!r} must be in the NANP reserved-for-fiction "
        f"555-01xx block (555-0100 through 555-0199)"
    )


# Finding B5: this used to be `RESUMES_DIR.glob("*.json")` plus a HARDCODED
# `jd_backend_data_engineer.json` and `labels.json` -- so any NEW non-resume
# file under fixtures/ was never PII-scanned at all. Proven: adding
# `fixtures/jd_second_role.json` carrying a real email and phone left all 264
# tests green. The trigger is imminent and self-inflicted: this branch's own
# EXTRACTION_PLAN update has 4b adding an outbox-shaped fixture under
# core/tests/evals/ and 4d adding reverse-match JDs. Scope the scan by
# DIRECTORY, never by enumerated filename.
_ALL_FIXTURE_FILES = sorted(FIXTURES_DIR.rglob("*.json"))


def _offending_emails(text: str) -> list[str]:
    """Every email-shaped string in ``text`` that is NOT on the reserved
    synthetic domain. Whitespace inside the match (a PDF-reflowed
    ``<user> @<domain>``) is stripped before the allowlist check -- finding
    B6: requiring ``local@domain`` contiguous is exactly backwards for a
    corpus whose thesis is that FORMAT-DIVERGENT identifiers are the leak
    class that matters."""
    offenders: list[str] = []
    for match in _EMAIL_SHAPED_RE.finditer(text):
        normalised = re.sub(r"\s+", "", match.group()).lower()
        if not normalised.endswith(_ALLOWED_EMAIL_DOMAIN):
            offenders.append(match.group())
    return offenders


def _offending_phones(text: str) -> list[str]:
    """Every phone-shaped string in ``text`` whose digits do not normalise
    into the reserved-for-fiction 555-01xx block."""
    offenders: list[str] = []
    for match in _PHONE_SHAPED_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if not _ALLOWED_PHONE_DIGITS_RE.fullmatch(digits):
            offenders.append(match.group())
    return offenders


@pytest.mark.parametrize("path", _ALL_FIXTURE_FILES)
def test_every_email_shaped_string_in_a_fixture_is_on_the_synthetic_domain(
    path: Path,
) -> None:
    """ALLOWLIST (finding E1): not "does this look like gmail?" but "is this
    the one domain we allow?". The previous 6-domain blocklist passed a real
    institutional address and a plausible corporate one -- i.e. every
    corporate, university and ISP domain -- which is precisely the surface
    through which a real person's data (e.g. copied from the hris source repo)
    would enter the corpus."""
    for text in _scan_texts(path):
        offenders = _offending_emails(text)
        assert not offenders, (
            f"{path.name}: email-shaped string(s) {offenders!r} are not on the "
            f"reserved synthetic domain {_ALLOWED_EMAIL_DOMAIN!r} (RFC 2606 "
            f"'test' TLD). Every email in the corpus must be synthetic -- "
            f"no real/plausible domain may appear, anywhere in the file."
        )


@pytest.mark.parametrize("path", _ALL_FIXTURE_FILES)
def test_every_phone_shaped_string_in_a_fixture_is_in_the_reserved_fake_range(
    path: Path,
) -> None:
    """ALLOWLIST (finding E2): every phone-shaped match -- `604-555-1212`,
    `(604) 555-1212`, a bare 10-digit `6045551212`, or the local 7-digit form
    -- must normalise into the NANP reserved-for-fiction 555-01xx block. The
    previous scanner missed the parenthesised and bare-digit forms outright
    (the `(?<!555-01)` lookbehind was dead code against a 3-3-4 match, and
    would have false-positived on a legitimate `604-555-0101`)."""
    for text in _scan_texts(path):
        offenders = _offending_phones(text)
        assert not offenders, (
            f"{path.name}: phone-shaped string(s) {offenders!r} do not "
            f"normalise into the reserved-for-fiction 555-01xx block. Every "
            f"phone number in the corpus must be synthetic."
        )


# ── The PII scanners are themselves gated (findings B6 / N5) ──────────────
#
# A scanner nobody tested is a scanner nobody can trust: the round-3 email
# regex demanded `local@domain` CONTIGUOUS, so `<user> @<domain>.ca` -- one
# space, the single most likely artifact of a PDF text extractor, and the very
# leak SHAPE r17/ADR-007-F1-R exists to model -- sailed straight through. The
# `\n`-split case was caught only INCIDENTALLY (the raw-source pass sees the
# literal `n` of the JSON `\n` escape as a local-part), i.e. by luck.
#
# Every probe below uses an RFC-2606 reserved, non-resolving domain
# (`.invalid`) or the reserved-for-fiction NANP block: the guard's own source
# file must not carry the strings its invariant bans (finding B4).
_LEAKY_EMAIL_PROBES = [
    "notreal.person@nonexistent-employer.invalid",  # contiguous (round-3 case)
    "notreal.person @nonexistent-employer.invalid",  # space BEFORE the @ (B6)
    "notreal.person@ nonexistent-employer.invalid",  # space AFTER the @ (B6)
    "notreal.person\n@nonexistent-employer.invalid",  # line-broken (B6)
    "notreal.person @ nonexistent-university.invalid",  # both sides (B6)
]

_CLEAN_EMAIL_PROBES = [
    "casey.rivera@example.test",
    "harper.nakamura @example.test",  # reflowed, but still synthetic
]

_LEAKY_PHONE_PROBES = [
    "604-555-1212",
    "(604) 555-1212",
    "6045551212",
    "+1 604 555 1212",
    "604–555–1212",  # U+2013 en-dash (N5)
    "604‑555‑1212",  # U+2011 non-breaking hyphen (N5)
    "604/555/1212",  # slash separators (N5)
]

_CLEAN_PHONE_PROBES = [
    "555-0101",
    "604 555 0117",
    "604  555\n0117",  # r17's reflowed-but-synthetic phone
    "604–555–0117",  # en-dash, still inside the reserved block
]


@pytest.mark.parametrize("probe", _LEAKY_EMAIL_PROBES)
def test_email_scanner_flags_format_divergent_addresses(probe: str) -> None:
    """Finding B6. Whitespace around the `@` must NOT defeat the scanner."""
    assert _offending_emails(probe) == [probe], (
        f"the email scanner did not flag {probe!r} -- a leak reflowed by a PDF "
        f"extractor is the exact class ADR-007 F1-R is about, and it is the "
        f"class this corpus's r17 fixture exists to model"
    )


@pytest.mark.parametrize("probe", _CLEAN_EMAIL_PROBES)
def test_email_scanner_does_not_flag_the_synthetic_domain(probe: str) -> None:
    """Negative control: the scanner must not be a blanket 'any @ is a leak'
    check, or the corpus's own synthetic addresses would fail it."""
    assert _offending_emails(probe) == []


@pytest.mark.parametrize("probe", _LEAKY_PHONE_PROBES)
def test_phone_scanner_flags_every_separator_style(probe: str) -> None:
    """Finding N5. A number pasted out of a real PDF plausibly carries a
    unicode hyphen/dash or a slash instead of an ASCII hyphen."""
    assert _offending_phones(probe), (
        f"the phone scanner did not flag {probe!r} -- it is a real-range NANP "
        f"number outside the reserved-for-fiction 555-01xx block"
    )


@pytest.mark.parametrize("probe", _CLEAN_PHONE_PROBES)
def test_phone_scanner_does_not_flag_the_reserved_fake_range(probe: str) -> None:
    assert _offending_phones(probe) == []


# ── Phase-4a strengthening item 1: adversarial/weak backstop flag ────────


def test_all_weak_and_adversarial_labels_are_flagged_must_not_surface_in_topk() -> None:
    labels = _load_labels()
    for resume_id, entry in labels["resumes"].items():
        if entry["tag"] in {"weak", "adversarial"}:
            assert entry.get("must_not_surface_in_topk") is True, (
                f"{resume_id} ({entry['tag']}): must be flagged "
                f"must_not_surface_in_topk=true -- the backstop against a "
                f"partially-broken 4c ranker letting a bad candidate through"
            )


def test_r09_adversarial_keyword_stuffer_is_flagged_must_not_surface_in_topk() -> None:
    """Explicit check on the named fixture the spec calls out -- the highest
    keyword-overlap resume in the whole corpus must never surface in top-k."""
    labels = _load_labels()
    entry = labels["resumes"]["r09_sam_ortiz"]
    assert entry["tag"] == "adversarial"
    assert entry["must_not_surface_in_topk"] is True


def test_r09_adversarial_bait_is_top_tier_on_every_non_evidence_signal() -> None:
    """Finding B1 (round 3 opened it; round 4 closed it) -- the bait's POTENCY.

    thresholds.toml promises r09 is caught "however high its raw
    skill-keyword overlap with the JD is" and labels.json claims that overlap
    "is the highest of the whole corpus" -- but no test checked either.
    Mutation proof: cutting r09 down to a single ungrounded ``Python`` skill
    (``years: 1``, ``last_used_year: 2005``, generic summary) left all 226
    corpus tests green. A DEFANGED bait is rejected by any scorer at stage 2,
    so the fabrication trap silently stops trapping: 4c could ship an
    evidence verifier that returns True unconditionally and still see r09 fall
    out of the top-k for the wrong reason.

    ROUND 4 -- the round-3 version of this test WAS that defanged bait, in the
    one place nobody looked. MatchWeights' structured score has FIVE
    sub-scores (skill 0.40, experience 0.25, education 0.10, seniority 0.15,
    vector 0.10); this test asserted three and omitted EDUCATION and VECTOR --
    precisely the two on which r09 was weak. r09 shipped with a sub-bachelor
    ``Diploma, General Studies``, which fails the JD's ``min_level:
    bachelors`` outright, so the ``[adversarial]`` arm was INERT: a
    MatchWeights-faithful engine with a **no-op evidence verifier** scored r09
    at 0.7878 -> rank 8, i.e. outside the k=5 window, and PASSED both
    ``must_not_surface_in_topk`` and ``precision@5 = 1.0``. The trap never
    fired. Education alone is 0.10 x 0.6 = 0.06 of ``score_final``, which
    exceeded the 0.0485 gap between r09 and the top-5 cutoff -- so a single
    fixture field, asserted by nothing, was doing the rejection work the
    evidence verifier is supposed to do. Giving r09 a JD-allowed BSc leaves
    all 264 tests green; so did deleting its education entirely.

    With the bait repaired (BSc Computer Science, a JD-allowed field), the
    no-op-verifier engine ranks r09 **2nd** -> precision@5 = 0.80 -> the
    adversarial arm correctly FAILS. Under a CORRECT verifier r09 scores
    ~0.547 and lands at rank 11 -- far below the 0.844 top-5 cutoff. See
    labels.json for why an adversarial fixture that is top-tier on every
    non-evidence signal MUST land adjacent to the borderline tier: that is
    arithmetic (``0.6*structured + 0.3*0 + 0.1*0``), not a mislabel.

    So: r09 must be structurally TOP-TIER on every non-evidence signal. Only
    EVIDENCE verification may reject it.
    """
    labels = _load_labels()
    entry = labels["resumes"]["r09_sam_ortiz"]
    assert entry["tag"] == "adversarial"
    parsed = ResumeParsed.model_validate(_load_json(FIXTURES_DIR / entry["fixture"]))
    jd = JDExtracted.model_validate(_load_json(FIXTURES_DIR / labels["job"]["fixture"]))

    claimed = {s.name.lower(): s for s in parsed.skills}

    # (a) Claims EVERY required and EVERY nice-to-have skill -- maximal
    #     keyword overlap, so stage-2 skill coverage cannot be what sinks it.
    required = {s.name.lower() for s in jd.required_skills}
    nice = {s.name.lower() for s in jd.nice_to_have_skills}
    assert required <= claimed.keys(), (
        f"r09 must claim every required skill to be the corpus's maximal "
        f"keyword-stuffer; missing {sorted(required - claimed.keys())}"
    )
    assert nice <= claimed.keys(), (
        f"r09 must claim every nice-to-have skill too; missing "
        f"{sorted(nice - claimed.keys())}"
    )

    # (b) Every required skill's claimed years clears the JD's min_years, so
    #     the years component of the skill sub-score is maxed out.
    for jd_skill in jd.required_skills:
        assert jd_skill.min_years is not None
        claim = claimed[jd_skill.name.lower()]
        assert claim.years is not None
        assert claim.years >= jd_skill.min_years, (
            f"r09: {jd_skill.name!r} claims {claim.years} years but the JD asks "
            f"for {jd_skill.min_years} -- a years-short bait is demoted by the "
            f"structured scorer, not by the evidence verifier"
        )

    # (c) Every claimed JD-relevant skill sits in the TOP recency bucket, so
    #     recency decay cannot be what sinks it either.
    for name in required | nice:
        claim = claimed[name]
        assert claim.last_used_year is not None
        age = CURRENT_YEAR - claim.last_used_year
        assert age <= DEFAULT_WEIGHTS.recency_recent_years, (
            f"r09: {claim.name!r} last_used_year={claim.last_used_year} is {age} "
            f"years stale -- it must sit in the recency_recent bucket "
            f"(<= {DEFAULT_WEIGHTS.recency_recent_years}y) so recency is not "
            f"doing the rejection work the evidence verifier must do"
        )

    # (d) Seniority/experience: clears the JD minimum WITHOUT tripping the
    #     overqualification penalty.
    assert jd.min_years_experience > 0
    years = parsed.total_years_experience
    assert years >= jd.min_years_experience, (
        f"r09: total_years_experience={years} must clear the JD's "
        f"min_years_experience={jd.min_years_experience}"
    )
    assert years < DEFAULT_WEIGHTS.overqual_ratio * jd.min_years_experience, (
        f"r09: total_years_experience={years} must stay below the overqual "
        f"trigger ({DEFAULT_WEIGHTS.overqual_ratio} x "
        f"{jd.min_years_experience}) -- an overqualified bait would be demoted "
        f"by the overqual penalty rather than by the fabrication verifier"
    )

    # (e) EDUCATION (0.10 of the structured score) -- the sub-score the round-3
    #     test omitted and that was silently defusing the whole trap. r09 must
    #     MEET the JD's min_level AND hold one of its allowed fields, so
    #     neither the level check nor education_partial can demote it.
    assert jd.education is not None
    assert jd.education.min_level == "bachelors"
    assert parsed.education, "r09 must carry an education entry"
    edu = parsed.education[0]
    assert edu.degree.lower().startswith("bsc"), (
        f"r09: degree {edu.degree!r} must be a bachelor's-LEVEL degree meeting "
        f"the JD's min_level={jd.education.min_level!r}. A sub-bachelor degree "
        f"fails the education check on its own -- which is exactly how the "
        f"shipped `Diploma, General Studies` made the [adversarial] arm inert: "
        f"a no-op-evidence-verifier engine dropped r09 out of the top-5 on "
        f"EDUCATION, so the fabrication trap never fired"
    )
    allowed_fields = {f.lower() for f in jd.education.fields}
    assert edu.field is not None
    assert edu.field.lower() in allowed_fields, (
        f"r09: education field {edu.field!r} must be one of the JD's allowed "
        f"fields {sorted(allowed_fields)} -- a non-allowed field only earns "
        f"MatchWeights.education_partial ({DEFAULT_WEIGHTS.education_partial}), "
        f"which is a structured demotion the evidence verifier must not need"
    )
    # ...and the education CHUNK must narrate the same degree, so the bait is
    # not top-tier in the structured education[] entry while confessing a
    # diploma in the text an evidence/vector pass actually reads.
    chunk_blob = " ".join(c.text for c in parsed.chunks)
    assert edu.degree in chunk_blob, (
        f"r09: the structured degree {edu.degree!r} must also appear in a "
        f"chunk -- fixture and narrated text must not disagree"
    )

    # (f) VECTOR (0.10 of the structured score) -- the other omitted sub-score.
    #     The corpus CANNOT pin a cosine (it has no embedder and 4c's vector
    #     sub-score is model-computed), so it pins the vector sub-score's
    #     INPUT instead: `summary` is what `_build_summary_text` embeds into
    #     `summary_emb`, and r09's must name every JD skill so the bait is
    #     maximally on-topic in embedding space too. This is a weaker guarantee
    #     than (a)-(e) and is recorded as such: the residual is that a vector
    #     sub-score could still demote r09 by a few points. Measured, it does
    #     (r09 loses ~0.007 of structured to the 5th-place fixture on vector) --
    #     an order of magnitude below the 0.06 that education was worth, and it
    #     does NOT keep the repaired bait out of a no-op verifier's top-5.
    summary = parsed.summary.lower()
    for name in sorted(required | nice):
        assert name in summary, (
            f"r09: skill {name!r} must appear in `summary` -- summary is the "
            f"text embedded into summary_emb, and the bait must be maximally "
            f"on-topic in the vector sub-score's input, not only in the "
            f"structured skills[] list"
        )


def test_only_weak_and_adversarial_labels_carry_the_must_not_surface_flag() -> None:
    """Negative control: a 'strong'/'borderline' fixture flagged
    must_not_surface_in_topk would silently defeat precision@k -- catch a
    copy-paste mistake immediately."""
    labels = _load_labels()
    for resume_id, entry in labels["resumes"].items():
        if entry["tag"] in {"strong", "borderline"}:
            assert (
                "must_not_surface_in_topk" not in entry
                or not entry["must_not_surface_in_topk"]
            ), f"{resume_id} ({entry['tag']}): must not carry the backstop flag"


# ── Phase-4a strengthening item 2: recency-decay stale-skills fixture ────


_RECENCY_STALE_SKILL_NAMES = {
    "python",
    "postgresql",
    "apache airflow",
    "docker",
    "rest api design",
}


def test_r10_stale_recency_candidate_is_tagged_borderline() -> None:
    labels = _load_labels()
    assert labels["resumes"]["r10_jamie_okafor"]["tag"] == "borderline"


def test_r10_recency_relevant_skills_are_grounded_and_in_the_mid_or_old_bucket() -> (
    None
):
    labels = _load_labels()
    entry = labels["resumes"]["r10_jamie_okafor"]
    parsed = ResumeParsed.model_validate(_load_json(FIXTURES_DIR / entry["fixture"]))

    matched = [s for s in parsed.skills if s.name.lower() in _RECENCY_STALE_SKILL_NAMES]
    assert len(matched) == len(_RECENCY_STALE_SKILL_NAMES), (
        "r10 must claim every one of the required must-have skills to "
        "actually exercise recency demotion (not a plain skill-miss)"
    )

    for skill in matched:
        marker = SKILL_EVIDENCE_MARKERS[skill.name.lower()]
        cited = _cited_text(parsed, skill.evidence_chunk_ids)
        assert marker in cited, (
            f"r10: {skill.name!r} claim has no textual support -- it must be "
            f"genuinely grounded so the demotion is provably from recency, "
            f"not from a would-be-caught fabrication"
        )
        assert skill.last_used_year is not None
        age = CURRENT_YEAR - skill.last_used_year
        assert age > DEFAULT_WEIGHTS.recency_mid_years, (
            f"r10: {skill.name!r} last_used_year={skill.last_used_year} is only "
            f"{age} years stale -- must exceed MatchWeights.recency_mid_years "
            f"({DEFAULT_WEIGHTS.recency_mid_years}) to land in the 'old' bucket "
            f"and actually trigger demotion"
        )
        assert 2017 <= skill.last_used_year <= 2018, (
            f"r10: {skill.name!r} last_used_year={skill.last_used_year} should "
            f"be ~2017-2018 per the corpus spec"
        )


# ── Phase-4a strengthening item 3: non-CS-education partial-credit fixture ──


def test_r11_non_cs_education_candidate_is_tagged_strong() -> None:
    labels = _load_labels()
    assert labels["resumes"]["r11_skyler_brooks"]["tag"] == "strong"


def test_r11_covers_every_required_skill_with_a_non_cs_bachelors_degree() -> None:
    labels = _load_labels()
    entry = labels["resumes"]["r11_skyler_brooks"]
    parsed = ResumeParsed.model_validate(_load_json(FIXTURES_DIR / entry["fixture"]))
    jd = JDExtracted.model_validate(_load_json(FIXTURES_DIR / labels["job"]["fixture"]))
    assert jd.education is not None

    required_names = {s.name.lower() for s in jd.required_skills}
    candidate_names = {s.name.lower() for s in parsed.skills}
    assert required_names <= candidate_names, (
        "r11 must claim every required skill -- the point is that skills are "
        "fully covered and ONLY the education field differs from the "
        "all-CS-bachelor strong tier"
    )

    assert parsed.education, "r11 must have an education entry"
    edu = parsed.education[0]
    assert edu.degree.lower().startswith("bsc"), (
        "r11 must hold a genuine bachelor's-level degree (min_level is met) "
        "so the only discriminator is the FIELD, not the level"
    )
    allowed_fields = {f.lower() for f in jd.education.fields}
    assert edu.field is not None
    assert edu.field.lower() not in allowed_fields, (
        f"r11: education field {edu.field!r} must NOT be one of the JD's "
        f"approved fields {jd.education.fields!r} -- that mismatch is what "
        f"exercises MatchWeights.education_partial"
    )


# ── Phase-4a strengthening item 4: live motivation weight via cover letter ──


def test_r04_borderline_candidate_has_a_non_empty_cover_letter() -> None:
    parsed = ResumeParsed.model_validate(
        _load_json(RESUMES_DIR / "r04_morgan_lee.json")
    )
    assert parsed.cover_letter_chunks, (
        "r04 must carry cover_letter_chunks -- otherwise the 0.1 motivation "
        "weight is a constant 0 across the whole corpus and a broken "
        "motivation scorer is invisible"
    )


def test_r04_cover_letter_chunk_ids_are_valid_cl_nnn_tokens() -> None:
    parsed = ResumeParsed.model_validate(
        _load_json(RESUMES_DIR / "r04_morgan_lee.json")
    )
    ids = [c.id for c in parsed.cover_letter_chunks]
    assert ids, "r04 cover_letter_chunks must be non-empty"
    for chunk_id in ids:
        assert re.fullmatch(r"cl_\d{3}", chunk_id), (
            f"r04: cover letter chunk id {chunk_id!r} must be a one-based "
            f"cl_NNN token, in the parallel id space from resume c_NNN chunks"
        )
    assert ids[0] == "cl_001"
    assert ids == sorted(ids)


@pytest.mark.parametrize("resume_id", _resume_ids_from_fixture_files())
def test_only_r04_carries_a_non_empty_cover_letter(resume_id: str) -> None:
    """Negative control: every other fixture's cover_letter_chunks stays
    empty (including r16, the round-2 motivation-twin -- its whole point is
    that it does NOT have a cover letter), so r04 is unambiguously the one
    live motivation-weight case in the corpus."""
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{resume_id}.json"))
    if resume_id == "r04_morgan_lee":
        assert parsed.cover_letter_chunks
    else:
        assert parsed.cover_letter_chunks == []


# ── Phase-4a strengthening item 5 / round-2 GAP 1: expected_rank_band ────

# Canonical per-tag expected_rank_band, COMPUTED from the corpus's tier
# population counts (round-2 GAP 1 fix) so the bands are feasible BY
# CONSTRUCTION -- a correct ranker can always satisfy them.
# test_expected_rank_bands_fit_tier_populations (below) independently
# re-derives feasibility from labels.json on every run and will fail if this
# constant falls out of sync.
#
# ROUND 4 (the B1 knock-on): 'weak' and 'adversarial' NO LONGER SHARE A BAND.
#
# Round 3 gave both [Nstrong+Nborderline+1, null] -- "below every borderline".
# That was only true because the bait was BROKEN: r09 held a sub-bachelor
# diploma, so it lost the whole education sub-score and sank below the
# borderline tier for a reason that had nothing to do with fabrication (see
# test_r09_adversarial_bait_is_top_tier_on_every_non_evidence_signal). Repair
# the bait -- as finding B1 requires -- and the arithmetic is unavoidable:
#
#   score_final(r09) = 0.6*structured + 0.3*evidence_completeness + 0.1*motivation
#                    = 0.6*~0.91      + 0.3*0                     + 0.1*0
#                    ~= 0.547
#
# A candidate that is top-tier on EVERY non-evidence signal and scores ZERO on
# evidence lands, by construction, adjacent to the borderline tier -- measured:
# rank 11 of 17, one slot above the weakest borderline fixture (r05, 0.507).
# It is nowhere near the top-5 cutoff (0.844), so every gate still bites; it
# simply is not "below every borderline candidate", and a band that claimed
# otherwise would be the round-1 infeasible-band bug all over again, just
# pointed at a different tier. THIS IS ARITHMETIC, NOT A MISLABEL: do not
# "fix" it by weakening a threshold or by re-tagging r09.
#
# So the adversarial bait gets its OWN band -- "somewhere below every strong
# fixture, and therefore outside the top-k" -- and the borderline band gains
# exactly ONE slot of slack, because the bait consumes exactly one rank slot
# inside the borderline/weak region and pushes whatever it outranks down by
# one. The tiers otherwise keep their round-2 meaning.
_TAG_POPULATIONS_AT_AUTHORING_TIME = {
    "strong": 7,
    "borderline": 4,
    # 5 since the round-3 hardening added r17, the ADR-007 F1-R
    # format-divergent PII positive control (an honestly-weak candidate).
    "weak": 5,
    "adversarial": 1,
}
_N_STRONG = _TAG_POPULATIONS_AT_AUTHORING_TIME["strong"]
_N_BORDERLINE = _TAG_POPULATIONS_AT_AUTHORING_TIME["borderline"]
_N_ADVERSARIAL = _TAG_POPULATIONS_AT_AUTHORING_TIME["adversarial"]

_STRONG_MAX = _N_STRONG  # 7
_BORDERLINE_MIN = _N_STRONG + 1  # 8
# +_N_ADVERSARIAL: the one rank slot the bait may consume inside this region.
_BORDERLINE_MAX = _N_STRONG + _N_BORDERLINE + _N_ADVERSARIAL  # 12
_WEAK_MIN = _N_STRONG + _N_BORDERLINE + 1  # 12
_ADVERSARIAL_MIN = _BORDERLINE_MIN  # 8

TAG_RANK_BANDS: dict[str, dict[str, int | None]] = {
    "strong": {"min": 1, "max": _STRONG_MAX},
    "borderline": {"min": _BORDERLINE_MIN, "max": _BORDERLINE_MAX},
    "weak": {"min": _WEAK_MIN, "max": None},
    "adversarial": {"min": _ADVERSARIAL_MIN, "max": None},
}


def test_every_label_entry_has_an_expected_rank_band_matching_its_tag() -> None:
    labels = _load_labels()
    for resume_id, entry in labels["resumes"].items():
        assert "expected_rank_band" in entry, f"{resume_id}: missing expected_rank_band"
        band = entry["expected_rank_band"]
        assert set(band.keys()) == {"min", "max"}, f"{resume_id}: bad band shape {band}"
        canonical = TAG_RANK_BANDS[entry["tag"]]
        assert band["min"] == canonical["min"], (
            f"{resume_id} ({entry['tag']}): band min {band['min']} != canonical "
            f"{canonical['min']}"
        )
        assert band["max"] == canonical["max"], (
            f"{resume_id} ({entry['tag']}): band max {band['max']} != canonical "
            f"{canonical['max']}"
        )


def test_expected_rank_bands_are_internally_consistent() -> None:
    for tag, band in TAG_RANK_BANDS.items():
        assert band["min"] is not None and band["min"] >= 1, f"{tag}: bad min"
        if band["max"] is not None:
            assert band["min"] <= band["max"], f"{tag}: min > max ({band})"


def test_expected_rank_bands_are_ordered_with_only_the_adversarial_slack() -> None:
    """The quality tiers stay strictly ordered; the ONLY relaxation is the one
    rank slot the adversarial bait may consume in the borderline/weak region
    (see the TAG_RANK_BANDS comment for why that is arithmetic, not slop).

    The round-3 version of this test asserted
    ``TAG_RANK_BANDS["weak"] == TAG_RANK_BANDS["adversarial"]``. That
    invariant was an artifact of the DEFANGED bait and cannot survive finding
    B1's fix, so it is replaced -- deliberately, and with the weaker claim
    stated out loud -- by: the bait ranks below every STRONG fixture, and
    therefore outside the top-k. It is NOT claimed to rank below every
    borderline fixture.
    """
    strong_max = TAG_RANK_BANDS["strong"]["max"]
    borderline_min = TAG_RANK_BANDS["borderline"]["min"]
    borderline_max = TAG_RANK_BANDS["borderline"]["max"]
    weak_min = TAG_RANK_BANDS["weak"]["min"]
    adversarial_min = TAG_RANK_BANDS["adversarial"]["min"]

    assert strong_max is not None and borderline_min is not None
    assert (
        strong_max < borderline_min
    ), "strong tier must rank entirely above borderline"

    assert borderline_max is not None and weak_min is not None
    assert weak_min > strong_max, "weak tier must rank entirely below strong"
    assert borderline_max - weak_min == _N_ADVERSARIAL - 1, (
        f"the borderline band may overrun the weak band's start by exactly the "
        f"number of adversarial fixtures ({_N_ADVERSARIAL}) minus one -- one "
        f"displaced rank slot per bait, no more. Got borderline_max="
        f"{borderline_max}, weak_min={weak_min}"
    )

    assert adversarial_min is not None
    assert TAG_RANK_BANDS["weak"] != TAG_RANK_BANDS["adversarial"], (
        "weak and adversarial no longer share a band (round-4 finding B1): a "
        "bait that is top-tier on every non-evidence signal scores "
        "0.6*structured and lands ADJACENT to the borderline tier, not below "
        "every honestly-weak candidate"
    )
    assert adversarial_min > strong_max, (
        "the adversarial bait must rank below EVERY strong fixture -- that is "
        "the invariant that survives, and the one the evidence verifier earns"
    )
    assert TAG_RANK_BANDS["adversarial"]["max"] is None


def test_weak_and_adversarial_bands_sit_strictly_outside_top_k() -> None:
    with THRESHOLDS_PATH.open("rb") as fh:
        thresholds = tomllib.load(fh)
    k = thresholds["precision_at_k"]["k"]
    weak_min = TAG_RANK_BANDS["weak"]["min"]
    adversarial_min = TAG_RANK_BANDS["adversarial"]["min"]
    assert weak_min is not None and weak_min > k
    assert adversarial_min is not None and adversarial_min > k


def test_expected_rank_bands_fit_tier_populations() -> None:
    """Round-2 GAP-1 integrity guard: a per-tag expected_rank_band is only
    useful if a CORRECT ranker can actually satisfy it.

    Round 3 checked feasibility by requiring the bands to TILE ranks 1..N with
    no gap/overlap. That check is no longer expressive enough: after finding
    B1 the adversarial band deliberately OVERLAPS the borderline and weak
    bands (the bait consumes one rank slot somewhere in that region -- see
    TAG_RANK_BANDS). Overlapping windows are still perfectly gateable, they
    just need the general criterion instead of the tiling special case.

    So this is now a full **Hall's-condition** check on the interval bipartite
    graph "fixtures -> rank slots": a system of interval domains admits a
    perfect matching iff for EVERY contiguous window of ranks [lo, hi], the
    number of fixtures whose whole allowed band is CONTAINED in that window is
    at most the window's width. Populations are recomputed FRESH from
    labels.json on every run (this test does not trust TAG_RANK_BANDS to
    describe the corpus; it checks that it does).

    This still catches the round-1 bug exactly: 5 'strong' fixtures with band
    [1, 3] means the window [1, 3] contains 5 fixtures in 3 slots -> RED.
    """
    populations = _tag_populations()
    n_total = sum(populations.values())
    assert n_total == len(_load_labels()["resumes"])

    windows: list[tuple[str, int, int]] = []
    for tag, pop in populations.items():
        if pop == 0:
            continue
        band = TAG_RANK_BANDS[tag]
        lo = band["min"]
        assert lo is not None
        hi = band["max"] if band["max"] is not None else n_total
        assert 1 <= lo <= hi <= n_total, (
            f"{tag}: band [{lo}, {band['max']}] does not fit inside the "
            f"corpus's rank space 1..{n_total}"
        )
        windows.extend([(tag, lo, hi)] * pop)

    assert len(windows) == n_total

    for lo in range(1, n_total + 1):
        for hi in range(lo, n_total + 1):
            contained = [(t, a, b) for (t, a, b) in windows if lo <= a and b <= hi]
            assert len(contained) <= hi - lo + 1, (
                f"rank window [{lo}, {hi}] is {hi - lo + 1} slot(s) wide but "
                f"{len(contained)} fixture(s) are confined to it "
                f"({sorted({t for t, _, _ in contained})}) -- a correct ranker "
                f"CANNOT place them all. (This is the round-1 bug: "
                f"expected_rank_band=[1,3] for 'strong' while the corpus had 5 "
                f"'strong' fixtures.)"
            )


# ── Phase-4a strengthening item 6: self-dox positive control ─────────────


def test_r12_self_dox_candidate_is_tagged_weak_and_flagged() -> None:
    labels = _load_labels()
    entry = labels["resumes"]["r12_reese_dawson"]
    assert entry["tag"] == "weak"
    assert entry["must_not_surface_in_topk"] is True


def test_r12_candidate_name_appears_in_bullet_and_in_candidate_name() -> None:
    parsed = ResumeParsed.model_validate(
        _load_json(RESUMES_DIR / "r12_reese_dawson.json")
    )
    name = parsed.candidate.name
    assert name == "Reese Dawson"
    bullet_texts = [b.text for exp in parsed.experience for b in exp.bullets]
    assert any(name in text for text in bullet_texts), (
        "r12: the candidate's own name must appear verbatim inside a "
        "structured experience[].bullets[].text -- the ADR-007 N1-allowed "
        "residual positive control. A leak-checker must NOT flag this "
        "occurrence, but WOULD flag the same string in embedding-input text "
        "or an anonymized export (4c-required T4)."
    )


def test_r12_candidate_name_appears_in_chunk_text_the_embed_boundary_surface() -> None:
    """Finding E3 -- the OTHER half of r12's control, which was unasserted.

    ADR-007 draws a line between two surfaces that r12 straddles:

    * ``experience[].bullets[].text`` -- the N1 residual: identity here rides
      the outbox/at-rest payload unscrubbed, and is PERMITTED. That is what
      the test above pins.
    * ``chunks[].text`` -- the §7-F1 surface: every chunk is fed to
      ``embed()``, so ``_redact_candidate_pii`` MUST scrub identity out of it
      before the vector is built. A name reaching a Neo4j vector index is
      PII-equivalent under PIPEDA/FIPPA.

    Nothing pinned the second one: stripping the name out of r12's chunk
    ``c_003`` left all 226 corpus tests green, so the embed-boundary control
    could be silently neutered and 4c's leak scan would have no positive
    control to fail against. (c_003's text is byte-identical to the bullet's,
    which is exactly why both surfaces must be asserted separately.)
    """
    parsed = ResumeParsed.model_validate(
        _load_json(RESUMES_DIR / "r12_reese_dawson.json")
    )
    name = parsed.candidate.name
    assert name == "Reese Dawson"
    chunk_texts = [c.text for c in parsed.chunks]
    assert any(name in text for text in chunk_texts), (
        "r12: the candidate's own name must appear verbatim in >= 1 "
        "chunks[].text -- chunks are EMBEDDED, so this is the positive "
        "control the 4c embed-boundary leak scan must catch (ADR-007 §7-F1). "
        "Without it, a no-op _redact_candidate_pii passes the gate."
    )


# ── Phase-4a strengthening item 7: overqualified fixture ─────────────────


def test_r13_overqual_candidate_is_tagged_strong() -> None:
    labels = _load_labels()
    assert labels["resumes"]["r13_quinn_delgado"]["tag"] == "strong"


def test_r13_total_years_experience_triggers_the_overqual_ratio() -> None:
    labels = _load_labels()
    entry = labels["resumes"]["r13_quinn_delgado"]
    parsed = ResumeParsed.model_validate(_load_json(FIXTURES_DIR / entry["fixture"]))
    jd = JDExtracted.model_validate(_load_json(FIXTURES_DIR / labels["job"]["fixture"]))

    assert parsed.total_years_experience >= 12
    assert jd.min_years_experience > 0
    ratio = parsed.total_years_experience / jd.min_years_experience
    assert ratio >= DEFAULT_WEIGHTS.overqual_ratio, (
        f"r13: total_years_experience={parsed.total_years_experience} / "
        f"jd.min_years_experience={jd.min_years_experience} = {ratio:.2f}, "
        f"must be >= MatchWeights.overqual_ratio "
        f"({DEFAULT_WEIGHTS.overqual_ratio}) to actually trigger overqual "
        f"scoring"
    )


# ── Phase-4a strengthening item 8: gold_evidence anchors ─────────────────


def test_at_least_two_strong_fixtures_carry_gold_evidence_anchors() -> None:
    labels = _load_labels()
    strong_with_gold = [
        rid
        for rid, entry in labels["resumes"].items()
        if entry["tag"] == "strong" and entry.get("gold_evidence")
    ]
    assert len(strong_with_gold) >= 2


@pytest.mark.parametrize("resume_id", _resume_ids_with_gold_evidence())
def test_gold_evidence_anchor_is_an_exact_substring_of_its_cited_chunk(
    resume_id: str,
) -> None:
    labels = _load_labels()
    entry = labels["resumes"][resume_id]
    parsed = ResumeParsed.model_validate(_load_json(FIXTURES_DIR / entry["fixture"]))
    by_name = {s.name: s for s in parsed.skills}
    chunk_text = _chunk_text_by_id(parsed)

    assert entry["gold_evidence"], f"{resume_id}: gold_evidence must be non-empty"
    for skill_name, quote in entry["gold_evidence"].items():
        assert (
            skill_name in by_name
        ), f"{resume_id}: gold_evidence references unknown skill {skill_name!r}"
        skill = by_name[skill_name]
        assert skill.evidence_chunk_ids, (
            f"{resume_id}: {skill_name!r} has no evidence_chunk_ids to anchor "
            f"gold_evidence against"
        )
        cited = " ".join(
            chunk_text[cid] for cid in skill.evidence_chunk_ids if cid in chunk_text
        )
        assert quote in cited, (
            f"{resume_id}: gold_evidence[{skill_name!r}] = {quote!r} is not an "
            f"exact substring of its cited chunk text {cited!r}"
        )
        # Sanity-check the stand-in fuzz measure the negative-evidence guard
        # below relies on: a genuine (exact-substring) anchor must clear the
        # threshold under it, or a "< fuzz_threshold" assertion would be
        # vacuously true for every string on earth.
        assert _best_partial_ratio(quote, cited) >= DEFAULT_WEIGHTS.evidence_verify_fuzz


# ── Falsifiability hardening G1: negative (fabricated) evidence anchors ───


def _resume_ids_with_negative_evidence() -> list[str]:
    labels = _load_labels()
    return sorted(
        resume_id
        for resume_id, entry in labels["resumes"].items()
        if entry.get("negative_evidence")
    )


def test_negative_evidence_anchors_exist_including_on_the_adversarial_fixture() -> None:
    """Finding G1. Every ``gold_evidence`` anchor is an exact substring of its
    cited chunk, so every one of them verifies at 1.0 -- which means
    ``[evidence].verification_rate_min = 1.0`` is satisfiable TODAY by a
    verifier that returns ``True`` unconditionally. Nothing in the corpus
    pinned a quote that MUST score below ``fuzz_threshold``, so the
    anti-fabrication invariant was deferred to 4c on trust. The
    ``negative_evidence`` block is the in-corpus falsifier."""
    with_negative = _resume_ids_with_negative_evidence()
    assert len(with_negative) >= 2, (
        "at least one fabricated quote per relevant fixture -- the adversarial "
        "bait AND >= 1 genuine fixture (so the verifier is shown to "
        "DISCRIMINATE, not merely to reject everything)"
    )
    assert "r09_sam_ortiz" in with_negative, (
        "the keyword-stuffer must carry the fabrication the verifier has to "
        "catch -- it is the whole reason the fixture exists"
    )
    labels = _load_labels()
    assert any(
        labels["resumes"][rid].get("gold_evidence") for rid in with_negative
    ), "at least one fixture must carry BOTH gold and negative anchors"


@pytest.mark.parametrize("resume_id", _resume_ids_with_negative_evidence())
def test_negative_evidence_quote_cannot_verify_against_its_cited_chunk(
    resume_id: str,
) -> None:
    """Each ``negative_evidence`` entry maps a chunk id -> a FABRICATED quote
    that a correct anti-fabrication verifier must fail to verify against that
    chunk (score < ``MatchWeights.evidence_verify_fuzz``), so it gets blanked
    rather than surfaced.

    Scored with ``_best_partial_ratio`` -- the LENIENT best-window measure --
    so a quote that fails here fails under any reasonable rapidfuzz-style
    ratio the 4c verifier picks."""
    labels = _load_labels()
    entry = labels["resumes"][resume_id]
    parsed = ResumeParsed.model_validate(_load_json(FIXTURES_DIR / entry["fixture"]))
    chunk_text = _chunk_text_by_id(parsed)
    fuzz = DEFAULT_WEIGHTS.evidence_verify_fuzz

    assert entry["negative_evidence"], f"{resume_id}: negative_evidence is empty"
    for chunk_id, quote in entry["negative_evidence"].items():
        assert chunk_id in chunk_text, (
            f"{resume_id}: negative_evidence cites unknown chunk {chunk_id!r} -- "
            f"a fabricated quote must be anchored to a REAL chunk, or the "
            f"verifier never gets the chance to reject it"
        )
        cited = chunk_text[chunk_id]
        assert quote not in cited, (
            f"{resume_id}: negative_evidence[{chunk_id!r}] is an exact substring "
            f"of the chunk -- it is not a fabrication at all"
        )
        score = _best_partial_ratio(quote, cited)
        assert score < fuzz, (
            f"{resume_id}: negative_evidence[{chunk_id!r}] = {quote!r} scores "
            f"{score:.3f} against its cited chunk, which is >= the "
            f"anti-fabrication threshold {fuzz} -- a correct verifier would "
            f"VERIFY it, so it is not a usable fabrication control"
        )


# ── Round-2 GAP 2: matched-pair ordering_controls ─────────────────────────

_EXPECTED_ORDERING_CONTROL_DIMENSIONS = {"education", "overqual", "motivation"}


def test_labels_manifest_has_ordering_controls_for_each_gated_dimension() -> None:
    controls = _ordering_controls()
    assert controls, "labels.json must carry a non-empty ordering_controls list"
    dims = {c["dimension"] for c in controls}
    assert dims == _EXPECTED_ORDERING_CONTROL_DIMENSIONS, (
        f"ordering_controls dimensions {dims} != expected "
        f"{_EXPECTED_ORDERING_CONTROL_DIMENSIONS} -- education/overqual/"
        f"motivation must each have exactly one matched-pair control"
    )


@pytest.mark.parametrize("control", _ordering_controls(), ids=lambda c: c["dimension"])
def test_ordering_control_entry_is_well_formed(control: dict[str, str]) -> None:
    resume_ids = set(_resume_ids_from_labels())
    assert control["dimension"] in _EXPECTED_ORDERING_CONTROL_DIMENSIONS
    assert control["higher_id"] in resume_ids, (
        f"ordering_controls[{control['dimension']}].higher_id "
        f"{control['higher_id']!r} has no matching labels.json entry"
    )
    assert control["lower_id"] in resume_ids, (
        f"ordering_controls[{control['dimension']}].lower_id "
        f"{control['lower_id']!r} has no matching labels.json entry"
    )
    assert (
        control["higher_id"] != control["lower_id"]
    ), "a matched pair must be two distinct fixtures"
    assert control["rationale"].strip(), (
        f"ordering_controls[{control['dimension']}] must document WHY the "
        f"higher_id should outrank the lower_id"
    )


def test_ordering_control_pair_members_share_the_same_tag() -> None:
    """Each matched pair should sit in the same tier -- the target dimension
    is a within-tier tie-breaker (education/overqual/motivation each move
    score by a small amount), not something that should also be doing the
    work of a tier-level tag flip (that's what r10's recency fixture is
    for)."""
    labels = _load_labels()
    for control in _ordering_controls():
        higher_tag = labels["resumes"][control["higher_id"]]["tag"]
        lower_tag = labels["resumes"][control["lower_id"]]["tag"]
        assert higher_tag == lower_tag, (
            f"{control['dimension']}: higher_id {control['higher_id']!r} tag "
            f"{higher_tag!r} != lower_id {control['lower_id']!r} tag "
            f"{lower_tag!r} -- a matched pair should isolate ONE dimension, "
            f"not also cross a tier boundary"
        )


# ── Round-2 GAP 2: per-pair "identical except X" twin integrity ──────────

# Scoring-relevant fields every twin pair must match exactly EXCEPT the pair's
# one target dimension. "skills"/"experience"/"chunks" are compared as
# raw-JSON list equality (order-sensitive, since evidence_chunk_ids/bullet
# ordering is itself part of the fixture contract already checked above).


def test_r14_education_twin_is_identical_to_r11_except_education_field() -> None:
    """r14 (education twin) must differ from r11 ONLY in the education
    field: identical skills, cited chunks, and experience, both hold a
    genuine bachelor's-LEVEL degree, but r14's field is JD-allowed
    (Computer Science) and r11's is not (Mechanical Engineering). If the
    twins accidentally differed anywhere else, a future 4c ordering
    assertion built on this pair would be confounded."""
    labels = _load_labels()
    jd = JDExtracted.model_validate(_load_json(FIXTURES_DIR / labels["job"]["fixture"]))
    assert jd.education is not None
    allowed_fields = {f.lower() for f in jd.education.fields}

    r11 = _load_resume_raw("r11_skyler_brooks")
    r14 = _load_resume_raw("r14_devon_ashworth")

    assert r11["skills"] == r14["skills"], "twins must claim identical skills"
    assert (
        r11["experience"] == r14["experience"]
    ), "twins must have identical experience (company/title/dates/bullets)"
    assert r11["total_years_experience"] == r14["total_years_experience"]
    # summary is embedded by _build_summary_text -> summary_emb (the vector
    # sub-score's input), so it MUST be byte-identical or it becomes an
    # uncontrolled second differentiator on top of the education entry.
    assert (
        r11["summary"] == r14["summary"]
    ), "twins must share a byte-identical, degree-neutral summary (it is embedded)"

    # Finding D1: FULL chunk-list equality, matching the other two pairs.
    #
    # This test used to relax to *cited*-chunk equality, which let the
    # education chunk c_005 differ between the twins ("BSc Mechanical
    # Engineering, ..." vs "BSc Computer Science, ..."). But EVERY chunk is
    # embedded (_embed_batched -> chunk_embs) and stage-3 evidence retrieval
    # runs over the whole chunk list against a JD that carries an education
    # requirement -- so r14 could out-score r11 through the EVIDENCE path (0.3
    # of score_final) even if MatchWeights.education_partial were a total
    # no-op, which is exactly what this pair exists to detect. c_005 is cited
    # by nothing in either twin, and education_partial reads the structured
    # education[] entry rather than a chunk, so the education chunk is simply
    # deleted from both fixtures. (Same confound commit e8e83be fixed for
    # `summary`, not carried over to the chunk list at the time.)
    assert r11["chunks"] == r14["chunks"], (
        "twins must share a byte-identical chunk LIST -- a differing chunk is "
        "embedded and evidence-retrieved, so it confounds the education signal"
    )

    r11_edu = r11["education"][0]
    r14_edu = r14["education"][0]
    assert (
        r11_edu["degree"].lower().startswith("bsc")
    ), "r11 must hold a bachelor's-level degree"
    assert (
        r14_edu["degree"].lower().startswith("bsc")
    ), "r14 must hold the SAME degree level -- only field differs"
    assert (
        r11_edu["field"].lower() not in allowed_fields
    ), "r11's field must stay outside the JD's allowed set (the control baseline)"
    assert (
        r14_edu["field"].lower() in allowed_fields
    ), "r14's field must be inside the JD's allowed set -- the target dimension"
    assert r11_edu["field"] != r14_edu["field"]


def test_r15_overqual_twin_is_identical_to_r13_except_total_years_experience() -> None:
    """r15 (overqual twin) must differ from r13 ONLY in
    total_years_experience: identical skills, experience, education, and
    chunks. r13's ratio triggers MatchWeights.overqual_ratio; r15's does
    not."""
    labels = _load_labels()
    jd = JDExtracted.model_validate(_load_json(FIXTURES_DIR / labels["job"]["fixture"]))

    r13 = _load_resume_raw("r13_quinn_delgado")
    r15 = _load_resume_raw("r15_cameron_whitfield")

    assert r13["skills"] == r15["skills"], "twins must claim identical skills"
    assert (
        r13["experience"] == r15["experience"]
    ), "twins must have identical experience"
    assert r13["education"] == r15["education"], "twins must share identical education"
    assert r13["chunks"] == r15["chunks"], "twins must share byte-identical chunks"
    # total_years_experience is NOT embedded, so summary is the ONLY place the
    # overqual signal could leak into summary_emb (the vector sub-score). It
    # must be byte-identical, or a no-op overqual penalty could still be
    # "rewarded" via the vector path and pass 4c's ordering assertion.
    assert (
        r13["summary"] == r15["summary"]
    ), "twins must share a byte-identical, seniority/years-neutral summary"

    assert r13["total_years_experience"] != r15["total_years_experience"], (
        "the twins must differ on total_years_experience -- that is the sole "
        "target dimension for this pair"
    )

    ratio_r13 = r13["total_years_experience"] / jd.min_years_experience
    ratio_r15 = r15["total_years_experience"] / jd.min_years_experience
    assert ratio_r13 >= DEFAULT_WEIGHTS.overqual_ratio, (
        f"r13 (control baseline) must still trigger overqual: ratio "
        f"{ratio_r13:.2f} must be >= {DEFAULT_WEIGHTS.overqual_ratio}"
    )
    assert ratio_r15 < DEFAULT_WEIGHTS.overqual_ratio, (
        f"r15 (target) must NOT trigger overqual: ratio {ratio_r15:.2f} must "
        f"be < {DEFAULT_WEIGHTS.overqual_ratio}"
    )
    assert r15["total_years_experience"] >= jd.min_years_experience, (
        "r15 must still clear the JD's minimum years -- it's a non-overqual "
        "control, not an under-qualified one"
    )


def test_r16_motivation_twin_is_identical_to_r04_except_cover_letter_chunks() -> None:
    """r16 (motivation twin) must differ from r04 ONLY in
    cover_letter_chunks: identical skills, experience, education,
    total_years_experience, and resume chunks. r04 carries a populated cover
    letter; r16 has none."""
    r04 = _load_resume_raw("r04_morgan_lee")
    r16 = _load_resume_raw("r16_rowan_castillo")

    assert r04["skills"] == r16["skills"], "twins must claim identical skills"
    assert (
        r04["experience"] == r16["experience"]
    ), "twins must have identical experience"
    assert r04["education"] == r16["education"], "twins must share identical education"
    assert r04["chunks"] == r16["chunks"], "twins must share byte-identical chunks"
    assert r04["total_years_experience"] == r16["total_years_experience"]
    assert (
        r04["summary"] == r16["summary"]
    ), "twins must share a byte-identical summary (it is embedded)"

    assert r04[
        "cover_letter_chunks"
    ], "r04 (control baseline) must keep its cover letter"
    assert (
        r16["cover_letter_chunks"] == []
    ), "r16 (target) must have NO cover letter -- the sole target dimension"


# ── Falsifiability hardening E5: format-divergent PII positive controls ───
#
# Two gaps in the merged corpus's PII arm:
#   1. NO fixture put a candidate name in `summary` (the `summary_emb`
#      boundary), and email/phone appeared ONLY inside the structured
#      `candidate` block -- so those arms of the 4c leak check had no positive
#      control in EMBEDDABLE text at all. A no-op scrub would pass.
#   2. ADR-007's round-4 F1-R finding was specifically about FORMAT-DIVERGENT
#      leaks (a line-broken name, a reflowed phone, a bare email local-part)
#      slipping past a scrub built from the LLM's *normalized* identifiers --
#      the fix that took Phase 3 four audit rounds to land had ZERO
#      eval-corpus regression coverage.
#
# r17 is the positive control for both. It is an honestly-weak candidate (no
# JD-relevant skills), so it exercises the PII surface without perturbing any
# ranking-quality signal.

_R17 = "r17_harper_nakamura"


def test_r17_is_tagged_weak_and_flagged_must_not_surface() -> None:
    labels = _load_labels()
    entry = labels["resumes"][_R17]
    assert entry["tag"] == "weak"
    assert entry["must_not_surface_in_topk"] is True
    assert entry["decision_point"] == "pii_format_divergent_positive_control"


def test_r17_candidate_name_appears_verbatim_in_the_summary() -> None:
    """The `summary_emb` boundary: `_build_summary_text` composes the summary
    into the embedded text, and ADR-007 §7-F1 scrubs identity out of it before
    `embed()`. No fixture exercised that arm."""
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{_R17}.json"))
    name = parsed.candidate.name
    assert name == "Harper Nakamura"
    assert name in parsed.summary, (
        "r17: the candidate's own name must appear verbatim in `summary` -- it "
        "is the only positive control for the summary_emb leak boundary"
    )


def test_r17_email_and_phone_appear_verbatim_in_embeddable_chunk_text() -> None:
    """A résumé header chunk carries name + email + phone. Before r17, email
    and phone existed ONLY in the structured `candidate` block, so the
    email/phone arms of the leak check had nothing to catch in embedded
    text."""
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{_R17}.json"))
    email, phone = parsed.candidate.email, parsed.candidate.phone
    assert email is not None and phone is not None
    chunk_texts = [c.text for c in parsed.chunks]
    assert any(email in t for t in chunk_texts), "r17: email must be in a chunk"
    assert any(phone in t for t in chunk_texts), "r17: phone must be in a chunk"


def test_r17_carries_every_adr007_f1r_format_divergent_variant() -> None:
    """The three F1-R shapes, each one a whitespace/format divergence between
    the structured identifier and the résumé body -- exactly what defeated the
    round-3 scrub and what the round-4 whitespace-flexible pattern +
    email-local-part scrubbing closed:

    * a LINE-BROKEN name  (`Harper\\nNakamura` vs `Harper Nakamura`)
    * a REFLOWED phone    (whitespace runs differing from the structured value)
    * a BARE email LOCAL-PART (`harper.nakamura`, no `@domain`)

    Each must be present verbatim in embeddable chunk text, and must NOT be a
    literal copy of the structured value (or it would be caught by a naive
    `re.escape` scrub and prove nothing)."""
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{_R17}.json"))
    name = parsed.candidate.name
    email = parsed.candidate.email
    phone = parsed.candidate.phone
    assert name is not None and email is not None and phone is not None
    blob = "\n".join(c.text for c in parsed.chunks)

    first, last = name.split(" ", 1)

    # (1) line-broken name: both tokens, in order, separated by a newline.
    assert re.search(rf"{re.escape(first)}\n{re.escape(last)}", blob), (
        "r17: a LINE-BROKEN name (first\\nlast) must appear in chunk text -- "
        "the ADR-007 F1-R regression control for the whitespace-flexible scrub"
    )

    # (2) reflowed phone: same digits, different whitespace runs.
    divergent_phones = [
        m.group()
        for m in _PHONE_SHAPED_RE.finditer(blob)
        if re.sub(r"\D", "", m.group()) == re.sub(r"\D", "", phone)
        and m.group() != phone
    ]
    assert divergent_phones, (
        f"r17: a REFLOWED phone (same digits as {phone!r}, different "
        f"whitespace) must appear in chunk text"
    )
    assert any(
        re.search(r"\s\s|\n|\t", p) for p in divergent_phones
    ), "r17: the reflowed phone must actually diverge on WHITESPACE runs"

    # (3) bare email local-part, with no @domain following it.
    local = email.split("@", 1)[0]
    assert re.search(rf"{re.escape(local)}(?!@)", blob), (
        "r17: a BARE email LOCAL-PART (no @domain) must appear in chunk text -- "
        "the ADR-007 F1-R control for local-part scrubbing"
    )


def test_r17_claims_no_jd_relevant_skill_so_it_perturbs_no_ranking_signal() -> None:
    """r17 exists to exercise the PII surface, not the ranker: it must stay an
    honestly-weak candidate so it cannot quietly change precision@k or any
    matched-pair ordering control."""
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{_R17}.json"))
    claimed = {s.name.lower() for s in parsed.skills}
    assert not (claimed & SKILL_EVIDENCE_MARKERS.keys()), (
        f"r17 must claim no JD-relevant skill; found "
        f"{sorted(claimed & SKILL_EVIDENCE_MARKERS.keys())}"
    )


# ── Falsifiability hardening H: run_evals.py is executed, not just read ──


def test_run_evals_main_returns_nonzero_until_4c_lands() -> None:
    """Finding H1. No test executed ``run_evals.py``, so its "cannot go green
    before 4c" honesty was structural, not gated: a stray `return 0`, or an
    accidentally-importable stub orchestrator, would have made the
    merge-blocking ranking-evals gate pass on an engine that does not exist.

    When 4c lands ``src.pipeline.matching.orchestrator``, this test must be
    replaced by a real assertion on the computed metrics -- do NOT delete it
    to make a green run possible."""
    module = _import_run_evals()
    assert module.main() != 0, (
        "run_evals.main() must fail while src.pipeline.matching.orchestrator "
        "does not exist (Phase 4c). A zero exit here means the ranking-evals "
        "gate would PASS with no ranking engine at all."
    )


def test_run_evals_loads_the_same_corpus_the_unit_tests_guard() -> None:
    module = _import_run_evals()
    corpus = module.load_corpus()
    thresholds = module.load_thresholds()
    assert {r.resume_id for r in corpus.resumes} == set(_resume_ids_from_labels())
    assert thresholds["precision_at_k"]["min_precision"] == _MIN_PRECISION


def test_run_evals_load_corpus_confines_fixture_paths_to_the_fixtures_dir() -> None:
    """Finding H2 (defense in depth, test-only surface). ``load_corpus()``
    joins ``FIXTURES_DIR`` with a path read from ``labels.json``, and in
    pathlib an ABSOLUTE right-hand side silently REPLACES the left-hand side
    (``Path('/a') / '/etc/passwd' == Path('/etc/passwd')``). Every resolved
    fixture path must stay inside ``FIXTURES_DIR``."""
    module = _import_run_evals()
    corpus = module.load_corpus()
    fixtures_dir = module.FIXTURES_DIR.resolve()
    for resume in corpus.resumes:
        resolved = Path(resume.path).resolve()
        assert resolved.is_relative_to(fixtures_dir), (
            f"{resume.resume_id}: fixture path {resolved} escaped " f"{fixtures_dir}"
        )


def test_run_evals_load_corpus_rejects_a_fixture_path_outside_the_fixtures_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation of the corpus, not of the code: point a labels entry at an
    ABSOLUTE path outside ``fixtures/`` and ``load_corpus()`` must refuse
    rather than silently loading it (pathlib's absolute-RHS replacement)."""
    module = _import_run_evals()

    escapee = tmp_path / "outside" / "evil.json"
    escapee.parent.mkdir()
    escapee.write_text(json.dumps({"chunks": []}), encoding="utf-8")

    fake_fixtures = tmp_path / "fixtures"
    fake_fixtures.mkdir()
    (fake_fixtures / "jd.json").write_text("{}", encoding="utf-8")
    (fake_fixtures / "labels.json").write_text(
        json.dumps(
            {
                "job": {"id": "job_x", "fixture": "jd.json"},
                "resumes": {"r_evil": {"tag": "weak", "fixture": str(escapee)}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "FIXTURES_DIR", fake_fixtures)

    with pytest.raises(ValueError, match="outside"):
        module.load_corpus()
