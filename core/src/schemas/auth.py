"""Pydantic schemas for the CAS identity/session domain (FU-5 slice 5,
ADR-019 §10).

Two shapes, mirroring the ``users`` / ``sessions`` DDL in
``src/models/ddl.py`` column-for-column:

* ``User`` — ``role`` is a plain string column here (NOT the hris
  ``user_roles`` join-table tuple), and this schema carries ``active`` /
  ``last_seen_at`` in place of hris's ``status`` / ``last_login_at``.
* ``Session`` — ``id`` is the opaque ``secrets.token_urlsafe(32)`` cookie
  value (TEXT primary key, not UUID). ``ip_addr`` is modelled as
  ``str | None`` even though the column is Postgres ``INET`` — the service
  layer casts with ``::text`` on the way out so this schema never needs a
  Postgres-specific type.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class User(BaseModel):
    """One row of ``users``."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    cas_username: str
    display_name: str | None
    email: str | None
    role: str
    active: bool
    created_at: dt.datetime
    last_seen_at: dt.datetime


class Session(BaseModel):
    """One row of ``sessions``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: UUID
    expires_at: dt.datetime
    revoked_at: dt.datetime | None
    user_agent: str | None
    ip_addr: str | None
    created_at: dt.datetime
