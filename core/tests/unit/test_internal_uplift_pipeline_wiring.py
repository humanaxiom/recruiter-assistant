"""Regression pins — the internal-uplift disclosure chain, from ``resumes
.internal_apsa``/``internal_cupe`` to a rendered shortlist card, has FOUR
links and (per a 2026-09-09 merge-blocking review of this branch)
``grep -rn match_internal_uplift core/tests/`` returned NOTHING: none of the
four had a test that would die if the wiring were deleted, even though the
arithmetic itself (``test_internal_uplift_scoring.py``) and the settings
factory's OTHER tunables (``test_matching_context_settings_wiring.py``) were
both already covered.

    settings.match_internal_uplift
      -> MatchingContext.internal_uplift_amount   (link 1 -- orchestrator.py:217)
      -> PipelineMeta.internal_uplift_amount      (link 2 -- orchestrator.py:1097)
      -> _CombineInput.internal_apsa/internal_cupe (link 3 -- orchestrator.py:1030-1031)
      -> ShortlistEntry.internal_apsa/internal_cupe (link 4 -- shortlist_service.py)

Links 1 and 2 are pinned in ``test_matching_context_settings_wiring.py``
(extended alongside its existing ``git_sha`` end-to-end proof). Link 4 is
pinned in ``test_services_shortlist_read.py``, driven from a STORED ROW's
``score_breakdown`` jsonb rather than a hand-built ``ShortlistEntry`` --
that file's own docstring explains why (the fold/projection landmine).

THIS file is link 3 only: does ``generate_shortlist`` actually carry the
per-candidate flags it already reads in stage 2 into the ``_CombineInput`` it
hands to stage 4, or does it drop them on the floor the way
``manager_prompt`` once did? Mirrors
``test_manager_prompt_pipeline_wiring.py``'s own "link 3: the combine" test
byte-for-byte in technique (source inspection, not a full mocked pipeline
run) -- that file's docstring explains why this is the right layer to pin a
wiring break at: a test that only checks the far end (a fully mocked
stage1/stage2/stage3 run) tells you the chain broke without telling you
WHERE, and this exact class of break has already happened once on this
branch for a sibling field.

All four tests below are REGRESSION PINS, not bug reports: reading
``orchestrator.py``/``shortlist_service.py`` as they stand today, all four
links are ALREADY wired correctly (this branch's own ``green`` commit did
it) -- these tests are expected to PASS ON ARRIVAL. What they add is a
FAILURE the moment any one link regresses, which nothing previously
provided.
"""

from __future__ import annotations

import inspect

from src.pipeline.matching.orchestrator import generate_shortlist


def test_generate_shortlist_carries_internal_flags_into_the_combine_input() -> None:
    """Link 3: ``generate_shortlist`` must build its ``_CombineInput`` list
    with ``internal_apsa=c.internal_apsa`` / ``internal_cupe=c.internal_cupe``
    off the stage-2 candidate, not let either default to ``False`` for every
    candidate on every job.

    Regression pin, passes on arrival: ``orchestrator.py:1030-1031`` already
    does this. Delete those two kwargs and this fails immediately, where
    today nothing would.
    """
    source = inspect.getsource(generate_shortlist)
    assert "internal_apsa=c.internal_apsa" in source, (
        "generate_shortlist builds _CombineInput without internal_apsa — the "
        "flag read off resumes.internal_apsa in stage 2 is dropped on the "
        "floor, so the uplift applies to nobody on the real forward path"
    )
    assert "internal_cupe=c.internal_cupe" in source, (
        "generate_shortlist builds _CombineInput without internal_cupe — the "
        "flag read off resumes.internal_cupe in stage 2 is dropped on the "
        "floor, so the uplift applies to nobody on the real forward path"
    )


def test_generate_shortlist_passes_the_context_uplift_amount_to_stage4_combine() -> (
    None
):
    """A sibling of link 3: even with both flags carried, ``stage4_combine``
    must be called with ``internal_uplift_amount=ctx.internal_uplift_amount``
    -- otherwise every candidate gets ``stage4_combine``'s own hardcoded
    default regardless of what ``MatchingContext`` (and, upstream, Settings)
    actually configured, silently reproducing the exact "configurable in name
    only" defect link 1/2 exist to prevent, one call further downstream.

    Regression pin, passes on arrival: ``orchestrator.py:1036`` already does
    this.
    """
    source = inspect.getsource(generate_shortlist)
    assert "internal_uplift_amount=ctx.internal_uplift_amount" in source, (
        "generate_shortlist calls stage4_combine without threading "
        "ctx.internal_uplift_amount through — every candidate would silently "
        "get stage4_combine's own module-level default uplift instead of the "
        "one Settings configured"
    )
