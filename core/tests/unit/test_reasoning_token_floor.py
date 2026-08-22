"""RED pin — every JSON extraction call must clear the reasoning-model floor.

**The incident.** On 2026-08-21 the first real smoke run uploaded three résumés
and *none* of them got LLM skill extraction: two fell back to the keyword scan
(``degraded``) and one failed outright, with
``parse_resume.skills_llm_failed ... response content was empty (possibly
reasoning model exhausted token budget); reasoning_present=True``. Degraded
résumés are excluded from shortlisting (ADR-030), so the shortlist came back
empty — **the ranking pipeline could not rank anybody.**

The cause was a number. ``resume_skills_v2`` was called with
``max_tokens=1536``. On ``gpt-oss:20b`` the DISCARDED reasoning trace counts
against ``max_tokens`` before a single byte of JSON is emitted (ADR-021 §6), so
the budget was gone before the answer started.

**This was already known and already written down.** ADR-044 / PR #94 hit the
identical failure on the skill classifier, proved live that 1024 classified 0 of
6 skills while 4096 classified 6 of 6, and left a comment saying "do not
optimise this back toward 1024 — that value was proven live to zero out the
feature". The lesson was recorded against ONE call site while four others kept
their own smaller literals, and the most important of them — the extraction the
entire product depends on — was one of those four.

That is this repo's signature defect (ROADMAP A7): a true, hard-won invariant
living in a comment, with nothing enforcing it anywhere else. So this file
enforces it structurally rather than trusting the next author to have read
ADR-044.

**Why a source scan and not a call assertion.** The failure mode is a NEW call
site added later with a hand-picked literal, which no per-call test would cover
because nobody writes a test for the call they forgot to think about. The scan
sees every call, including ones that do not exist yet.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.pipeline.llm.client import REASONING_JSON_MIN_TOKENS

_SRC = Path(__file__).resolve().parents[2] / "src"
_PROFILE_DIR = Path(__file__).resolve().parents[3] / "docs" / "model-profiles"

#: The extraction paths whose output the product cannot work without. A short
#: response is not the point — the reasoning trace is charged first regardless
#: of how small the answer is.
_MUST_CLEAR_FLOOR = (
    "worker/resume_tasks.py",
    "worker/tasks.py",
    "pipeline/skill_classifier.py",
)

#: Known below-floor call site, deliberately NOT fixed on this branch and
#: recorded in docs/ROADMAP.md instead. `skills_graph`'s vocabulary tiebreaker
#: asks for a single token and HAS a deterministic fallback, so an empty
#: response degrades resolution rather than emptying a shortlist. Raising it
#: changes canonical-key resolution, which is a scoring-path change and does not
#: belong in an incident fix. Listed here so it is a DECISION with a reason
#: attached, not an oversight this file silently tolerates.
_RECORDED_EXCEPTIONS = {"pipeline/skills_graph.py"}


def _call_sites() -> list[tuple[str, int, int]]:
    """Every literal ``max_tokens=<int>`` under ``src/``, as (path, line, n)."""
    found: list[tuple[str, int, int]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = re.search(r"\bmax_tokens\s*=\s*(\d+)\b", line)
            if match:
                found.append((rel, lineno, int(match.group(1))))
    return found


def test_the_floor_covers_every_accepted_model_profile() -> None:
    """**This assertion used to read ``== 4096``, and that was the bug.**

    4096 came from ADR-044, measured against ONE prompt (the classifier). On
    2026-08-22 `scripts/model-check.sh` probed every real prompt on an idle peer
    and found `resume_skills_v2` — the extraction the whole product depends on —
    managed only 2 of 4 concurrent calls at 4096, and 4 of 4 at 8192. A constant
    pinned to a number measured elsewhere was still too low for the call that
    mattered, and a test asserting that exact number made it *harder* to
    correct, not easier.

    So the floor is now tied to the MEASUREMENTS rather than to a literal: it
    must cover the largest ``recommended_max_tokens`` of every accepted profile
    in ``docs/model-profiles/``. Point the stack at a hungrier model, run
    `model-check.sh`, commit its profile, and this fails until the floor is
    raised to match — which is the coupling ADR-045 exists to enforce.
    """
    profiles = sorted(_PROFILE_DIR.glob("*.json"))
    assert profiles, (
        "no committed model profiles — run scripts/model-check.sh and commit "
        "what it writes; the floor is meaningless without a measurement"
    )
    for path in profiles:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("accepted"):
            continue
        needed = int(data.get("recommended_max_tokens") or 0)
        assert REASONING_JSON_MIN_TOKENS >= needed, (
            f"{path.name} measured a need for {needed} tokens but the shared "
            f"floor is {REASONING_JSON_MIN_TOKENS}. Raise the floor: a budget "
            "below the measured requirement does not truncate the answer, it "
            "returns an empty one."
        )


@pytest.mark.parametrize("module", _MUST_CLEAR_FLOOR)
def test_no_extraction_call_sits_below_the_reasoning_floor(module: str) -> None:
    below = [
        (path, line, n)
        for path, line, n in _call_sites()
        if path == module and n < REASONING_JSON_MIN_TOKENS
    ]
    assert not below, (
        f"{module} has a max_tokens literal below the proven floor of "
        f"{REASONING_JSON_MIN_TOKENS}: {below}. On gpt-oss:20b the discarded "
        "reasoning trace is charged against this budget BEFORE any JSON is "
        "emitted, so a smaller value does not truncate the answer — it returns "
        "an empty one. This exact number emptied every shortlist on 2026-08-21."
    )


def test_every_below_floor_call_site_is_a_recorded_decision() -> None:
    """The scan must not quietly grow exceptions. Anything below the floor is
    either fixed or listed above with a reason — never merely present."""
    offenders = {
        path
        for path, _line, n in _call_sites()
        if n < REASONING_JSON_MIN_TOKENS and path not in _RECORDED_EXCEPTIONS
    }
    # `client.py`'s own default is the signature default, not a call site.
    offenders.discard("pipeline/llm/client.py")
    assert not offenders, (
        f"new below-floor LLM call sites appeared: {sorted(offenders)}. Either "
        "raise them to REASONING_JSON_MIN_TOKENS or record why they are safe."
    )
