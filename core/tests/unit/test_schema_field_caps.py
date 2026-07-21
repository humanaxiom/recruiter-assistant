"""Unit tests — per-field ``max_length`` caps at the LLM ingest boundary.

Phase 2 ported the resume/job schemas from hris but several string fields on
the LLM-output shapes have no ``max_length`` cap. That is a real hole: these
fields flow straight from an untrusted local LLM response into either a
Postgres ``JSONB`` column or (for ``CandidateInfo``) a ``pgp_sym_encrypt``
``BYTEA`` column, with no upper bound on how large a single field can be. A
pathological or looping small model can emit a multi-KB string for a field
that should be a name or a job title.

Two kinds of test here:

1. Concrete cap tests for the fields confirmed missing a cap (name/email/
   phone/location, Bullet.text, Experience.company/title/start/end,
   EducationItem.degree/institution/field, Education.fields list length).
2. A STANDING GUARD: every ``str`` field (including ``str | None``) on
   ``CandidateInfo``, ``Bullet``, ``Experience``, ``EducationItem`` must
   carry a ``max_length`` constraint. This is the actual deliverable — it
   fails immediately if a FUTURE field is added to one of these models
   without a cap, rather than silently reopening the hole. Note this is
   intentionally broader than the concrete list above: it also requires
   ``Bullet.chunk_id`` (not explicitly named in the task spec) to carry a
   cap, since it is a ``str | None`` field on a guarded model.

Finding 7 (regression guard, from ranking-evals) adds a third kind: a direct,
obviously-named standing guard for the Phase-2 schema data-loss bug where
``ResumeParsed(skills=[ResumeSkill(...)])`` silently yielded ``skills == []``
because the model-level ``_drop_invalid_rows`` pre-validator only recognised
dict rows and dropped already-validated sub-model instances on the floor.
That bug was previously guarded only indirectly, via unrelated summary-text
cap tests — a confusing failure for a future editor who reintroduces it. The
fix (an ``isinstance(row, sub)`` pass-through branch) is already in
``src/schemas/resumes.py``, so the Finding-7 tests below PASS today; they are
a STANDING GUARD, not a red test.

ADR-022 follow-up item #3 adds a fourth kind, at the bottom of this file: the
evidence models (``RequirementEvidence`` / ``CoverLetterEvidence`` /
``EvidenceObject``) have the SAME unbounded-field hole, and it is worse
because the values feed O(n·m) fuzzy matching before they reach JSONB.
"""

from __future__ import annotations

import typing
from typing import Any

import pytest
from annotated_types import MaxLen
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from src.schemas.jobs import Education
from src.schemas.matching import (
    CoverLetterEvidence,
    EvidenceObject,
    RequirementEvidence,
)
from src.schemas.resumes import (
    Bullet,
    CandidateInfo,
    EducationItem,
    Experience,
    ResumeParsed,
    ResumeSkill,
    ResumeSkillDetail,
    ResumeSkillDetails,
)

# ── Introspection helpers ────────────────────────────────────────────────────


def _is_str_field(annotation: object) -> bool:
    """True for ``str`` or ``str | None`` (``Optional[str]``)."""
    if annotation is str:
        return True
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = typing.get_args(annotation)
        return str in args and type(None) in args and len(args) == 2
    return False


def _has_max_length(field_info: FieldInfo) -> bool:
    return any(isinstance(m, MaxLen) for m in field_info.metadata)


GUARD_MODELS: tuple[type[BaseModel], ...] = (
    CandidateInfo,
    Bullet,
    Experience,
    EducationItem,
)


@pytest.mark.parametrize("model", GUARD_MODELS, ids=[m.__name__ for m in GUARD_MODELS])
def test_every_str_field_on_the_guarded_models_has_a_max_length_cap(
    model: type[BaseModel],
) -> None:
    offenders = [
        name
        for name, info in model.model_fields.items()
        if _is_str_field(info.annotation) and not _has_max_length(info)
    ]
    assert not offenders, (
        f"{model.__name__} has str field(s) with no max_length cap: "
        f"{offenders} — a pathological LLM string in one of these flows "
        f"straight into Postgres (BYTEA for PII, JSONB otherwise) with no "
        f"upper bound."
    )


# ── CandidateInfo — highest blast radius (feeds pgp_sym_encrypt BYTEA) ──────


@pytest.mark.parametrize("field", ["name", "email", "phone", "location"])
def test_candidate_info_field_accepts_up_to_300_chars(field: str) -> None:
    value = "x" * 300
    info = CandidateInfo(**{field: value})
    assert getattr(info, field) == value


@pytest.mark.parametrize("field", ["name", "email", "phone", "location"])
def test_candidate_info_field_rejects_over_300_chars(field: str) -> None:
    with pytest.raises(ValidationError):
        CandidateInfo(**{field: "x" * 301})


