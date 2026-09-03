"""RED — SFU campus canonicalisation.

The sponsor asked for Location to mean **campus**: Burnaby/BBY,
Vancouver/YVR, Surrey/SRY. Three requirements fall out of that, and the third
is the one a naive implementation gets wrong.

1. The three campuses have canonical names, and their codes are inputs.
   A recruiter typing "bby" and a Taleo listing saying "Burnaby, BC" must land
   on the same stored value, or the column cannot be filtered or counted.
2. Anything unrecognised is preserved, never dropped. "Remote", "Great
   Northern Way", a future fourth campus — the column is TEXT and losing what
   somebody typed is worse than storing something uncanonical.
3. **Text naming more than one campus resolves to nothing.** Measured on the
   pilot box: exactly one of 26 JDs mentions a campus at all, and it says
   "…Student Services, and the Surrey and Vancouver campuses" — a description
   of the role's SCOPE, not its location. A scanner that returned the first
   match would have confidently mislabelled the only row it fired on. That is
   why nothing here scans a job description; this canonicalises a value some
   other source already asserted IS the location.
"""

from __future__ import annotations

import pytest


def test_the_three_campuses_are_the_vocabulary() -> None:
    from src.campus import CAMPUS_CODES

    assert CAMPUS_CODES == {"Burnaby": "BBY", "Vancouver": "YVR", "Surrey": "SRY"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Burnaby", "Burnaby"),
        ("burnaby", "Burnaby"),
        ("  BURNABY  ", "Burnaby"),
        ("BBY", "Burnaby"),
        ("bby", "Burnaby"),
        ("Burnaby, BC", "Burnaby"),
        ("Burnaby Mountain", "Burnaby"),
        ("SFU Burnaby Campus", "Burnaby"),
        ("Vancouver", "Vancouver"),
        ("YVR", "Vancouver"),
        ("Harbour Centre", "Vancouver"),
        ("Harbour Center", "Vancouver"),
        ("Vancouver, BC", "Vancouver"),
        ("Surrey", "Surrey"),
        ("SRY", "Surrey"),
        ("Surrey Central City", "Surrey"),
        ("Surrey, BC", "Surrey"),
    ],
)
def test_a_recognised_campus_canonicalises(raw: str, expected: str) -> None:
    from src.campus import canonical_campus

    assert canonical_campus(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "Remote",
        "Toronto",
        "Great Northern Way",
        # Names TWO campuses — see the module docstring. The pilot box's only
        # campus-mentioning JD reads exactly like this.
        "the Surrey and Vancouver campuses",
        "Burnaby / Surrey",
    ],
)
def test_anything_unrecognised_or_ambiguous_is_not_a_campus(raw: str | None) -> None:
    from src.campus import canonical_campus

    assert canonical_campus(raw) is None


def test_a_substring_of_a_longer_word_is_not_a_match() -> None:
    """``SRY`` inside an unrelated token must not fire. Whole-word matching,
    because the codes are three letters and three letters occur everywhere."""
    from src.campus import canonical_campus

    assert canonical_campus("MISRYAD") is None
    assert canonical_campus("Vancouverite") is None


def test_canonicalise_preserves_what_it_cannot_recognise() -> None:
    """The write-path helper: converge known values, never lose a typed one.
    A column that silently drops "Remote" teaches people not to use it."""
    from src.campus import canonicalise_location

    assert canonicalise_location("bby") == "Burnaby"
    assert canonicalise_location("Remote") == "Remote"
    assert canonicalise_location("  Remote  ") == "Remote"
    assert canonicalise_location("") is None
    assert canonicalise_location("   ") is None
    assert canonicalise_location(None) is None
