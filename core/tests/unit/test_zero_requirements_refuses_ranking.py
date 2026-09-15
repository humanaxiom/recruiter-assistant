"""RED pin — pilot defect 2026-09-10: a job whose JD parse extracted ZERO
``required_skills`` AND ZERO ``nice_to_have_skills`` could still be ranked
(``POST /jobs/{id}/shortlist`` -> 202), producing a meaningless shortlist with
no disclosure to the recruiter. Mirrors ADR-017 decision 1's "refuse rather
than silently degrade" shape and the ADR-040/041 disclosure line.

``src.services.shortlist_service.assert_job_has_requirements`` does not exist
yet, and ``generate_shortlist`` does not call it — every test below fails
either at import/attribute error or because the route still enqueues on an
all-zero JD. RED half of the TDD cycle.

Harness modelled EXACTLY on ``test_route_shortlist.py``'s ``_mock_conn`` /
``_build_app`` / ``_client`` (~lines 207-247). The existing
``test_generate_shortlist_returns_202_and_enqueues`` uses a bare
``_mock_conn()`` (``fetchrow`` returns ``None`` by default) and already pins
"nonexistent job still 202" — this file must not weaken or duplicate that,
only add the zero-requirements branch alongside it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import shortlist as shortlist_routes
from src.errors import AppError, ConflictError
from src.models.pool import get_db
from src.services import shortlist_service


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(
    *,
    fetchrow: _Row | None = None,
    fetch: list[_Row] | None = None,
    fetchval: Any = None,
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _build_app(
    conn: MagicMock, *, arq: MagicMock | None = None, role: Role = Role.ADMIN
) -> FastAPI:
    app = FastAPI()
    app.include_router(shortlist_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── route: 409 refuses ranking when both counts are zero ───────────────────


@pytest.mark.asyncio
async def test_generate_shortlist_409s_when_both_requirement_counts_are_zero() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 0, "nice_count": 0}))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "resource.conflict"
    arq.enqueue_job.assert_not_awaited()
    # The ranking-state write must never fire on the refused path — the
    # existing SQL constant this route uses BEFORE it used to enqueue.
    executed_sql = [call.args[0] for call in conn.execute.await_args_list]
    assert not any(
        "shortlist_state = 'ranking'" in sql for sql in executed_sql
    ), "a 409 refusal must not have set shortlist_state='ranking' first"


@pytest.mark.asyncio
async def test_generate_shortlist_409_message_names_the_cause() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 0, "nice_count": 0}))
    app = _build_app(conn)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 409
    detail = resp.json()["detail"].lower()
    assert "requirement" in detail or "skill" in detail


@pytest.mark.asyncio
async def test_generate_shortlist_409s_when_description_parsed_is_null() -> None:
    """A never-parsed JD: ``description_parsed`` is NULL, so
    ``jsonb_array_length(NULL -> 'required_skills')`` reads back as the
    ``coalesce``d zero on BOTH sides -- same refusal as a parsed-but-empty
    JD, not a silent 202."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 0, "nice_count": 0}))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 409
    arq.enqueue_job.assert_not_awaited()


# ── route: 202 proceeds whenever at least one list is non-empty ────────────


@pytest.mark.asyncio
async def test_generate_shortlist_202s_when_only_nice_to_have_present() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 0, "nice_count": 3}))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("shortlist_job", str(job_id))


@pytest.mark.asyncio
async def test_generate_shortlist_202s_when_only_required_present() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 5, "nice_count": 0}))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("shortlist_job", str(job_id))


# ── route: a nonexistent job (no row) still 202s -- unchanged behaviour ────


@pytest.mark.asyncio
async def test_generate_shortlist_202s_when_job_does_not_exist() -> None:
    """The guard's ``fetchrow`` returning ``None`` must return SILENTLY (the
    pre-existing behaviour pinned at ``test_route_shortlist.py``'s
    ``test_generate_shortlist_returns_202_and_enqueues``, which also uses a
    bare ``_mock_conn()``) -- a nonexistent job is not this guard's problem to
    diagnose, and must not become a new 404/409 here."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=None)
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)

    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")

    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("shortlist_job", str(job_id))


# ── direct unit test of the guard function itself ───────────────────────────


@pytest.mark.asyncio
async def test_assert_job_has_requirements_raises_conflict_on_all_zero() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 0, "nice_count": 0}))

    with pytest.raises(ConflictError):
        await shortlist_service.assert_job_has_requirements(conn, job_id)


@pytest.mark.asyncio
async def test_assert_job_has_requirements_raises_conflict_status_409() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 0, "nice_count": 0}))

    with pytest.raises(ConflictError) as exc_info:
        await shortlist_service.assert_job_has_requirements(conn, job_id)
    assert exc_info.value.status == 409
    assert exc_info.value.code == "resource.conflict"


@pytest.mark.asyncio
async def test_assert_job_has_requirements_passes_when_required_present() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 2, "nice_count": 0}))

    # Must not raise.
    await shortlist_service.assert_job_has_requirements(conn, job_id)


@pytest.mark.asyncio
async def test_assert_job_has_requirements_passes_when_nice_to_have_present() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 0, "nice_count": 1}))

    await shortlist_service.assert_job_has_requirements(conn, job_id)


@pytest.mark.asyncio
async def test_assert_job_has_requirements_returns_silently_for_missing_job() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=None)

    # Must not raise -- a nonexistent job is not this guard's problem.
    await shortlist_service.assert_job_has_requirements(conn, job_id)


@pytest.mark.asyncio
async def test_assert_job_has_requirements_queries_by_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural pin: the guard's ``fetchrow`` call is keyed on ``job_id``,
    querying ``jobs`` -- not some other table -- so a coder cannot satisfy the
    other tests with an unrelated query."""
    job_id: UUID = uuid4()
    conn = _mock_conn(fetchrow=_Row({"required_count": 1, "nice_count": 0}))

    await shortlist_service.assert_job_has_requirements(conn, job_id)

    conn.fetchrow.assert_awaited_once()
    call = conn.fetchrow.await_args
    assert job_id in call.args
    sql = call.args[0]
    assert "jobs" in sql.lower()
    assert "required_skills" in sql
    assert "nice_to_have_skills" in sql