# ── Bullet.text ───────────────────────────────────────────────────────────


def test_bullet_text_accepts_up_to_1000_chars() -> None:
    bullet = Bullet(text="x" * 1000)
    assert len(bullet.text) == 1000


def test_bullet_text_rejects_over_1000_chars() -> None:
    with pytest.raises(ValidationError):
        Bullet(text="x" * 1001)


# ── Experience.company / .title (300) and .start / .end (50) ────────────────


@pytest.mark.parametrize("field", ["company", "title"])
def test_experience_company_and_title_accept_up_to_300_chars(field: str) -> None:
    kwargs: dict[str, Any] = {"company": "Acme", "title": "Dev"}
    kwargs[field] = "x" * 300
    exp = Experience(**kwargs)
    assert getattr(exp, field) == "x" * 300


@pytest.mark.parametrize("field", ["company", "title"])
def test_experience_company_and_title_reject_over_300_chars(field: str) -> None:
    kwargs: dict[str, Any] = {"company": "Acme", "title": "Dev"}
    kwargs[field] = "x" * 301
    with pytest.raises(ValidationError):
        Experience(**kwargs)


@pytest.mark.parametrize("field", ["start", "end"])
def test_experience_start_and_end_accept_up_to_50_chars(field: str) -> None:
    kwargs: dict[str, Any] = {"company": "Acme", "title": "Dev", field: "x" * 50}
    exp = Experience(**kwargs)
    assert getattr(exp, field) == "x" * 50


@pytest.mark.parametrize("field", ["start", "end"])
def test_experience_start_and_end_reject_over_50_chars(field: str) -> None:
    kwargs: dict[str, Any] = {"company": "Acme", "title": "Dev", field: "x" * 51}
    with pytest.raises(ValidationError):
        Experience(**kwargs)


# ── EducationItem.degree / .institution / .field (300) ──────────────────────


@pytest.mark.parametrize("field", ["degree", "institution", "field"])
def test_education_item_field_accepts_up_to_300_chars(field: str) -> None:
    kwargs: dict[str, Any] = {"degree": "BSc", "institution": "UBC"}
    kwargs[field] = "x" * 300
    item = EducationItem(**kwargs)
    assert getattr(item, field) == "x" * 300


@pytest.mark.parametrize("field", ["degree", "institution", "field"])
def test_education_item_field_rejects_over_300_chars(field: str) -> None:
    kwargs: dict[str, Any] = {"degree": "BSc", "institution": "UBC"}
    kwargs[field] = "x" * 301
    with pytest.raises(ValidationError):
        EducationItem(**kwargs)


# ── jobs.Education.fields — the list itself has no cap today ────────────────


def test_education_fields_list_accepts_up_to_20_entries() -> None:
    edu = Education(fields=[f"f{i}" for i in range(20)])
    assert len(edu.fields) == 20


def test_education_fields_list_rejects_over_20_entries() -> None:
    with pytest.raises(ValidationError):
        Education(fields=[f"f{i}" for i in range(21)])


# ── Finding 7: standing guard for the Phase-2 lossy-validator regression ────
#
# NOTE: this whole section is a STANDING GUARD, not a red test — the
# already-validated-sub-model pass-through fix is already present in
# ResumeParsed._drop_invalid_rows / ResumeSkillDetails._coerce_rows. These
# tests are expected to PASS today; they exist so a future edit that
# reintroduces the "rows are not dicts, so they get silently dropped" bug
# fails loudly, by name, instead of via an unrelated summary-text cap test.


def test_resume_parsed_skills_survive_already_validated_sub_models() -> None:
    parsed = ResumeParsed(skills=[ResumeSkill(name="python")])
    assert parsed.skills != []
    assert parsed.skills[0].name == "python"


def test_resume_parsed_experience_survives_already_validated_sub_models() -> None:
    parsed = ResumeParsed(experience=[Experience(company="Acme", title="Engineer")])
    assert parsed.experience != []
    assert parsed.experience[0].company == "Acme"


def test_resume_parsed_education_survives_already_validated_sub_models() -> None:
    parsed = ResumeParsed(education=[EducationItem(degree="BSc", institution="UBC")])
    assert parsed.education != []
    assert parsed.education[0].degree == "BSc"


def test_resume_skill_details_skills_survive_already_validated_sub_models() -> None:
    details = ResumeSkillDetails(skills=[ResumeSkillDetail(name="python")])
    assert details.skills != []
    assert details.skills[0].name == "python"


