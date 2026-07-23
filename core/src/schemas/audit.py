"""Pydantic schema for the FU-5 slice 10 legacy reveal-audit viewer
(``GET /audit/reveals-legacy``, ADR-019 §6 / §9.4).

``RevealAuditItem`` carries ONLY ``reveal_audit``'s own columns
(id/resume_id/job_id/actor/context/revealed_at) — the route this backs must
never join against ``resumes``, so there is no PII field here at all, not
even an optional one.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RevealAuditItem(BaseModel):
    """One row of ``reveal_audit``, verbatim."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    resume_id: UUID
    job_id: UUID | None
    actor: str | None
    context: str | None
    revealed_at: dt.datetime


__all__ = ["RevealAuditItem"]
