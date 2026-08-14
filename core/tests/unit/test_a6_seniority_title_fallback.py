"""RED pin — ROADMAP A6 (4): the one real fix on this branch.

``orchestrator.py::_most_recent_title`` picks ``current[0]`` unconditionally,
so a résumé whose CURRENT role has a blank title returns ``None`` even when a
previous role reads "Senior Backend Engineer" — costing a real candidate the
full 15% seniority weight for a title that was, in fact, readable.

The fix: fall back to the most recent role that actually HAS a title,
instead of returning ``None`` outright. Precedence is preserved — current
role first, then document order — the fallback only widens WHICH role's
title is read, never re-orders the roles themselves.

This is deliberately the ONLY value change on the branch (see the spec's
scope decisions): it can only ever RAISE a candidate's seniority sub-score,
never lower it, and all 20 corpus fixtures have a titled current role, so the
corpus is provably unaffected.
"""

from __future__ import annotations

from src.pipeline.matching.orchestrator import _most_recent_title


def test_falls_back_to_titled_previous_role_when_current_title_blank() -> None:
    """THE pin. Blank current title, titled previous role -> the previous
    role's title, not None."""
    parsed = {
        "experience": [
            {"title": "", "is_current": True},
            {"title": "Senior Backend Engineer", "is_current": False},
        ]
    }
    assert _most_recent_title(parsed) == "Senior Backend Engineer", (
        "a blank CURRENT title must fall back to the previous role's title, "
        "not return None and cost the candidate the full seniority weight"
    )


def test_falls_back_when_the_current_roles_title_key_is_missing_entirely() -> None:
    parsed = {
        "experience": [
            {"is_current": True},  # no "title" key at all
            {"title": "Staff Engineer"},
        ]
    }
    assert _most_recent_title(parsed) == "Staff Engineer"


def test_still_returns_none_when_no_role_has_a_title_at_all() -> None:
    """The fallback widens WHICH role is read; it must not invent a title
    that does not exist anywhere in the résumé."""
    parsed = {
        "experience": [
            {"title": "", "is_current": True},
            {"title": None},
        ]
    }
    assert _most_recent_title(parsed) is None


def test_returns_none_for_no_experience_at_all() -> None:
    assert _most_recent_title({"experience": []}) is None
    assert _most_recent_title({}) is None


def test_current_role_precedence_is_unchanged_when_the_current_role_has_a_title() -> (
    None
):
    """The fallback must never override a perfectly good current-role title
    with an earlier one — current-first still wins."""
    parsed = {
        "experience": [
            {"title": "Backend Engineer", "is_current": False},
            {"title": "Staff Engineer", "is_current": True},
        ]
    }
    assert _most_recent_title(parsed) == "Staff Engineer"


def test_document_order_precedence_is_unchanged_when_the_first_role_has_a_title() -> (
    None
):
    """No ``is_current`` role at all — existing precedence already falls
    through to ``roles[0]``. When it has a title, the fallback must not
    disturb that."""
    parsed = {"experience": [{"title": "Engineer II"}, {"title": "Engineer I"}]}
    assert _most_recent_title(parsed) == "Engineer II"


def test_document_order_fallback_when_no_role_is_current_and_the_first_is_blank() -> (
    None
):
    """No ``is_current`` role, and ``roles[0]`` itself has a blank title —
    the fallback must still prefer DOCUMENT ORDER among the titled roles
    (the first titled role in list order), not an arbitrary titled role."""
    parsed = {
        "experience": [
            {"title": ""},
            {"title": "Backend Engineer"},
            {"title": "Staff Engineer"},
        ]
    }
    assert _most_recent_title(parsed) == "Backend Engineer", (
        "with no current role and a blank roles[0], the fallback must pick "
        "the first TITLED role in document order, not any titled role"
    )