# ── ADR-022 follow-up item #3 — bounded evidence fields ─────────────────────
#
# ADR-022 "Follow-up items" #3 (MEDIUM, ``schemas/matching.py:127,147,157``):
# neither ``evidence`` field, nor ``RequirementEvidence.requirement``, nor the
# ``requirements`` list carries a ``max_length``. ``overall_summary`` /
# ``overall_motivation`` are capped at 1000 but the QUOTES are not, which is
# backwards: the quotes are the larger, less trustworthy values.
#
# The blast radius is worse than for the resume schemas above, because the
# evidence quote is not merely stored — ``verify_evidence`` feeds it to
# ``rapidfuzz.partial_ratio`` against every cited chunk, which is O(n·m) in
# quote length × chunk length. A 2,000,000-char quote and 100,000 requirements
# are both accepted today, so a single looping model response is a CPU-bound
# denial of service on the shortlist worker before it is a JSONB bloat problem.
#
# Caps pinned here: ``evidence`` 2000 (both models), ``requirement`` 500,
# ``requirements`` list 64. ``evidence_chunk_ids`` is already capped at 8 and
# ``overall_*`` at 1000; both are re-pinned below as standing guards.

# (model, field, expected declared cap). Covers the NEW caps and the EXISTING
# ones, so a future edit that loosens either fails by name.
EVIDENCE_CAPS: tuple[tuple[type[BaseModel], str, int], ...] = (
    (RequirementEvidence, "evidence", 2000),
    (RequirementEvidence, "requirement", 500),
    (RequirementEvidence, "evidence_chunk_ids", 8),
    (CoverLetterEvidence, "evidence", 2000),
    (CoverLetterEvidence, "evidence_chunk_ids", 8),
    (EvidenceObject, "requirements", 64),
    (EvidenceObject, "overall_summary", 1000),
    (EvidenceObject, "overall_motivation", 1000),
)

_CAP_IDS = [f"{m.__name__}.{f}={n}" for m, f, n in EVIDENCE_CAPS]


@pytest.mark.parametrize("model, field, cap", EVIDENCE_CAPS, ids=_CAP_IDS)
def test_evidence_model_field_declares_the_expected_max_length(
    model: type[BaseModel], field: str, cap: int
) -> None:
    """Declarative pin on the exact numbers, independent of the behavioural
    tests below — so a cap cannot be quietly raised to a useless value."""
    info = model.model_fields[field]
    declared = [m.max_length for m in info.metadata if isinstance(m, MaxLen)]
    assert declared == [cap], (
        f"{model.__name__}.{field} should declare max_length={cap}, "
        f"found {declared or 'no MaxLen constraint'}"
    )


# ── evidence quote: 2000 chars, on BOTH models ──────────────────────────────

EVIDENCE_MODELS: tuple[tuple[type[BaseModel], dict[str, Any]], ...] = (
    (RequirementEvidence, {"requirement": "Python"}),
    (CoverLetterEvidence, {"theme": "motivation"}),
)

_EV_IDS = [m.__name__ for m, _ in EVIDENCE_MODELS]


@pytest.mark.parametrize("model, base", EVIDENCE_MODELS, ids=_EV_IDS)
def test_evidence_quote_accepts_exactly_2000_chars(
    model: type[BaseModel], base: dict[str, Any]
) -> None:
    obj = model(**{**base, "evidence": "x" * 2000})
    assert len(str(obj.model_dump()["evidence"])) == 2000


