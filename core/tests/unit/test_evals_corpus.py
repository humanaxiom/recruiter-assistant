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
        ("pii", "leak_check"),
        ("pii", "allow_structured_fields"),
        ("determinism", "temperature"),
        ("determinism", "max_score_delta"),
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
