"""Route-level tests for Phase 6's job routes — ``src.api.routes.jobs``
(new module; does not exist yet). The whole file fails at collection
(``ModuleNotFoundError``) — RED half of the TDD cycle.

Uses a real ASGI app (a fresh ``FastAPI()``, not the whole ``src.api.main``
app, to stay independent of lifespan wiring) with ``httpx.AsyncClient`` +
``ASGITransport``, exercised through ``app.dependency_overrides`` for
``get_db`` / ``get_arq`` / ``resolve_role`` — mirroring the tester
mandate: mock db/arq/blob_store, let the real service layer run against a
mocked ``asyncpg`` connection (no service-internals monkeypatching).

**Ambiguities this file locks (the coder's implementation MUST match):**

* ``src.api.routes.jobs`` exposes an ``APIRouter`` named ``router`` with
  ABSOLUTE paths (no router-level ``prefix``): ``POST /jobs``,
  ``GET /jobs``, ``GET /jobs/{job_id}``, ``PATCH /jobs/{job_id}/status``,
  ``POST /jobs/jd-extract``. ``src.api.main`` mounts it with
  ``app.include_router(jobs.router)``.
* ``POST /jobs`` — body is ``JobCreate`` JSON. Returns **201** with the
  created ``JobOut`` and enqueues ``arq.enqueue_job("parse_job",
  str(job.id))`` with the NEWLY CREATED id (not a client-supplied one — jobs
  have no client-chosen id).
* ``GET /jobs/{job_id}`` — 404 (via a global ``AppError`` exception handler
  mapping ``NotFoundError.status == 404``) when the id does not resolve.
  **``blind_review`` fail-open guard (ADR-006 / carried every phase since
  Phase 2 security)**: tested BOTH directions — a job stored
  ``blind_review=True`` in the DB returns ``"blind_review": true`` in the
  JSON body, one stored ``False`` returns ``false``. This is the load-bearing
  pair; a route that hardcodes either literal fails one of the two.
* ``GET /jobs`` — a TRIMMED ``JobListItem`` shape: exactly
  ``{id, title, department, status, created_at, parsed_at}`` per row — no
  ``description_raw``/``blind_review``/other full-detail fields leak into
  the list view.
* ``PATCH /jobs/{job_id}/status`` — body ``JobTransition`` (``{"to":
  "open"}``). A valid forward transition (draft -> open) returns **200**
  with the updated ``JobOut``. An invalid transition (e.g. draft -> archived,
  skipping open/closed) returns **409** (a business-rule conflict, distinct
  from the 422 a syntactically-invalid ``JobStatus`` enum member would
  already get from pydantic).
* ``POST /jobs/jd-extract`` — ``multipart/form-data`` with a ``file`` field.
  Returns **200** with a ``JDExtractText``-shaped body
  (``filename``/``text``/``chars``) and performs **NO** database write at
  all — ``db.execute``/``db.fetchrow``/``db.fetch`` are never called for
  this route.
* The optional ``X-Actor-Name`` header populates ``created_by`` on job
  create; absent, the actor defaults to the fixed label ``"api"``.

**FU-4 (RBAC) — route→role table for this file, per the locked decisions:**

| Route                        | Method | Allowed roles                  |
|-------------------------------|--------|---------------------------------|
| ``/jobs``                     | POST   | admin, recruiter                |
| ``/jobs/jd-extract``          | POST   | admin, recruiter                |
| ``/jobs/bulk``                | POST   | admin, recruiter (see ``test_route_jobs_bulk.py``) |
| ``/jobs``                     | GET    | all four                        |
| ``/jobs/{id}``                | GET    | all four                        |
| ``/jobs/{id}``                | PATCH  | admin, recruiter ONLY (D5 — the most explicit coverage: this PATCH can set ``blind_review: false``, permanently un-blinding every résumé/shortlist under the job with NO audit row) |
| ``/jobs/{id}/status``         | PATCH  | admin, recruiter                |

Every route now goes through a per-route ``Depends(require_role(...))``
(replacing the Phase-6 uniform router-level ``require_api_key``), which in
turn depends on the shared ``resolve_role``. Tests bypass auth for the
non-RBAC-focused tests by overriding ``resolve_role`` to always resolve
``Role.ADMIN`` (admin is allowed everywhere in the table above) — the actual
per-route ``require_role(...)`` check still runs for real against that
resolved role, it is never itself overridden (see
``test_api_deps.py::test_require_role_composes_with_resolve_role_via_dependency_override``
for why that is the correct override point).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import jobs as jobs_routes
from src.errors import AppError
from src.models.pool import get_db

_NOW = dt.datetime(2026, 7, 16, tzinfo=dt.UTC)

# The disallowed roles for the admin/recruiter-only routes in this file.
_NON_JOB_WRITER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)
_ALL_ROLES: tuple[Role, ...] = tuple(Role)


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _job_row(
    *,
    job_id: UUID | None = None,
    title: str = "Senior Backend Engineer",
    status: str = "draft",
    blind_review: bool = True,
    department: str | None = "Engineering",
    created_by: str | None = "api",
) -> _Row:
    return _Row(
        {
            "id": job_id or uuid4(),
            "title": title,
            "department": department,
            "location": "Remote",
            "employment_type": "full_time",
            "seniority": "senior",
            "min_years": 5,
            "description_raw": "We need a senior backend engineer. " * 3,
            "description_parsed": None,
            "status": status,
            "retention_days": 180,
            "blind_review": blind_review,
            "failure_reason": None,
            "created_by": created_by,
            "created_at": _NOW,
            "updated_at": _NOW,
            "parsed_at": None,
            "closed_at": None,
        }
    )


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
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=cm)
    return conn


def _build_app(
    conn: MagicMock, *, arq: MagicMock | None = None, role: Role = Role.ADMIN
) -> FastAPI:
    app = FastAPI()
    app.include_router(jobs_routes.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: arq or MagicMock(
        enqueue_job=AsyncMock()
    )
    # FU-4: bypass key→role resolution with a fixed resolved role — the real
    # per-route require_role(...) check still runs against it for real.
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── POST /jobs ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_job_returns_201() -> None:
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_job_enqueues_parse_job_with_the_new_id() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    async with await _client(app) as client:
        await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
    arq.enqueue_job.assert_awaited_once_with("parse_job", str(job_id))


@pytest.mark.asyncio
async def test_create_job_as_recruiter_succeeds() -> None:
    """Admin is not the ONLY writer role — recruiter must also be able to
    create jobs (proves the allowed set is {admin, recruiter}, not just
    admin-only)."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
    assert resp.status_code == 201


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_create_job_403s_for_hiring_manager_and_auditor(role: Role) -> None:
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs",
            json={
                "title": "Senior Backend Engineer",
                "description_raw": "We need a senior backend engineer. " * 3,
            },
        )
    assert resp.status_code == 403