@pytest.mark.parametrize("model, base", EVIDENCE_MODELS, ids=_EV_IDS)
def test_evidence_quote_rejects_2001_chars(
    model: type[BaseModel], base: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model(**{**base, "evidence": "x" * 2001})


@pytest.mark.parametrize("model, base", EVIDENCE_MODELS, ids=_EV_IDS)
def test_evidence_quote_rejects_a_pathological_two_million_char_quote(
    model: type[BaseModel], base: dict[str, Any]
) -> None:
    """The size ADR-022 names explicitly — the O(n·m) fuzzy-match DoS input."""
    with pytest.raises(ValidationError):
        model(**{**base, "evidence": "x" * 2_000_000})


@pytest.mark.parametrize("model, base", EVIDENCE_MODELS, ids=_EV_IDS)
def test_evidence_quote_cap_is_enforced_via_model_validate_too(
    model: type[BaseModel], base: dict[str, Any]
) -> None:
    """The LLM JSON path, not just in-process construction."""
    with pytest.raises(ValidationError):
        model.model_validate({**base, "evidence": "x" * 2001})


# ── RequirementEvidence.requirement: 500 chars ──────────────────────────────


def test_requirement_text_accepts_exactly_500_chars() -> None:
    req = RequirementEvidence(requirement="x" * 500)
    assert len(req.requirement) == 500


def test_requirement_text_rejects_501_chars() -> None:
    with pytest.raises(ValidationError):
        RequirementEvidence(requirement="x" * 501)


def test_requirement_text_cap_is_enforced_via_model_validate_too() -> None:
    with pytest.raises(ValidationError):
        RequirementEvidence.model_validate({"requirement": "x" * 501})


# ── EvidenceObject.requirements: 64 entries ─────────────────────────────────


def _reqs(count: int) -> list[RequirementEvidence]:
    return [RequirementEvidence(requirement=f"req-{i}") for i in range(count)]


def test_requirements_list_accepts_exactly_64_entries() -> None:
    ev = EvidenceObject(requirements=_reqs(64))
    assert len(ev.requirements) == 64


def test_requirements_list_rejects_65_entries() -> None:
    with pytest.raises(ValidationError):
        EvidenceObject(requirements=_reqs(65))


def test_requirements_list_rejects_a_pathological_100000_entry_list() -> None:
    """The count ADR-022 names explicitly."""
    with pytest.raises(ValidationError):
        EvidenceObject.model_validate(
            {"requirements": [{"requirement": "x"} for _ in range(100_000)]}
        )


# ── Anti-over-rejection: the real corpus must still validate unchanged ───────
#
# The caps above are only defensible if they cannot reject legitimate input.
# Measured against the shipped eval corpus (core/tests/evals/fixtures):
#
#   * longest résumé chunk       = 148 chars  (r01_casey_rivera.json)
#   * longest cover-letter chunk = 243 chars  (r04_morgan_lee.json)
#   * JD requirement count       = 8          (5 required_skills + 3
#                                              nice_to_have_skills, per
#                                              run_evals._extract_evidence)
#
# So the 2000-char quote cap has ~8x headroom over the longest quotable chunk,
# and the 64-requirement cap has 8x headroom over the real JD. If any test
# below starts failing, the cap is wrong — not the corpus.

# Verbatim longest chunk in the corpus (r01_casey_rivera.json), 148 chars.
CORPUS_LONGEST_CHUNK = (
    "Designed and shipped Python REST APIs consumed by 40+ internal services "
    "at Nimbus Analytics Inc, using FastAPI and typed request/response "
    "contracts."
)

# Verbatim longest cover-letter chunk in the corpus (r04_morgan_lee.json), 243.
CORPUS_LONGEST_CL_CHUNK = (
    "I'm reaching out because your backend data engineering role lines up "
    "exactly with the invoicing-pipeline work I've been doing at Capital "
    "Ledger Systems, and I'm eager to bring that same focus to a team solving "
    "these problems at a larger scale."
)

# The JD's 8 requirements, verbatim from jd_backend_data_engineer.json.
CORPUS_REQUIREMENTS: tuple[str, ...] = (
    "Python",
    "PostgreSQL",
    "Apache Airflow",
    "Docker",
    "REST API design",
    "Kubernetes",
    "dbt",
    "Terraform",
)


def test_corpus_measurements_are_still_accurate() -> None:
    """Keeps the headroom argument above honest: if the corpus grows past
    these sizes this fails, and the caps get re-justified deliberately."""
    assert len(CORPUS_LONGEST_CHUNK) == 148
    assert len(CORPUS_LONGEST_CL_CHUNK) == 243
    assert len(CORPUS_REQUIREMENTS) == 8


def test_a_corpus_sized_evidence_payload_still_validates() -> None:
    """The whole realistic shape — 8 requirements, each quoting the longest
    real chunk, plus a cover-letter theme quoting the longest real cover-letter
    chunk — must validate, and round-trip unchanged."""
    payload: dict[str, Any] = {
        "requirements": [
            {
                "requirement": name,
                "status": "met",
                "evidence": CORPUS_LONGEST_CHUNK,
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            }
            for name in CORPUS_REQUIREMENTS
        ],
        "overall_summary": "Strong overall fit for the backend data role.",
        "cover_letter_presence": True,
        "cover_letter_evidence": [
            {
                "theme": "motivation",
                "evidence": CORPUS_LONGEST_CL_CHUNK,
                "evidence_chunk_ids": ["cl_001"],
                "confidence": 0.85,
            }
        ],
        "overall_motivation": "Clear, specific motivation tied to the role.",
    }
    ev = EvidenceObject.model_validate(payload)

    assert len(ev.requirements) == 8
    assert ev.requirements[0].requirement == "Python"
    # Unchanged: no truncation, no mangling of legitimate corpus text.
    assert all(r.evidence == CORPUS_LONGEST_CHUNK for r in ev.requirements)
    assert ev.cover_letter_evidence[0].evidence == CORPUS_LONGEST_CL_CHUNK

    roundtripped = EvidenceObject.model_validate(ev.model_dump())
    assert roundtripped.model_dump() == ev.model_dump()


@pytest.mark.parametrize("name", CORPUS_REQUIREMENTS)
def test_each_corpus_requirement_name_is_well_under_the_500_cap(name: str) -> None:
    assert RequirementEvidence(requirement=name).requirement == name
