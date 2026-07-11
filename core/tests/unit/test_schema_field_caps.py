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
"""

from __future__ import annotations

import typing
from typing import Any

import pytest
from annotated_types import MaxLen
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from src.schemas.jobs import Education
from src.schemas.resumes import Bullet, CandidateInfo, EducationItem, Experience

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
