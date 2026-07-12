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
* no fixture contains anything resembling real PII -- every fixture uses the
  ``*@example.test`` / ``555-01xx`` synthetic markers and a name drawn from a
  fixed, obviously-fake allowlist; no fixture text contains a real-looking
  email domain or a phone-number pattern outside the reserved fake range.

--- Phase-4a strengthening (adequacy-review round) additions ---

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

This test suite is expected to PASS today -- it needs only the Phase 2
schemas, which are already merged.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
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
}

_EMAIL_RE = re.compile(r"^[a-z]+\.[a-z]+@example\.test$")
_PHONE_RE = re.compile(r"^555-01\d{2}$")

# Suspicious real-looking patterns to grep for across every fixture's raw
# text (not just the structured candidate fields) -- catches PII leaking
# into summary/bullet/chunk free text, not only the CandidateInfo block.
_REAL_EMAIL_DOMAIN_RE = re.compile(
    r"@(gmail|yahoo|hotmail|outlook|icloud|proton(mail)?)\.\w+", re.IGNORECASE
)
# A NANP-shaped phone number (xxx-xxx-xxxx / (xxx) xxx-xxxx) that is NOT in
# the reserved-fake 555-01xx block.
_REAL_PHONE_RE = re.compile(r"(?<!555-01)\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")

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

# Canonical per-tag expected_rank_band (Phase-4a strengthening item 5).
# Strictly ordered/disjoint across tiers: strong < borderline < weak/adversarial.
# max=None means "unbounded below" (the tier has no upper rank ceiling).
TAG_RANK_BANDS: dict[str, dict[str, int | None]] = {
    "strong": {"min": 1, "max": 3},
    "borderline": {"min": 4, "max": 6},
    "weak": {"min": 7, "max": None},
    "adversarial": {"min": 7, "max": None},
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        result: dict[str, Any] = json.load(fh)
    return result


def _load_labels() -> dict[str, Any]:
    return _load_json(LABELS_PATH)


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


@pytest.mark.parametrize(
    "section, key",
    [
        ("precision_at_k", "k"),
        ("precision_at_k", "min_precision"),
        ("evidence", "verification_rate_min"),
        ("evidence", "fuzz_threshold"),
        ("evidence", "min_completeness_in_topk"),
        ("pii", "leak_check"),
        ("pii", "allow_structured_fields"),
        ("determinism", "temperature"),
        ("determinism", "max_score_delta"),
        ("adversarial", "must_not_surface_in_topk"),
    ],
)
def test_thresholds_toml_has_required_section_key(section: str, key: str) -> None:
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert section in data, f"missing [{section}] section in thresholds.toml"
    assert key in data[section], f"missing {section}.{key} in thresholds.toml"


def test_thresholds_precision_at_k_values_are_sane() -> None:
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert data["precision_at_k"]["k"] >= 1
    assert 0.0 < data["precision_at_k"]["min_precision"] <= 1.0


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


def test_thresholds_determinism_pins_zero_temperature_and_zero_drift() -> None:
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert data["determinism"]["temperature"] == 0.0
    assert data["determinism"]["max_score_delta"] == 0.0


def test_thresholds_pii_leak_check_is_enabled() -> None:
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert data["pii"]["leak_check"] is True


def test_thresholds_adversarial_must_not_surface_in_topk_is_enabled() -> None:
    """The backstop invariant (Phase-4a strengthening item 1) must be a hard
    True -- a False/soft value would defeat the whole point of the flag."""
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    assert data["adversarial"]["must_not_surface_in_topk"] is True


def test_thresholds_min_completeness_in_topk_is_a_sane_fraction() -> None:
    with THRESHOLDS_PATH.open("rb") as fh:
        data = tomllib.load(fh)
    value = data["evidence"]["min_completeness_in_topk"]
    assert 0.0 < value <= 1.0


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


_ALL_FIXTURE_FILES = sorted(RESUMES_DIR.glob("*.json")) + [
    FIXTURES_DIR / "jd_backend_data_engineer.json",
    LABELS_PATH,
]


@pytest.mark.parametrize("path", _ALL_FIXTURE_FILES)
def test_no_fixture_file_contains_a_real_looking_email_domain(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    match = _REAL_EMAIL_DOMAIN_RE.search(raw)
    assert (
        match is None
    ), f"{path.name}: looks like a real email domain: {match.group()!r}"


@pytest.mark.parametrize("path", _ALL_FIXTURE_FILES)
def test_no_fixture_file_contains_a_phone_number_outside_the_fake_range(
    path: Path,
) -> None:
    raw = path.read_text(encoding="utf-8")
    match = _REAL_PHONE_RE.search(raw)
    assert (
        match is None
    ), f"{path.name}: looks like a real phone number: {match.group()!r}"


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
    empty, so r04 is unambiguously the one live motivation-weight case."""
    parsed = ResumeParsed.model_validate(_load_json(RESUMES_DIR / f"{resume_id}.json"))
    if resume_id == "r04_morgan_lee":
        assert parsed.cover_letter_chunks
    else:
        assert parsed.cover_letter_chunks == []


# ── Phase-4a strengthening item 5: expected_rank_band ─────────────────────


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


def test_expected_rank_bands_are_strictly_ordered_and_non_overlapping() -> None:
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
    assert borderline_max < weak_min, "borderline tier must rank entirely above weak"
    assert (
        TAG_RANK_BANDS["weak"] == TAG_RANK_BANDS["adversarial"]
    ), "weak and adversarial share the 'outside top-k' band"
    assert adversarial_min is not None


def test_weak_and_adversarial_bands_sit_strictly_outside_top_k() -> None:
    with THRESHOLDS_PATH.open("rb") as fh:
        thresholds = tomllib.load(fh)
    k = thresholds["precision_at_k"]["k"]
    weak_min = TAG_RANK_BANDS["weak"]["min"]
    adversarial_min = TAG_RANK_BANDS["adversarial"]["min"]
    assert weak_min is not None and weak_min > k
    assert adversarial_min is not None and adversarial_min > k


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
