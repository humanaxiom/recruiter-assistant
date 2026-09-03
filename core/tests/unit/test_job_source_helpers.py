"""RED — the pure helpers behind the Taleo job upsert (ADR-046).

Two functions, both I/O-free, and both doing something the sync would be wrong
without.

``build_description_raw`` pads a Taleo posting's inline summary with its
structured fields so the result clears ``JobCreate``'s ``min_length=50``. That
is not padding for its own sake: several real SIMOFRAS postings put almost
everything in a linked PDF and leave two sentences inline, and a job whose
``description_raw`` is too short to create is a job the sync silently drops.

``normalise_employment_type`` maps free-text Taleo strings onto this repo's
``EmploymentType`` enum. It is deliberately CONSERVATIVE — an unrecognised
string becomes ``None`` (the column is nullable) rather than a plausible guess,
because a wrong-but-plausible bucket is worse than an empty one: it is
invisible, and it feeds ranking.

Neither exists yet — RED half of the TDD cycle.
"""

from __future__ import annotations

import pytest

# ------------------------------------------------------ build_description_raw


def test_summary_and_structured_fields_are_combined() -> None:
    from src.services.job_source_service import build_description_raw

    out = build_description_raw(
        "We are hiring a Research Analyst.",
        {"Department": "Neuroscience", "Salary Range": "$70,000"},
        None,
    )
    assert "Research Analyst" in out
    assert "Neuroscience" in out
    assert "$70,000" in out


def test_a_short_posting_is_padded_past_the_creation_floor() -> None:
    """THE reason this function exists. ``JobCreate.description_raw`` has
    ``min_length=50``; several real SIMOFRAS postings put the substance in a
    linked PDF and leave a sentence inline. Without the padding those jobs
    cannot be created at all, and the sync drops them without saying so."""
    from src.services.job_source_service import build_description_raw

    out = build_description_raw(
        "Analyst.",
        {"Department": "Neuroscience", "Location": "Burnaby", "Type": "Full Time"},
        None,
    )
    assert len(out) >= 50


def test_the_pdf_url_is_recorded_in_the_description() -> None:
    """ADR-046 alternative E: the PDF body is NOT fetched, so the URL is the
    only way an operator can reach the real job description. Losing it makes a
    thin posting unreviewable."""
    from src.services.job_source_service import build_description_raw

    out = build_description_raw("Analyst.", {}, "https://tre.tbe.taleo.net/jd.pdf")
    assert "https://tre.tbe.taleo.net/jd.pdf" in out


def test_an_entirely_empty_posting_still_yields_text() -> None:
    """``description_raw`` is NOT NULL. Returning "" would raise on insert and
    fail the whole sync run over one malformed posting."""
    from src.services.job_source_service import build_description_raw

    assert build_description_raw("", {}, None).strip() != ""


# -------------------------------------------------- normalise_employment_type


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Full Time", "full_time"),
        ("full-time", "full_time"),
        ("Part Time", "part_time"),
        ("Contract", "contract"),
        ("Co-op", "intern"),
        ("Internship", "intern"),
        # ORDER MATTERS, and this is the whole reason the function is not a
        # dict lookup. "Temporary Full Time" matches both "full+time" and
        # "temporary"; it is a fixed-term post, so 'contract' is right and
        # 'full_time' would misrepresent it to anyone filtering on permanence.
        ("Temporary Full Time", "contract"),
        ("Co-op Full Time", "intern"),
    ],
)
def test_known_employment_types_map(raw: str, expected: str) -> None:
    from src.services.job_source_service import normalise_employment_type

    assert normalise_employment_type(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "Seasonal Casual", "???"])
def test_an_unrecognised_type_is_none_not_a_guess(raw: str | None) -> None:
    """Conservative on purpose. ``employment_type`` is nullable, and a
    wrong-but-plausible bucket is worse than an empty one — it is invisible,
    survives review, and feeds ranking."""
    from src.services.job_source_service import normalise_employment_type

    assert normalise_employment_type(raw) is None


def test_every_mapped_value_is_a_real_employment_type() -> None:
    """The mapping must land inside the enum the rest of the system uses. A
    typo here would be an ``employment_type`` no filter matches and no
    constraint rejects."""
    from typing import get_args

    from src.schemas.jobs import EmploymentType
    from src.services.job_source_service import normalise_employment_type

    legal = set(get_args(EmploymentType))
    for raw in ("Full Time", "Part Time", "Contract", "Co-op", "Temporary Full Time"):
        mapped = normalise_employment_type(raw)
        assert mapped in legal, f"{raw!r} mapped to {mapped!r}, not an EmploymentType"
