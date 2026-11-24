"""Wire schemas for the human decision record.

A decision is a human accept / reject / hold on one candidate for one job,
with a mandatory written rationale. It never changes rank or score; it is an
append-only record next to the system's ranking.

``DecisionIn`` carries only the decision and rationale -- who decided comes
from the session (``extra="forbid"`` turns a client-supplied ``decided_by``
into a 422).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

DecisionValue = Literal["accept", "reject", "hold"]

RATIONALE_MIN = 10
RATIONALE_MAX = 1000

# C0 controls and DEL, except TAB (0x09) and LF (0x0a), which carry real
# formatting in a multi-line rationale. NUL in particular would be rejected by
# Postgres at INSERT time.
_FORBIDDEN_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: DecisionValue
    rationale: str = Field(min_length=RATIONALE_MIN, max_length=RATIONALE_MAX)

    @field_validator("rationale", mode="before")
    @classmethod
    def _strip_and_screen(cls, value: object) -> object:
        # Strip BEFORE the length constraints so padding cannot satisfy them.
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if _FORBIDDEN_CONTROL.search(stripped):
            raise ValueError("rationale contains control characters")
        return stripped


class DecisionOut(BaseModel):
    id: UUID
    job_id: UUID
    resume_id: UUID
    decision: DecisionValue
    rationale: str
    decided_by: str
    decided_by_role: str
    decided_at: dt.datetime
    snap_rank: int | None = None
    snap_score_final: float | None = None
    snap_blind_review: bool = False


class DecisionBadge(BaseModel):
    """The shortlist-card view: no rationale."""

    resume_id: UUID
    decision: DecisionValue
    decided_by: str
    decided_at: dt.datetime


class DecisionHistory(BaseModel):
    latest: DecisionOut | None = None
    history: list[DecisionOut]
