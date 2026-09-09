"""RED — the stage-3 evidence budget must clear the model's measured floor.

**Reported from the running product on 2026-09-09: "Generate shortlist has not
been producing anything."** The run had been retrying for hours behind
"the ranking model was briefly unavailable… no action needed".

It was not unavailable. ``match_evidence_max_tokens`` was **2048**, and
``gpt-oss:20b`` on this deployment's transport (OpenAI-compatible,
unconstrained, reasoning channel on) spends that entire budget reasoning and
returns empty ``content``. Stage 3 fails CLOSED (ADR-029), so one starved
candidate withholds the whole shortlist.

**Measured, at the real concurrency, on the real failing job** — the first two
attempts at this measurement each called ONE prompt at a time and both
SUCCEEDED at 2048 (205s for a 5-chunk résumé, 303s for a 15-chunk one), which
nearly certified the broken configuration as healthy. Concurrency was the
missing variable, exactly as the committed profile warns ("a single uncontended
call took ~35s while four concurrent ones blew a 300s timeout"):

    2048 tokens, 4 concurrent:  15 chunks OK 297s · 12 chunks FAIL ·
                                11 chunks OK 315s ·  9 chunks FAIL
    4096 tokens, 4 concurrent:  all four OK, 219-273s

Note the failures are NOT size-ordered: the largest résumé passed while smaller
ones failed. 2048 sits close enough to the edge that reasoning length tips some
inputs over and not others, which is why this looked intermittent rather than
broken and why casual testing passed.

**Why the default lands at 8192 rather than the measured 4096.** Four samples
is a thin basis for a floor when the failure is input-dependent — 2048 passed
two of those same four. ``max_tokens`` is a CEILING, not a target: a call that
finishes at 3000 tokens costs the same whether the cap is 4096 or 8192, so the
margin is free on every healthy call and only engages where 4096 would have
failed outright. And the cost of being wrong is asymmetric: ADR-029 makes this
all-or-nothing, so one under-budgeted candidate loses the entire shortlist.
8192 is also ``REASONING_JSON_MIN_TOKENS``, the value this codebase already
uses for "reasoning model, JSON out", and the committed profile's own
``recommended_max_tokens``.
"""

from __future__ import annotations

import pytest

#: The measured floor: the smallest budget at which all four concurrent
#: evidence calls returned schema-valid JSON. Recorded so a future change that
#: LOWERS the budget has to argue with a number rather than a preference.
MEASURED_FLOOR = 4096


def test_the_evidence_budget_clears_the_measured_floor() -> None:
    from src.settings import Settings

    budget = Settings().match_evidence_max_tokens
    assert budget >= MEASURED_FLOOR, (
        f"match_evidence_max_tokens={budget} is at or below the budget that "
        f"returned EMPTY content for 2 of 4 concurrent evidence calls on "
        f"gpt-oss:20b. Stage 3 fails closed (ADR-029), so this does not "
        f"degrade one candidate — it withholds the whole shortlist."
    )


def test_the_orchestrator_literal_matches_the_setting() -> None:
    """``_EVIDENCE_MAX_TOKENS`` is the fallback for a ``MatchingContext`` built
    without settings (tests, direct callers). Its own comment says it is "kept
    equal to the Settings defaults" — an invariant that was, until this file,
    stated in prose with nothing enforcing it."""
    from src.pipeline.matching.orchestrator import _EVIDENCE_MAX_TOKENS
    from src.settings import Settings

    assert _EVIDENCE_MAX_TOKENS == Settings().match_evidence_max_tokens


def test_the_evidence_budget_is_not_below_the_other_json_prompts() -> None:
    """Every OTHER reasoning-model JSON call site in this codebase budgets
    ``REASONING_JSON_MIN_TOKENS``. Evidence was the one that did not, and it is
    the one that broke. The floor is per-PROMPT and must stay measured — this
    asserts the evidence prompt is not the odd one out again."""
    from src.pipeline.llm import REASONING_JSON_MIN_TOKENS
    from src.settings import Settings

    assert Settings().match_evidence_max_tokens >= REASONING_JSON_MIN_TOKENS


@pytest.mark.parametrize("budget", [512, 1024, 2048])
def test_the_guard_would_have_caught_the_shipped_value(budget: int) -> None:
    """The assertion above is only worth having if it FAILS on what shipped.
    2048 is the value that was live when a user reported the shortlist
    producing nothing."""
    assert budget < MEASURED_FLOOR
