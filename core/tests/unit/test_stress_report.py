"""RED — pins the contract for ``tests/e2e/report.py``, the stress harness's
percentile summary and PASS/FAIL verdict.

Not written here (Tester scope: tests only, never implementation) —
``tests/e2e/report.py`` itself. Every test that imports it fails at
COLLECTION with ``ModuleNotFoundError`` until it exists.
"""

from __future__ import annotations

from tests.e2e.report import render_markdown, summarise

# 1..100 (ms) — percentiles land on round numbers, so the exact expected
# value is unambiguous rather than "close to".
_HUNDRED = [float(i) for i in range(1, 101)]


# ── summarise: percentile math on known data ────────────────────────────


def test_summarise_p50_on_one_to_one_hundred() -> None:
    out = summarise({"jd_extract": _HUNDRED}, errors={}, timeouts={})
    assert (
        out["steps"]["jd_extract"]["p50"] == 50.0
        or out["steps"]["jd_extract"]["p50"] == 51.0
    )


def test_summarise_p99_on_one_to_one_hundred() -> None:
    out = summarise({"jd_extract": _HUNDRED}, errors={}, timeouts={})
    assert out["steps"]["jd_extract"]["p99"] >= 99.0


def test_summarise_max_and_count() -> None:
    out = summarise({"jd_extract": _HUNDRED}, errors={}, timeouts={})
    assert out["steps"]["jd_extract"]["max"] == 100.0
    assert out["steps"]["jd_extract"]["count"] == 100


def test_summarise_single_sample_all_percentiles_equal_the_sample() -> None:
    out = summarise({"rank": [42.0]}, errors={}, timeouts={})
    step = out["steps"]["rank"]
    assert step["p50"] == step["p95"] == step["p99"] == step["max"] == 42.0
    assert step["count"] == 1


def test_summarise_multiple_steps_each_get_their_own_stats() -> None:
    out = summarise(
        {"jd_extract": [10.0, 20.0], "resume_upload": [100.0, 200.0, 300.0]},
        errors={},
        timeouts={},
    )
    assert set(out["steps"]) == {"jd_extract", "resume_upload"}
    assert out["steps"]["resume_upload"]["count"] == 3


# ── verdict ──────────────────────────────────────────────────────────────


def test_verdict_is_pass_on_clean_run() -> None:
    out = summarise({"jd_extract": [10.0, 20.0]}, errors={}, timeouts={})
    assert out["verdict"] == "PASS"


def test_verdict_is_fail_on_any_error() -> None:
    out = summarise({"jd_extract": [10.0, 20.0]}, errors={"jd_extract": 1}, timeouts={})
    assert out["verdict"] == "FAIL"


def test_verdict_is_fail_on_any_timeout() -> None:
    out = summarise(
        {"jd_extract": [10.0, 20.0]}, errors={}, timeouts={"resume_upload": 1}
    )
    assert out["verdict"] == "FAIL"


def test_verdict_is_fail_on_an_empty_step_that_was_expected() -> None:
    """A step with zero samples means the run never got there at all — that
    is worse than a slow step, not a step to silently omit from the report."""
    out = summarise({"jd_extract": [10.0, 20.0], "rank": []}, errors={}, timeouts={})
    assert out["verdict"] == "FAIL"


def test_verdict_pass_requires_zero_errors_and_zero_timeouts() -> None:
    out = summarise(
        {"jd_extract": [10.0]},
        errors={"jd_extract": 0},
        timeouts={"jd_extract": 0},
    )
    assert out["verdict"] == "PASS"


def test_summarise_reports_totals() -> None:
    out = summarise(
        {"a": [1.0, 2.0], "b": [3.0]},
        errors={"a": 1},
        timeouts={"b": 2},
    )
    assert out["total_errors"] == 1
    assert out["total_timeouts"] == 2
    assert out["total_samples"] == 3


# ── render_markdown ──────────────────────────────────────────────────────


def test_render_markdown_contains_a_row_per_step() -> None:
    out = summarise(
        {"jd_extract": [10.0, 20.0], "resume_upload": [100.0]}, errors={}, timeouts={}
    )
    md = render_markdown(out)
    assert "jd_extract" in md
    assert "resume_upload" in md


def test_render_markdown_contains_the_verdict() -> None:
    out = summarise({"jd_extract": [10.0]}, errors={}, timeouts={})
    md = render_markdown(out)
    assert "PASS" in md


def test_render_markdown_contains_the_verdict_fail() -> None:
    out = summarise({"jd_extract": [10.0]}, errors={"jd_extract": 1}, timeouts={})
    md = render_markdown(out)
    assert "FAIL" in md


def test_render_markdown_is_a_str() -> None:
    out = summarise({"jd_extract": [10.0]}, errors={}, timeouts={})
    assert isinstance(render_markdown(out), str)
