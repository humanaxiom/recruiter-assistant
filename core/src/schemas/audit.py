"""Pydantic schema for the FU-5 slice 10 legacy reveal-audit viewer
(``GET /audit/reveals-legacy``, ADR-019 §6 / §9.4).

``RevealAuditItem`` carries ONLY ``reveal_audit``'s own columns
(id/resume_id/job_id/actor/context/revealed_at) — the route this backs must
never join against ``resumes``, so there is no PII field here at all, not
even an optional one.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
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


class AuditLogItem(BaseModel):
    """One row of the LIVE ``audit_log`` table, for the auditor viewer
    (Phase 1.4 / ADR-036).

    **Contains no candidate PII, by construction.** The read never joins
    ``resumes`` or ``jobs``, so nothing decrypts; ``subject_id`` is an opaque
    UUID. The single free-text field the table carries — ``details`` — passes
    through ``audit_service.redact_audit_details``, an ALLOWLIST that withholds
    any key not explicitly classified as safe (a résumé withdrawal ``reason`` is
    operator-typed prose about a named candidate, and is withheld).

    ``actor_username`` is resolved through a LEFT JOIN on ``users`` and is
    ``None`` for ``actor_kind='service'`` rows. It is staff identity, not
    candidate identity — naming the human who acted is the entire point of an
    attributable audit trail.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    actor_kind: str
    actor_user_id: UUID | None
    actor_username: str | None
    actor_service: str | None
    action: str
    subject_type: str
    subject_id: UUID
    job_id: UUID | None
    context: str | None
    details: Any
    occurred_at: dt.datetime


__all__ = ["RevealAuditItem", "AuditLogItem"]
