"""RED — pins the credential-suffix rule for
``candidate_roster_service._normalize_name`` (Item 4, 2026-09-17).

``_normalize_name`` splits on any non-letter character into an
order-invariant token set, so "PAT EXAMPLE, PMP" -> {pat, example, pmp} used
to differ from "Example, Pat" -> {example, pat} by exactly the credential
suffix (see the (now-flipped) xfail this file's contract replaces,
``tests/unit/test_stress_roster.py::test_credential_suffix_no_longer_defeats_name_match``).

Contract pinned here (deliberately conservative — "Ma"/"Ba"/"Ca" are real
surnames, so a token is NEVER stripped just because it is short or looks
like an abbreviation):

* Split the ORIGINAL string on the LAST comma. Only tokens in the TAIL
  segment (after that comma) are candidates for stripping.
* A tail token is dropped iff (a) its upper-cased letters are in the
  credential vocabulary below, AND (b) at least 2 tokens survive overall
  after dropping it. Below that floor, nothing is stripped.
* The tail's CONCATENATED letters are also tested against the vocabulary,
  so a punctuated credential like "P.Eng." — which the non-letter split
  breaks into two tokens, "p" and "eng" — is still recognised as one
  credential ("peng" == PENG) and both its tokens are dropped together.
* A credential positioned BEFORE the last comma is never touched — the
  stripping rule looks only at the tail.
* No comma anywhere in the string -> the name is returned untouched (this
  is also how the existing accent-fold behaviour keeps working: it never
  depended on a comma).

Vocabulary (deliberately excludes CA, BA, MA — those are real surnames):
CSM, PMP, CPA, CFA, MBA, PHD, PENG, CHRP, CPHR, CISSP, PMIACP, MSC, BSC,
BSW, MSW, MD, RN, LLB, JD, CMA, CGA, SHRM, GPHR, ITIL, CCNA, MCSE.
"""

from __future__ import annotations

import pytest

from src.services.candidate_roster_service import _normalize_name

_EXAMPLE_PAT = frozenset({"example", "pat"})


# ── positives: a credential-bearing tail collapses to the plain name ───────


@pytest.mark.parametrize(
    "spelling",
    [
        "PAT EXAMPLE, PMP",
        "Pat Example, PMP",
        "pat example, pmp",
        "  Pat   Example ,  PMP  ",
    ],
)
def test_single_credential_tail_normalises_to_the_plain_name(spelling: str) -> None:
    assert _normalize_name(spelling) == _EXAMPLE_PAT


def test_multiple_comma_separated_credentials_in_the_tail_are_all_stripped() -> None:
    """ "Pat Example, CSM, PMP" — both trailing credentials must fall away,
    leaving just the name, not "csm" surviving as a bogus extra token."""
    assert _normalize_name("Pat Example, CSM, PMP") == _EXAMPLE_PAT


def test_punctuated_credential_is_recognised_by_its_concatenated_letters() -> None:
    """ "P.Eng." splits (on non-letters) into "p" and "eng"; neither token
    alone is in the vocabulary, but their concatenation "peng" is PENG, so
    both must be dropped together."""
    assert _normalize_name("Pat Example, P.Eng.") == _EXAMPLE_PAT


# ── negatives: real surnames and under-the-floor cases must survive ────────


def test_excluded_two_letter_surname_ma_is_never_stripped() -> None:
    """ "Ma" is a real surname and is deliberately EXCLUDED from the
    credential vocabulary — "Ma, Wei" must keep both tokens."""
    assert _normalize_name("Ma, Wei") == frozenset({"ma", "wei"})


def test_excluded_two_letter_surname_ba_is_never_stripped() -> None:
    assert _normalize_name("Ba, Kofi") == frozenset({"ba", "kofi"})


def test_excluded_surname_ca_in_the_tail_is_never_stripped() -> None:
    """ "Ca" sits in the TAIL here (after the last comma) and looks exactly
    like a stray abbreviation, but CA is deliberately excluded from the
    vocabulary — the rule must not generalise to "any short tail token"."""
    assert _normalize_name("Chen, Ca") == frozenset({"chen", "ca"})


def test_no_comma_leaves_the_name_entirely_untouched() -> None:
    """ "Pat Ma" has no comma at all, so the tail-only stripping rule never
    even engages — every token, including the short surname, survives."""
    assert _normalize_name("Pat Ma") == frozenset({"pat", "ma"})


def test_single_token_credential_with_no_comma_is_not_emptied() -> None:
    """A bare "Pmp" has no comma, so it is untouched by the tail rule
    entirely (and, independently, dropping it would leave zero tokens)."""
    assert _normalize_name("Pmp") == frozenset({"pmp"})


def test_credential_tail_below_the_two_token_floor_is_not_stripped() -> None:
    """ "Solo, Pmp" — dropping "pmp" would leave only ONE token ("solo"),
    below the >= 2-token floor, so the credential must be kept instead of
    being stripped down to a single bare token."""
    assert _normalize_name("Solo, Pmp") == frozenset({"solo", "pmp"})


def test_credential_before_the_last_comma_is_not_stripped() -> None:
    """ "PMP, Pat Example" — the credential sits BEFORE the last comma (it
    IS the head, not the tail), so the tail-only rule must leave it alone
    even though "PMP" is in the vocabulary."""
    assert _normalize_name("PMP, Pat Example") == frozenset({"pmp", "pat", "example"})


# ── the pre-existing accent fold must still hold alongside this rule ───────


def test_accent_fold_still_applies_with_a_credential_tail() -> None:
    """The accent fold (module docstring, ~lines 78-91) must keep working
    unchanged: "Ruíz" folds to "ruiz" regardless of whether a credential tail
    is also present."""
    assert _normalize_name("Ruíz") == frozenset({"ruiz"})


def test_accent_fold_still_applies_alongside_a_stripped_credential() -> None:
    assert _normalize_name("Ruíz, Pat, PMP") == frozenset({"ruiz", "pat"})