# ── GET /jobs/{id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_404_via_app_error_handler() -> None:
    conn = _mock_conn(fetchrow=None)
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_job_blind_review_true_from_a_true_row() -> None:
    conn = _mock_conn(fetchrow=_job_row(blind_review=True))
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 200
    assert resp.json()["blind_review"] is True


@pytest.mark.asyncio
async def test_get_job_blind_review_false_from_a_false_row() -> None:
    """The pairing test — a route hardcoding True instead of reading the row
    would pass the test above but fail this one."""
    conn = _mock_conn(fetchrow=_job_row(blind_review=False))
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 200
    assert resp.json()["blind_review"] is False


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_get_job_is_readable_by_every_role(role: Role) -> None:
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}")
    assert resp.status_code == 200


# ── GET /jobs (list) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_jobs_returns_the_trimmed_joblistitem_shape() -> None:
    conn = _mock_conn(fetch=[_job_row(title="A"), _job_row(title="B")])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get("/jobs")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    expected_keys = {"id", "title", "department", "status", "created_at", "parsed_at"}
    assert set(body[0].keys()) == expected_keys


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_list_jobs_is_readable_by_every_role(role: Role) -> None:
    conn = _mock_conn(fetch=[_job_row()])
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.get("/jobs")
    assert resp.status_code == 200


# ── PATCH /jobs/{id}/status ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_job_status_draft_to_open_succeeds() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="open"), fetchval="draft")
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


@pytest.mark.asyncio
async def test_patch_job_status_rejects_an_invalid_transition() -> None:
    job_id = uuid4()
    conn = _mock_conn(
        fetchrow=_job_row(job_id=job_id, status="draft"), fetchval="draft"
    )
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "archived"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_job_status_as_recruiter_succeeds() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="open"), fetchval="draft")
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})
    assert resp.status_code == 200


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_patch_job_status_403s_for_hiring_manager_and_auditor(
    role: Role,
) -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, status="draft"), fetchval="draft")
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}/status", json={"to": "open"})
    assert resp.status_code == 403


# ── PATCH /jobs/{id} — general partial update ────────────────────────────


@pytest.mark.asyncio
async def test_patch_job_returns_200_with_updated_jobout() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, title="Updated Title"))
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "Updated Title"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated Title"


