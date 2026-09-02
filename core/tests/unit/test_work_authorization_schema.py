"""RED — Canadian work authorization (sponsor 2026-09-02 §O2, answer 4).

The sponsor's answer, verbatim: *"Last but visible. All candidates listed and
marked. All other metrics are invalidated though, if candidate has no permit."*

Four contracts follow from that sentence, and each is pinned below:

1. **Three states, and ``unknown`` is not ``not_eligible``.** This is an
   automated adverse decision on an attribute adjacent to national origin and
   immigration status — protected grounds under the BC Human Rights Code.
   Reading *absence of a declaration* as a negative is what turns a lawful
   bona-fide screen into discrimination. ``unknown`` is the default and must
   never band.
2. **Recruiter-declared, never inferred.** The candidate CSV that will
   eventually carry the candidate's own declaration is TBD (sponsor answer 1),
   so v1 is a recruiter-set field. There is deliberately NO code path that
   asks the LLM to guess this from résumé text.
3. **Banded last, still listed, still marked** — never hidden, never deleted.
   A human must be able to see and reverse the decision.
4. **The other metrics are invalidated, not merely deprioritised.** A rank and
   a 78% shown beside "no work permit" is a number the product cannot stand
   behind; ``metrics_invalidated`` says so explicitly rather than leaving the
   reader to infer it.

These contracts do not exist yet — this is the RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError

import src.schemas as schemas_pkg
from src.schemas.matching import ScoreBreakdown, ShortlistEntry
from src.schemas.resumes import (
    ResumeListItem,
    WorkAuthorization,
    WorkAuthorizationRequest,
)

_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)


# ------------------------------------------------------------- the three states


def test_work_authorization_has_exactly_three_states() -> None:
    """A boolean would collapse "did not say" into "not eligible" — the single
    most consequential modelling mistake available in this feature."""
    from typing import get_args

    assert set(get_args(WorkAuthorization)) == {"eligible", "not_eligible", "unknown"}


def test_work_authorization_is_exported_from_the_schemas_package() -> None:
    assert schemas_pkg.WorkAuthorization is WorkAuthorization
    assert "WorkAuthorization" in schemas_pkg.__all__
    assert "WorkAuthorizationRequest" in schemas_pkg.__all__


# ------------------------------------------------------------ the request body


def test_the_request_body_accepts_each_state_and_an_optional_note() -> None:
    for state in ("eligible", "not_eligible", "unknown"):
        req = WorkAuthorizationRequest(status=state)  # type: ignore[arg-type]
        assert req.status == state
        assert req.note is None
    assert WorkAuthorizationRequest(status="eligible", note="Confirmed PR card").note


def test_the_request_body_rejects_an_unknown_state() -> None:
    with pytest.raises(ValidationError):
        WorkAuthorizationRequest(status="maybe")  # type: ignore[arg-type]


def test_the_request_body_caps_the_note() -> None:
    """The note rides verbatim into the audit row's ``details`` JSONB, exactly
    like ``JobAssigneeCreate.note`` — an unbounded value is an at-rest
    storage-growth vector."""
    with pytest.raises(ValidationError):
        WorkAuthorizationRequest(status="not_eligible", note="x" * 501)


# --------------------------------------------------------------- résumé rows


def test_resume_list_item_defaults_to_unknown() -> None:
    """Every row that predates this feature reads back ``unknown`` — never
    ``not_eligible``. This is contract 1 at the read boundary."""
    row = ResumeListItem(
        id=uuid4(),
        original_filename="a.pdf",
        status="parsed",
        uploaded_at=_TS,
        parsed_at=_TS,
    )
    assert row.work_authorization == "unknown"


def test_resume_list_item_carries_a_declared_state() -> None:
    row = ResumeListItem(
        id=uuid4(),
        original_filename="a.pdf",
        status="parsed",
        uploaded_at=_TS,
        parsed_at=_TS,
        work_authorization="not_eligible",
    )
    assert row.work_authorization == "not_eligible"


# ------------------------------------------------------------ shortlist entry


def _entry(**kw: object) -> ShortlistEntry:
    base: dict[str, object] = {
        "id": uuid4(),
        "job_id": uuid4(),
        "resume_id": uuid4(),
        "rank": 3,
        "score_final": 0.78,
        "score_breakdown": ScoreBreakdown(
            skill=0.8,
            experience=0.7,
            education=0.6,
            seniority=0.5,
            vector=0.9,
            structured=0.75,
        ),
        "evidence": None,
        "generated_at": _TS,
    }
    base.update(kw)
    return ShortlistEntry(**base)  # type: ignore[arg-type]


def test_shortlist_entry_defaults_to_unknown_and_valid_metrics() -> None:
    entry = _entry()
    assert entry.work_authorization == "unknown"
    assert entry.metrics_invalidated is False


def test_an_unknown_declaration_never_invalidates_metrics() -> None:
    """Contract 1, at the surface where the consequence actually lands. This is
    the invariant the mutation probe targets."""
    assert _entry(work_authorization="unknown").metrics_invalidated is False


def test_an_eligible_declaration_never_invalidates_metrics() -> None:
    assert _entry(work_authorization="eligible").metrics_invalidated is False


def test_not_eligible_invalidates_the_metrics() -> None:
    """Sponsor answer 4: "All other metrics are invalidated ... if candidate
    has no permit." The scores are still CARRIED (a human may want to see what
    the engine thought) but the entry states that they do not stand."""
    entry = _entry(work_authorization="not_eligible")
    assert entry.metrics_invalidated is True
    assert entry.score_final == pytest.approx(0.78)


def test_metrics_invalidated_is_derived_not_supplied() -> None:
    """It must be impossible to persist an entry that says "no work permit" and
    "metrics valid" at the same time, or vice versa. Two fields that can
    disagree is how this repo has shipped defects before — the flag is computed
    from the declaration, never accepted from a caller."""
    lied_low = _entry(work_authorization="not_eligible", metrics_invalidated=False)
    lied_high = _entry(work_authorization="eligible", metrics_invalidated=True)
    assert lied_low.metrics_invalidated is True
    assert lied_high.metrics_invalidated is False
