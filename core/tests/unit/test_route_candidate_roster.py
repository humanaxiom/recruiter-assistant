"""RED — ``POST /jobs/{job_id}/candidate-roster`` (Sponsor Requirements PR2
slice 2).

Mirrors ``test_route_resumes_withdraw.py``'s convention: monkeypatch the
SERVICE layer (``bulk_ingest_service.parse_candidate_csv`` /
``candidate_roster_service.reconcile_candidate_roster``) so this file isolates
the ROUTE contract (RBAC, status code, request/response shape) from the
service's own behaviour, which is covered by
``test_candidate_roster_csv.py`` (parsing) and
``test_candidate_roster_reconciliation.py`` (matching/writes).

The route lives beside ``upload_resumes`` in ``src/api/routes/resumes.py``
and reuses the SAME ``_RESUME_WRITERS`` role gate — a roster import is a
batch of the same kind of write (audited screening declarations against real
people), not a read.

None of this exists yet:

* ``src.api.routes.resumes`` has no ``candidate_roster_service`` attribute at
  all yet, so ``monkeypatch.setattr(resumes_routes.candidate_roster_service,
  ...)`` fails with ``AttributeError`` — RED half of the TDD cycle.
* The route itself doesn't exist, so an unmocked request 404s from FastAPI's
  own router.
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

from src.api.deps import Role, resolve_role
from src.api.routes import resumes as resumes_routes
from src.errors import AppError
from src.models.pool import get_db
from src.services.bulk_ingest_service import CandidateRosterRow, ManifestError

_NON_WRITER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)

# Well above bulk_ingest_service._MAX_MANIFEST_BYTES (1 MiB) — the route must
# reject this before it ever reaches the reconciler.
_OVERSIZE_CSV = b"Name\n" + b"a\n" * (1024 * 1024)

_SMALL_CSV = (
    b'Name,Email,"Work Authorization"\n'
    b'"Smith, Jordan",jordan@example.invalid,"No Restrictions"\n'
)


def _mock_conn() -> MagicMock:
    conn = MagicMock(name="conn")
    conn.execute = AsyncMock(return_value="OK")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=uuid4())
    return conn


def _build_app(conn: MagicMock, *, role: Role = Role.ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _dummy_report() -> Any:
    from src.services.candidate_roster_service import RosterReconciliationReport

    return RosterReconciliationReport(
        matched=1,
        work_authorization_changed=1,
        work_authorization_unchanged=0,
        internal_apsa_changed=0,
        internal_apsa_unchanged=1,
        internal_cupe_changed=0,
        internal_cupe_unchanged=1,
        unmatched_csv_rows=[],
        unmatched_resumes=[],
        ambiguous_name_matches=[],
        conflicting=[],
        unrecognised_work_authorization_source=[],
    )


async def _post_csv(client: AsyncClient, job_id: UUID, blob: bytes = _SMALL_CSV) -> Any:
    return await client.post(
        f"/jobs/{job_id}/candidate-roster",
        files={"file": ("roster.csv", blob, "text/csv")},
    )


# ── role gate ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_can_upload_the_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.bulk_ingest_service,
        "parse_candidate_csv",
        MagicMock(return_value=[]),
    )
    reconcile = AsyncMock(return_value=_dummy_report())
    monkeypatch.setattr(
        resumes_routes.candidate_roster_service,
        "reconcile_candidate_roster",
        reconcile,
    )

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await _post_csv(client, job_id)

    assert resp.status_code == 200
    reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_recruiter_can_upload_the_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.bulk_ingest_service,
        "parse_candidate_csv",
        MagicMock(return_value=[]),
    )
    monkeypatch.setattr(
        resumes_routes.candidate_roster_service,
        "reconcile_candidate_roster",
        AsyncMock(return_value=_dummy_report()),
    )

    app = _build_app(_mock_conn(), role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await _post_csv(client, job_id)

    assert resp.status_code == 200


@pytest.mark.parametrize("role", _NON_WRITER_ROLES)
@pytest.mark.asyncio
async def test_403s_for_hiring_manager_and_auditor(
    role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    reconcile = AsyncMock(return_value=_dummy_report())
    monkeypatch.setattr(
        resumes_routes.candidate_roster_service,
        "reconcile_candidate_roster",
        reconcile,
    )

    app = _build_app(_mock_conn(), role=role)
    async with await _client(app) as client:
        resp = await _post_csv(client, job_id)

    assert resp.status_code == 403
    reconcile.assert_not_awaited()


# ── size cap ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_oversize_csv_is_rejected_before_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    parse = MagicMock(
        side_effect=ManifestError(
            f"manifest is {len(_OVERSIZE_CSV)} bytes; the cap is 1048576"
        )
    )
    monkeypatch.setattr(
        resumes_routes.bulk_ingest_service, "parse_candidate_csv", parse
    )
    reconcile = AsyncMock(return_value=_dummy_report())
    monkeypatch.setattr(
        resumes_routes.candidate_roster_service,
        "reconcile_candidate_roster",
        reconcile,
    )

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await _post_csv(client, job_id, blob=_OVERSIZE_CSV)

    assert resp.status_code == 422
    reconcile.assert_not_awaited()


# ── wiring ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parsed_rows_are_forwarded_to_the_reconciler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    parsed_rows = [
        CandidateRosterRow(
            line_no=2,
            name="Smith, Jordan",
            email="jordan@example.invalid",
            sfu_id=None,
            work_authorization="eligible",
            work_authorization_source="No Restrictions",
            internal_apsa=False,
            internal_cupe=False,
            submission_date=None,
        )
    ]
    monkeypatch.setattr(
        resumes_routes.bulk_ingest_service,
        "parse_candidate_csv",
        MagicMock(return_value=parsed_rows),
    )
    reconcile = AsyncMock(return_value=_dummy_report())
    monkeypatch.setattr(
        resumes_routes.candidate_roster_service,
        "reconcile_candidate_roster",
        reconcile,
    )

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await _post_csv(client, job_id)

    assert resp.status_code == 200
    reconcile.assert_awaited_once()
    flat = list(reconcile.await_args.args) + list(reconcile.await_args.kwargs.values())
    assert job_id in flat
    assert parsed_rows in flat


@pytest.mark.asyncio
async def test_the_response_body_is_the_reconciliation_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        resumes_routes.bulk_ingest_service,
        "parse_candidate_csv",
        MagicMock(return_value=[]),
    )
    report = _dummy_report()
    monkeypatch.setattr(
        resumes_routes.candidate_roster_service,
        "reconcile_candidate_roster",
        AsyncMock(return_value=report),
    )

    app = _build_app(_mock_conn(), role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await _post_csv(client, job_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["matched"] == 1
    assert body["work_authorization_changed"] == 1