@pytest.mark.asyncio
async def test_patch_job_404_for_missing_job() -> None:
    conn = _mock_conn(fetchrow=None)
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{uuid4()}", json={"title": "New Title"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_job_422_rejects_status_field() -> None:
    """``status`` has its own state-machine-guarded transition endpoint —
    it must not be smuggled through the general PATCH. ``JobUpdate`` has no
    ``status`` field at all, so pydantic's ``extra="forbid"`` rejects it with
    a 422 before the route body is ever handled."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{uuid4()}", json={"status": "open"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_job_422_rejects_approval_required_2nd_review_field() -> None:
    """A cut hris column must not be smuggled in through extra="forbid"."""
    conn = _mock_conn(fetchrow=_job_row())
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.patch(
            f"/jobs/{uuid4()}", json={"approval_required_2nd_review": True}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_job_requires_auth_like_sibling_routes() -> None:
    """Every route on this router still passes through ``resolve_role`` —
    remove the bypass override and a 401 must surface (FU-4: the auth-switch
    failure mode moved from ``require_api_key`` to ``resolve_role``, but the
    invariant that this route is not accidentally exempt from auth entirely
    is unchanged)."""
    from fastapi import HTTPException

    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn)

    def _deny() -> None:
        raise HTTPException(status_code=401, detail="missing api key")

    app.dependency_overrides[resolve_role] = _deny
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "X"})
    assert resp.status_code == 401


# ── PATCH /jobs/{id} — FU-4 D5: admin/recruiter ONLY ─────────────────────
#
# The MOST EXPLICIT coverage in this file per the FU-4 decisions doc: this
# PATCH can set ``blind_review: false``, and every redaction key gates off
# ``jobs.blind_review`` — so this route permanently un-blinds every résumé
# and shortlist under a job, with NO audit row at all. A wider blast radius
# than the audited ``POST /resumes/{id}/reveal`` path, hence the tighter,
# explicitly-pinned role set (admin/recruiter ONLY — narrower than
# ``PATCH /jobs/{id}/status``'s otherwise-identical allowed set, which is
# coincidence, not derived from it; each route's role set is independently
# asserted here).


@pytest.mark.asyncio
async def test_patch_job_admin_can_flip_blind_review_off() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, blind_review=False))
    app = _build_app(conn, role=Role.ADMIN)
    async with await _client(app) as client:
        resp = await client.patch(
            f"/jobs/{job_id}", json={"blind_review": False}
        )
    assert resp.status_code == 200
    assert resp.json()["blind_review"] is False


@pytest.mark.asyncio
async def test_patch_job_recruiter_can_flip_blind_review_off() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id, blind_review=False))
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.patch(
            f"/jobs/{job_id}", json={"blind_review": False}
        )
    assert resp.status_code == 200


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_patch_job_403s_for_hiring_manager_and_auditor_d5(role: Role) -> None:
    """D5 (planner finding): a hiring-manager or auditor key must NEVER be
    able to flip ``blind_review`` off for a whole job — that is a wider,
    unaudited de-anonymization blast radius than even the audited reveal
    endpoint."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.patch(
            f"/jobs/{job_id}", json={"blind_review": False}
        )
    assert resp.status_code == 403


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_patch_job_403s_hiring_manager_and_auditor_even_for_a_benign_field(
    role: Role,
) -> None:
    """The 403 applies to the WHOLE route, not just blind_review-touching
    payloads — a hiring-manager/auditor key cannot PATCH a job's title
    either. Pins that the restriction is per-route (D5), not per-field."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.patch(f"/jobs/{job_id}", json={"title": "New Title"})
    assert resp.status_code == 403


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_patch_job_403_happens_before_any_db_write(role: Role) -> None:
    """The role check must reject BEFORE the service layer ever touches the
    database — a disallowed caller must never cause a partial/attempted
    write, even one that would ultimately be rolled back."""
    job_id = uuid4()
    conn = _mock_conn(fetchrow=_job_row(job_id=job_id))
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.patch(
            f"/jobs/{job_id}", json={"blind_review": False}
        )
    assert resp.status_code == 403
    conn.execute.assert_not_called()


# ── POST /jobs/jd-extract ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_jd_extract_returns_extracted_text() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    content = b"We are hiring a senior backend engineer with Python experience."
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs/jd-extract",
            files={"file": ("jd.txt", content, "text/plain")},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "senior backend engineer" in body["text"].lower()
    assert body["filename"] == "jd.txt"
    assert body["chars"] == len(body["text"])


@pytest.mark.asyncio
async def test_jd_extract_performs_no_database_write() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    content = b"We are hiring a senior backend engineer."
    async with await _client(app) as client:
        await client.post(
            "/jobs/jd-extract",
            files={"file": ("jd.txt", content, "text/plain")},
        )
    conn.execute.assert_not_called()
    conn.fetchrow.assert_not_called()
    conn.fetch.assert_not_called()


@pytest.mark.parametrize("role", _NON_JOB_WRITER_ROLES)
@pytest.mark.asyncio
async def test_jd_extract_403s_for_hiring_manager_and_auditor(role: Role) -> None:
    conn = _mock_conn()
    app = _build_app(conn, role=role)
    content = b"We are hiring a senior backend engineer."
    async with await _client(app) as client:
        resp = await client.post(
            "/jobs/jd-extract",
            files={"file": ("jd.txt", content, "text/plain")},
        )
    assert resp.status_code == 403
