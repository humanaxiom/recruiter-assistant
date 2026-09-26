"""RED — pins the contract for ``tests/e2e/driver.py``, the shared HTML
scraping helpers that ``tests/smoke/conftest.py`` and the new stress harness
(``scripts/e2e.sh`` / ``scripts/stress.sh``) both need.

Today these regexes are DUPLICATED as private helpers inside
``tests/smoke/conftest.py`` (``_page_token``/``_form_for``/``_hidden_value``).
This file pins the PURE, importable versions — ``page_token``,
``hidden_value``, ``form_for``, ``count_parsed_pills``, ``withdraw_ids``,
``is_parsing`` — that ``tests/e2e/driver.py`` must expose, and pins that
``conftest.py`` is updated to import them rather than keep its own copy.

Not written here (Tester scope: tests only, never implementation) —
``tests/e2e/driver.py`` itself. Every test below fails at COLLECTION with
``ModuleNotFoundError`` until it exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.driver import (
    count_parsed_pills,
    form_for,
    hidden_value,
    is_parsing,
    page_token,
    withdraw_ids,
)

_CONFTEST = Path(__file__).resolve().parents[1] / "smoke" / "conftest.py"


# ── page_token ───────────────────────────────────────────────────────────


def test_page_token_extracts_the_hx_headers_csrf_token() -> None:
    html = '<body hx-headers=\'{"X-CSRF-Token": "abc123"}\'>' "<div>hello</div></body>"
    assert page_token(html) == "abc123"


def test_page_token_returns_none_when_absent() -> None:
    assert page_token("<body><div>no token here</div></body>") is None


def test_page_token_returns_none_on_empty_string() -> None:
    assert page_token("") is None


# ── hidden_value ─────────────────────────────────────────────────────────


def test_hidden_value_extracts_named_hidden_input() -> None:
    form = (
        '<form action="/x"><input type="hidden" name="csrf_token" '
        'value="tok-1" /></form>'
    )
    assert hidden_value(form, "csrf_token") == "tok-1"


def test_hidden_value_returns_none_for_missing_name() -> None:
    form = '<form action="/x"><input type="hidden" name="other" value="v" /></form>'
    assert hidden_value(form, "csrf_token") is None


def test_hidden_value_handles_empty_value_attribute() -> None:
    form = '<input type="hidden" name="reason" value="" />'
    assert hidden_value(form, "reason") == ""


@pytest.mark.parametrize("name", ["csrf_token", "job_id", "context"])
def test_hidden_value_is_specific_to_the_requested_name(name: str) -> None:
    form = (
        '<input type="hidden" name="csrf_token" value="A" />'
        '<input type="hidden" name="job_id" value="B" />'
        '<input type="hidden" name="context" value="C" />'
    )
    expected = {"csrf_token": "A", "job_id": "B", "context": "C"}[name]
    assert hidden_value(form, name) == expected


# ── form_for ─────────────────────────────────────────────────────────────


def test_form_for_extracts_the_matching_form_block() -> None:
    html = (
        "<div>other stuff</div>"
        '<form action="/resumes/abc/withdraw" method="post">'
        '<input name="reason" value="" />'
        "</form>"
        "<div>trailing</div>"
    )
    block = form_for(html, "/resumes/abc/withdraw")
    assert block is not None
    assert block.startswith("<form")
    assert block.endswith("</form>")
    assert "reason" in block


def test_form_for_returns_none_when_no_form_matches() -> None:
    html = '<form action="/jobs/1/status">x</form>'
    assert form_for(html, "/resumes/zzz/withdraw") is None


def test_form_for_picks_the_right_form_among_several() -> None:
    html = (
        '<form action="/resumes/id-1/withdraw"><input name="a" value="1"/></form>'
        '<form action="/resumes/id-2/withdraw"><input name="a" value="2"/></form>'
    )
    block = form_for(html, "/resumes/id-2/withdraw")
    assert block is not None
    assert 'value="2"' in block
    assert 'value="1"' not in block


# ── count_parsed_pills ───────────────────────────────────────────────────


def test_count_parsed_pills_counts_occurrences() -> None:
    html = '<span class="pill-parsed">Python</span><span class="pill-parsed">SQL</span>'
    assert count_parsed_pills(html) == 2


def test_count_parsed_pills_is_zero_when_absent() -> None:
    assert count_parsed_pills("<div>no pills here</div>") == 0


# ── withdraw_ids ─────────────────────────────────────────────────────────


def test_withdraw_ids_extracts_uuids_in_document_order() -> None:
    html = (
        '<a href="/resumes/11111111-1111-1111-1111-111111111111/withdraw">a</a>'
        '<a href="/resumes/22222222-2222-2222-2222-222222222222/withdraw">b</a>'
    )
    assert withdraw_ids(html) == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]


def test_withdraw_ids_returns_empty_list_when_none_present() -> None:
    assert withdraw_ids("<div>nothing to withdraw</div>") == []


def test_withdraw_ids_ignores_non_uuid_looking_segments() -> None:
    html = '<a href="/resumes/not-a-uuid/withdraw">a</a>'
    assert withdraw_ids(html) == []


# ── is_parsing ───────────────────────────────────────────────────────────


def test_is_parsing_true_when_badge_present() -> None:
    assert is_parsing('<span class="badge-parsing">Parsing…</span>') is True


def test_is_parsing_false_when_badge_absent() -> None:
    assert is_parsing('<span class="pill-parsed">Python</span>') is False


def test_is_parsing_false_on_empty_string() -> None:
    assert is_parsing("") is False


# ── conftest must import the shared helpers, not keep its own copy ────────


def test_smoke_conftest_imports_the_shared_driver_helpers() -> None:
    source = _CONFTEST.read_text(encoding="utf-8")
    assert (
        "from tests.e2e.driver import" in source
        or "from tests.e2e import driver" in source
    ), (
        "tests/smoke/conftest.py must import the CSRF/form helpers from "
        "tests.e2e.driver instead of keeping its own copy"
    )


def test_smoke_conftest_no_longer_defines_its_own_page_token_regex() -> None:
    """A text assertion is acceptable here (per the task brief): the whole
    point is that conftest.py stops OWNING this regex once it is shared."""
    source = _CONFTEST.read_text(encoding="utf-8")
    assert "X-CSRF-Token" not in source, (
        "the CSRF-token regex must move to tests/e2e/driver.py; conftest.py "
        "should only call page_token(...)"
    )
