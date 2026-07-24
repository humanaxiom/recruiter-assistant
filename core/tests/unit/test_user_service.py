"""Unit tests for ``src.services.user_service`` (FU-5 slice 5, ADR-019 §10
step 3, §10a).

Nothing here exists yet (``core/src/services/user_service.py``,
``core/src/schemas/auth.py``), so every test is expected to FAIL (RED) as an
``ImportError``/``ModuleNotFoundError`` at collection.

Deliberately narrow scope, per CLAUDE.md's ``offline`` caveat: whether the
``INSERT ... ON CONFLICT (cas_username) DO UPDATE`` actually upserts, whether
a second login truly leaves ``role`` untouched, and whether concurrent first
logins collapse to one row are all real-Postgres behaviour a mocked
connection cannot prove — that is
``tests/integration/test_session_user_service_pg.py``'s job.

What IS genuinely unit-provable without a database is the pure Python
decision of *which role string* ``provision_or_get`` computes before it ever
talks to Postgres — ADR-019 §10a's default-admin comparison
(``cas_username == default_admin_cas_username``) — captured via the bound
call args rather than by asserting on SQL text, plus the signature-level
contract that ``default_admin_cas_username`` must be passed in explicitly
(CLAUDE.md: "Config only via settings.py — never scattered"; this service
must not reach into settings itself).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from src.services import user_service

DEFAULT_ADMIN = "asalah"


class _NullAsyncCtx:
    async def __aenter__(self) -> _NullAsyncCtx:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    def __init__(self, fetchrow_row: Callable[..., dict[str, Any]]) -> None:
        self._fetchrow_row = fetchrow_row
        self.fetchrow_calls: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        self.fetchrow_calls.append(args)
        return self._fetchrow_row(*args)

    async def execute(self, query: str, *args: Any) -> str:
        return "OK"

    def transaction(self) -> _NullAsyncCtx:
        return _NullAsyncCtx()


def _build_row(username: str, *args: Any) -> dict[str, Any]:
    """Echoes the computed role back, located by value rather than by a
    hard-coded argument position — this test cares which role string
    ``provision_or_get`` decided on, not the shape of its SQL."""
    role = next((a for a in args if a in ("admin", "recruiter")), None)
    assert role is not None, f"no role value found among bind args: {args}"
    now = datetime.now(UTC)
    return {
        "id": uuid4(),
        "cas_username": username,
        "display_name": None,
        "email": None,
        "role": role,
        "active": True,
        "created_at": now,
        "last_seen_at": now,
    }


# ── default-admin role selection (ADR-019 §10a) — pure decision logic ──────


@pytest.mark.asyncio
async def test_admin_role_selected_for_default_admin_username() -> None:
    username = DEFAULT_ADMIN
    conn = _FakeConn(lambda *args: _build_row(username, *args))

    user = await user_service.provision_or_get(
        conn, cas_username=username, default_admin_cas_username=DEFAULT_ADMIN
    )

    assert user.role == "admin"


@pytest.mark.asyncio
async def test_recruiter_role_selected_for_non_default_username() -> None:
    username = "someone-else"
    conn = _FakeConn(lambda *args: _build_row(username, *args))

    user = await user_service.provision_or_get(
        conn, cas_username=username, default_admin_cas_username=DEFAULT_ADMIN
    )

    assert user.role == "recruiter"


@pytest.mark.asyncio
async def test_default_admin_match_is_case_sensitive() -> None:
    """``"Asalah" != "asalah"`` — no case-folding surprise that would widen
    the admin allowlist beyond the exact configured username."""
    username = "Asalah"
    conn = _FakeConn(lambda *args: _build_row(username, *args))

    user = await user_service.provision_or_get(
        conn, cas_username=username, default_admin_cas_username=DEFAULT_ADMIN
    )

    assert user.role == "recruiter"


@pytest.mark.asyncio
async def test_default_admin_username_is_parameter_driven_not_hardcoded() -> None:
    """Changing the configured default-admin value (as if from settings)
    must change who gets promoted — proves the comparison uses the passed-in
    parameter, not a literal baked into the service."""
    username = "custom-admin-id"
    conn = _FakeConn(lambda *args: _build_row(username, *args))

    user = await user_service.provision_or_get(
        conn, cas_username=username, default_admin_cas_username="custom-admin-id"
    )

    assert user.role == "admin"


# ── signature contract: no scattered config ─────────────────────────────────


def test_provision_or_get_requires_default_admin_cas_username_kwarg() -> None:
    """Must NOT read ``settings`` internally — the caller is required to
    pass ``default_admin_cas_username`` in. Omitting it is a TypeError at
    call time, not a silently-applied internal default."""
    conn = _FakeConn(lambda *args: _build_row("whoever", *args))

    with pytest.raises(TypeError):
        user_service.provision_or_get(conn, cas_username="whoever")  # type: ignore[call-arg]
