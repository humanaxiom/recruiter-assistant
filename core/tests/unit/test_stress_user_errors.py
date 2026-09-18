"""Pins that a dead virtual user in ``tests/e2e/stress.py`` flips the
stress-run verdict to FAIL, rather than being silently swallowed by
``asyncio.gather(..., return_exceptions=True)``.

The regression this closes: a user that raises OUTSIDE any ``_timed`` step
(a missing page token before the first timed call, a bad fixture pick) was
only ever printed to stderr — nothing recorded it as an error, so
``summarise()`` saw only the steps that DID complete and reported a clean
PASS with exit 0 for a run that never finished. Verified live against the
running stress stack on 2026-09-17: a dead-early user produced
``Verdict: PASS`` and ``exit 0``.
"""

from __future__ import annotations

from tests.e2e.report import summarise
from tests.e2e.stress import StepError, _user_run_errors


def test_no_dead_users_yields_no_errors() -> None:
    assert _user_run_errors([None, None, None]) == {}


def test_one_dead_user_is_counted() -> None:
    assert _user_run_errors([None, StepError("boom"), None]) == {"user_run": 1}


def test_every_user_dead_is_counted() -> None:
    assert _user_run_errors([RuntimeError("x"), StepError("y")]) == {"user_run": 2}


def test_a_dead_user_flips_the_verdict_to_fail() -> None:
    """The wiring this whole module exists to pin: a step that DID complete
    (so `steps` has samples) plus one dead user still FAILs the run."""
    results = [None, StepError("dead before the first _timed call")]
    errors = _user_run_errors(results)
    summary = summarise({"page_token": [10.0, 12.0]}, errors=errors, timeouts={})
    assert summary["verdict"] == "FAIL"
    assert summary["total_errors"] == 1


def test_no_dead_users_and_clean_steps_stays_pass() -> None:
    results: list[object] = [None, None]
    errors = _user_run_errors(results)
    summary = summarise({"page_token": [10.0]}, errors=errors, timeouts={})
    assert summary["verdict"] == "PASS"
