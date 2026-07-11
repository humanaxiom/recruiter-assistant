"""Unit tests for ``src/schemas/resumes.py`` — the resumes-domain contracts.

Phase 2 ports hris ``packages/schemas/src/schemas/resumes.py`` (import fixed to
``from src.schemas.jobs import Skill``). These tests pin:

* the KEEP set is importable from the module AND re-exported by ``src.schemas``,
* the ``_coerce_year`` two-digit-pivot / bool-rejection / range behaviour,
* ``ResumeParsed._drop_invalid_rows`` (lossy per-row filter) and the
  ``ResumeSkillNames`` / ``ResumeSkillDetails`` coercers (str-or-dict, cap 200),
* the DEVIATION ``ResumeOut.uploaded_by: str | None`` (nullable actor label),
* ``ResumeOut`` is the PII redaction boundary — it exposes ``candidate`` but
  never the ``candidate_email`` / ``blob_key`` ciphertext,
* ``extra="forbid"``/``"ignore"`` behave per model.

These modules do not exist yet — this is the RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

import src.schemas as schemas_pkg
import src.schemas.resumes as resumes_mod
from src.schemas.resumes import (
    CandidateInfo,
    CoverLetterParsed,
    EducationItem,
    Experience,
    ResumeChunk,
    ResumeCore,
    ResumeDeleteOut,
    ResumeListItem,
    ResumeOut,
    ResumeParsed,
    ResumeSkill,
    ResumeSkillDetail,
    ResumeSkillDetails,
    ResumeSkillNames,
    ResumeUploadResult,
    _coerce_year,
)

_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)

# The public surface the module and package must expose. ``Skill`` is re-used
# from jobs and re-exported by resumes; ``ResumeCore`` / ``ResumeSkill*`` are
# defined in the module even though hris omits them from ``__all__``.
RESUMES_PUBLIC: tuple[str, ...] = (
    "ResumeStatus",
    "ResumeChunk",
    "Bullet",
    "Experience",
    "EducationItem",
    "CandidateInfo",
    "ResumeSkill",
    "CoverLetterSentiment",
    "CoverLetterParsed",
    "ResumeParsed",
    "ResumeCore",
    "ResumeSkillNames",
    "ResumeSkillDetail",
    "ResumeSkillDetails",
    "ResumeUploadResult",
    "ResumeListItem",
    "ResumeOut",
    "ResumeDeleteOut",
    "Skill",
)

# (raw value, coerced year) — the two-digit pivot is 69: <69 ⇒ 20xx, ≥69 ⇒ 19xx.
YEAR_CASES: tuple[tuple[object, int | None], ...] = (
    ("23", 2023),
    ("70", 1970),
    (1995, 1995),
    (True, None),  # bool is an int subclass — must be rejected
    (False, None),
    (2200, None),  # out of the 1900–2100 window and not a 2-digit year
    ("junk", None),
)

# (key, good_row, bad_row) for the lossy ``_drop_invalid_rows`` filter.
DROP_CASES: tuple[tuple[str, dict[str, object], dict[str, object]], ...] = (
    ("skills", {"name": "Python"}, {"name": ""}),  # empty name < min_length=1
    ("experience", {"company": "Acme", "title": "Dev"}, {"company": "Acme"}),
    ("education", {"degree": "BSc", "institution": "UBC"}, {"degree": "BSc"}),
)

# (raw value, coerced years) for ResumeSkillDetail._coerce_years (range 0–80).
YEARS_CASES: tuple[tuple[object, int | None], ...] = (
    ("3", 3),
    (3.0, 3),
    (3, 3),
    (True, None),
    (None, None),
    ("junk", None),
    (200, None),  # over the 80 cap
    (-1, None),  # under 0
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _resume_out_kwargs(**over: object) -> dict[str, object]:
    """A complete, valid ``ResumeOut`` payload (every non-defaulted field set)."""
    base: dict[str, object] = {
        "id": uuid4(),
        "job_id": uuid4(),
        "original_filename": "jane.pdf",
        "mime_type": "application/pdf",
        "file_size_bytes": 1024,
        "sha256": "a" * 64,
        "candidate": CandidateInfo(name="Jane"),
        "candidate_email_hash": None,
        "parsed": None,
        "status": "parsed",
        "uploaded_by": "recruiter@sfu.ca",
        "uploaded_at": _TS,
        "parsed_at": None,
        "failure_reason": None,
        "consent_acknowledged": True,
    }
    base.update(over)
    return base


# ── Public surface / re-export ───────────────────────────────────────────────


@pytest.mark.parametrize("name", RESUMES_PUBLIC)
def test_module_exposes_keep_name(name: str) -> None:
    assert hasattr(resumes_mod, name)


@pytest.mark.parametrize("name", RESUMES_PUBLIC)
def test_package_reexports_keep_name(name: str) -> None:
    assert hasattr(schemas_pkg, name)


def test_resumes_reexports_skill_from_jobs() -> None:
    """The import was retargeted to ``src.schemas.jobs`` — same object."""
    from src.schemas.jobs import Skill as JobsSkill

    assert resumes_mod.Skill is JobsSkill


# ── Happy-path validation ────────────────────────────────────────────────────


def test_resume_chunk_minimal_valid() -> None:
    chunk = ResumeChunk(id="c_1", text="hello")
    assert chunk.section == "other"  # default


def test_experience_minimal_valid() -> None:
    exp = Experience(company="Acme", title="Dev")
    assert exp.is_current is False


def test_candidate_info_all_optional() -> None:
    info = CandidateInfo()
    assert info.name is None


def test_cover_letter_parsed_defaults() -> None:
    cl = CoverLetterParsed()
    assert cl.raw_text == ""
    assert cl.themes == []


def test_resume_list_item_minimal_valid() -> None:
    item = ResumeListItem(
        id=uuid4(),
        original_filename="jane.pdf",
        status="parsed",
        uploaded_at=_TS,
        parsed_at=None,
    )
    assert item.has_cover_letter is False


def test_resume_upload_result_minimal_valid() -> None:
    res = ResumeUploadResult(original_filename="jane.pdf", outcome="accepted")
    assert res.warnings == []


def test_resume_delete_out_defaults_empty_warnings() -> None:
    out = ResumeDeleteOut(id=uuid4(), deleted=True)
    assert out.cleanup_warnings == []


def test_resume_core_minimal_valid() -> None:
    core = ResumeCore()
    assert core.total_years_experience == 0


def test_resume_out_minimal_valid() -> None:
    out = ResumeOut(**_resume_out_kwargs())
    assert out.candidate.name == "Jane"


def test_resume_parsed_roundtrips_faithfully() -> None:
    payload = {
        "candidate": {"name": "Jane", "email": "j@x.com"},
        "summary": "Experienced engineer",
        "total_years_experience": 5,
        "skills": [{"name": "Python", "years": 3, "last_used_year": 2023}],
        "experience": [
            {"company": "Acme", "title": "Dev", "bullets": [{"text": "did a thing"}]}
        ],
        "education": [{"degree": "BSc", "institution": "UBC", "year": 2015}],
        "chunks": [{"id": "c_1", "text": "hello world"}],
    }
    parsed = ResumeParsed.model_validate(payload)
    again = ResumeParsed.model_validate(parsed.model_dump())
    assert again.model_dump() == parsed.model_dump()


# ── _coerce_year (via the helper AND via both model fields) ───────────────────


@pytest.mark.parametrize("raw, expected", YEAR_CASES)
def test_coerce_year_helper(raw: object, expected: int | None) -> None:
    assert _coerce_year(raw) == expected


@pytest.mark.parametrize("raw, expected", YEAR_CASES)
def test_education_item_year_coercion(raw: object, expected: int | None) -> None:
    item = EducationItem(degree="BSc", institution="UBC", year=raw)
    assert item.year == expected


@pytest.mark.parametrize("raw, expected", YEAR_CASES)
def test_resume_skill_last_used_year_coercion(
    raw: object, expected: int | None
) -> None:
    skill = ResumeSkill(name="Python", last_used_year=raw)
    assert skill.last_used_year == expected


# ── ResumeParsed._drop_invalid_rows (lossy per-row filter) ────────────────────


@pytest.mark.parametrize("key, good, bad", DROP_CASES)
def test_drop_invalid_rows_keeps_only_valid(
    key: str, good: dict[str, object], bad: dict[str, object]
) -> None:
    parsed = ResumeParsed.model_validate({key: [good, bad]})
    rows = getattr(parsed, key)
    assert len(rows) == 1


def test_drop_invalid_rows_passes_through_non_list_untouched() -> None:
    """A non-list ``skills`` is left untouched (not coerced to an empty list),
    so field validation then rejects it — proving the guard's ``continue``
    branch rather than a silent fix that would hide a malformed payload."""
    with pytest.raises(ValidationError):
        ResumeParsed.model_validate({"skills": {"name": "Python"}})


# ── ResumeSkillNames / ResumeSkillDetails coercers ───────────────────────────


def test_resume_skill_names_coerces_str_and_dict_rows() -> None:
    m = ResumeSkillNames.model_validate(
        {"skills": ["Python", {"name": "Go"}, 123, {"x": 1}, None]}
    )
    assert m.skills == ["Python", "Go"]


def test_resume_skill_names_caps_at_200() -> None:
    m = ResumeSkillNames.model_validate({"skills": [f"s{i}" for i in range(250)]})
    assert len(m.skills) == 200


def test_resume_skill_details_coerces_rows() -> None:
    m = ResumeSkillDetails.model_validate(
        {"skills": ["Python", {"name": "Go", "years": 3}, "", {"x": 1}, 5]}
    )
    assert [s.name for s in m.skills] == ["Python", "Go"]
    assert m.skills[1].years == 3


def test_resume_skill_details_caps_at_200() -> None:
    m = ResumeSkillDetails.model_validate({"skills": [f"s{i}" for i in range(250)]})
    assert len(m.skills) == 200


@pytest.mark.parametrize("raw, expected", YEARS_CASES)
def test_resume_skill_detail_years_coercion(raw: object, expected: int | None) -> None:
    detail = ResumeSkillDetail(name="Python", years=raw)
    assert detail.years == expected


# ── DEVIATION: ResumeOut.uploaded_by is a nullable str actor label ───────────


def test_resume_out_uploaded_by_accepts_a_plain_string() -> None:
    out = ResumeOut(**_resume_out_kwargs(uploaded_by="recruiter@sfu.ca"))
    assert out.uploaded_by == "recruiter@sfu.ca"  # not coerced to a UUID


def test_resume_out_uploaded_by_accepts_none() -> None:
    out = ResumeOut(**_resume_out_kwargs(uploaded_by=None))
    assert out.uploaded_by is None


def test_resume_out_uploaded_by_rejects_non_string() -> None:
    with pytest.raises(ValidationError):
        ResumeOut(**_resume_out_kwargs(uploaded_by=123))


# ── PII redaction boundary: candidate in, ciphertext out ─────────────────────


def test_resume_out_exposes_decrypted_candidate_view() -> None:
    assert "candidate" in ResumeOut.model_fields


@pytest.mark.parametrize("ciphertext", ["candidate_email", "blob_key"])
def test_resume_out_never_exposes_ciphertext(ciphertext: str) -> None:
    """The DTO is the redaction boundary — only the email *hash* is allowed."""
    assert ciphertext not in ResumeOut.model_fields


def test_resume_out_keeps_email_hash_not_ciphertext() -> None:
    assert "candidate_email_hash" in ResumeOut.model_fields


# ── extra="forbid" vs extra="ignore" ─────────────────────────────────────────


def test_resume_out_forbids_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ResumeOut(**_resume_out_kwargs(bogus=1))


def test_resume_parsed_ignores_unknown_key() -> None:
    parsed = ResumeParsed.model_validate({"bogus": 1})
    assert "bogus" not in parsed.model_dump()


def test_resume_chunk_ignores_unknown_key() -> None:
    chunk = ResumeChunk.model_validate({"id": "c_1", "text": "hi", "bogus": 1})
    assert "bogus" not in chunk.model_dump()
