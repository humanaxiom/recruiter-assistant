"""Route-level tests for Phase 6's shortlist routes —
``src.api.routes.shortlist`` (new module; does not exist yet). The whole
file fails at collection (``ModuleNotFoundError``) — RED half of the TDD
cycle.

**Ambiguities this file locks:**

* ``src.api.routes.shortlist`` exposes ``router: APIRouter`` (absolute
  paths): ``POST /jobs/{job_id}/shortlist`` (generate), ``GET
  /jobs/{job_id}/shortlist`` (list), ``GET /shortlist/{entry_id}`` (get
  one), ``GET /jobs/{job_id}/shortlist/export`` (export; query param
  ``format`` in ``{csv, evidence-csv, json}`` — **NO ``reveal`` query param
  as of FU-4/D3; see below**).
* ``POST /jobs/{job_id}/shortlist`` returns **202** and enqueues
  ``arq.enqueue_job("shortlist_job", str(job_id))``.
* Export filenames: ``shortlist-{job_id}-anon.csv`` (``format=csv``),
  ``shortlist-{job_id}-evidence-anon.csv`` (``format=evidence-csv``) — the
  ``-anon`` suffix is now ALWAYS present (FU-4/D3 removed the ``reveal``
  parameter, so the export route hardcodes ``reveal=False`` — the
  ``shortlist-{job_id}.csv`` un-suffixed filename is DEAD as of this route;
  it can only ever be produced by a hypothetical future audited bulk-reveal
  export route, not this one). Content-Type ``text/csv`` for both csv
  formats, ``application/json`` for ``format=json``, whose body is NESTED
  JSON (``score_breakdown``/``evidence``/``pipeline_meta`` are real JSON
  objects, not double-encoded strings) via
  ``Content-Disposition: attachment; filename="..."``.
* The export route calls ``pii_service.set_pii_key`` inside an open
  ``db.transaction()`` BEFORE calling ``shortlist_service.export_rows``
  (that function's own contract: the raw ``pgp_sym_decrypt`` in its SQL
  fails loud without it — see its docstring).
* **ABSENCE, not merely un-implemented**: the review-workflow decision/stage
  routes (``/shortlist/{id}/decision``, ``/shortlist/{id}/stage``) from hris
  are CUT and must never exist — hitting them is FastAPI's own unmatched-
  route 404, not a 401/403 (proving the route table itself has no entry,
  not that auth blocked something that would otherwise resolve).
* The exported CSV header is a POSITIVE set-equality against
  ``src.services.shortlist_service._CSV_FIELDS`` (already ported in Phase 5)
  — the route must reuse ``shortlist_csv()``/``shortlist_evidence_csv()``,
  not hand-roll its own header, and that header excludes every
  ``current_stage``/``current_decision``/``decision_*`` review-workflow
  column (there is no review pipeline in this project).

**FU-4/D3 — the ``reveal`` query param is REMOVED from the export route.**
Per the decisions doc: this was an UNAUDITED BULK de-anonymization across a
whole shortlist, not recorded in ADR-016. Exports are blind-only. If bulk-
reveal export becomes a real need it gets its own audited POST route later —
this route never un-blinds, regardless of what a caller appends to the query
string. Pinned below on the ACTUAL response bytes (the CSV/JSON body itself,
not merely the filename suffix), the same byte-scan discipline
``test_route_resumes.py`` uses for the résumé read route.

**FU-4 (RBAC) — route→role table for this file:**

| Route                             | Method | Allowed roles |
|--------------------------------------|--------|-----------------|
| ``/jobs/{id}/shortlist`` (generate)  | POST   | admin, recruiter |
| ``/jobs/{id}/shortlist`` (list)      | GET    | all four |
| ``/shortlist/{id}``                  | GET    | all four |
| ``/jobs/{id}/shortlist/export``      | GET    | all four (blind only) |
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
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
from src.errors import AppError
from src.models.pool import get_db
from src.services.shortlist_service import _CSV_FIELDS

_NOW = dt.datetime(2026, 7, 16, tzinfo=dt.UTC)

_NON_SHORTLIST_WRITER_ROLES: tuple[Role, ...] = (Role.HIRING_MANAGER, Role.AUDITOR)
_ALL_ROLES: tuple[Role, ...] = tuple(Role)

_NAME = "Jane Smith"
_EMAIL = "jane@example.test"
_PHONE = "604-555-0192"


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _score_breakdown() -> dict[str, Any]:
    return {
        "skill": 0.8,
        "experience": 0.6,
        "education": 0.4,
        "seniority": 0.5,
        "vector": 0.3,
        "structured": 0.55,
        "motivation": 0.1,
        "implied_experience": False,
        "skill_contributions": [],
        "score_structured": 0.55,
        "score_evidence": 0.6,
    }


def _entry_row(*, entry_id: UUID | None = None, job_id: UUID | None = None) -> _Row:
    return _Row(
        {
            "id": entry_id or uuid4(),
            "job_id": job_id or uuid4(),
            "resume_id": uuid4(),
            "rank": 1,
            "score_final": 0.87,
            "score_breakdown": json.dumps(_score_breakdown()),
            "evidence": json.dumps(
                {
                    "requirements": [],
                    "overall_summary": "Strong candidate.",
                    "cover_letter_presence": False,
                    "cover_letter_evidence": [],
                    "overall_motivation": "",
                }
            ),
            "generated_at": _NOW,
        }
    )


def _export_row(*, job_id: UUID) -> _Row:
    return _Row(
        {
            "rank": 1,
            "resume_id": uuid4(),
            "score_final": 0.87,
            "score_breakdown": json.dumps(_score_breakdown()),
            "evidence": json.dumps(
                {
                    "requirements": [],
                    "overall_summary": "Strong candidate.",
                    "cover_letter_presence": False,
                    "cover_letter_evidence": [],
                    "overall_motivation": "",
                }
            ),
            "pipeline_meta": json.dumps({"git_sha": "abc123def456"}),
            "generated_at": _NOW,
            "job_title": "Senior Backend Engineer",
            "job_department": "Engineering",
            "original_filename": "resume.pdf",
            "candidate_name": _NAME,
            "candidate_email": _EMAIL,
            "candidate_phone": _PHONE,
            "candidate_parsed": None,
        }
    )


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


# ── POST /jobs/{job_id}/shortlist ────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_shortlist_returns_202_and_enqueues() -> None:
    job_id = uuid4()
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 202
    arq.enqueue_job.assert_awaited_once_with("shortlist_job", str(job_id))


@pytest.mark.asyncio
async def test_generate_shortlist_as_recruiter_succeeds() -> None:
    job_id = uuid4()
    conn = _mock_conn()
    app = _build_app(conn, role=Role.RECRUITER)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 202


@pytest.mark.parametrize("role", _NON_SHORTLIST_WRITER_ROLES)
@pytest.mark.asyncio
async def test_generate_shortlist_403s_for_hiring_manager_and_auditor(
    role: Role,
) -> None:
    job_id = uuid4()
    conn = _mock_conn()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq=arq, role=role)
    async with await _client(app) as client:
        resp = await client.post(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 403
    arq.enqueue_job.assert_not_awaited()


# ── GET /jobs/{job_id}/shortlist (list) / GET /shortlist/{id} ────────────


@pytest.mark.asyncio
async def test_list_shortlist_returns_entries() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchval=False, fetch=[_entry_row(job_id=job_id)])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_list_shortlist_is_readable_by_every_role(role: Role) -> None:
    job_id = uuid4()
    conn = _mock_conn(fetchval=False, fetch=[_entry_row(job_id=job_id)])
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_shortlist_entry_404_when_missing() -> None:
    conn = _mock_conn(fetchval=None, fetchrow=None)
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_shortlist_entry_returns_the_entry_shape() -> None:
    entry_id = uuid4()
    conn = _mock_conn(fetchval=False, fetchrow=_entry_row(entry_id=entry_id))
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(entry_id)
    assert "score_breakdown" in body


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_get_shortlist_entry_is_readable_by_every_role(role: Role) -> None:
    entry_id = uuid4()
    conn = _mock_conn(fetchval=False, fetchrow=_entry_row(entry_id=entry_id))
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.get(f"/shortlist/{entry_id}")
    assert resp.status_code == 200


# ── export ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_csv_content_type_and_anon_filename() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetch=[_export_row(job_id=job_id)])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/export?format=csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    disposition = resp.headers.get("content-disposition", "")
    assert f"shortlist-{job_id}-anon.csv" in disposition


@pytest.mark.asyncio
async def test_export_evidence_csv_filename() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetch=[_export_row(job_id=job_id)])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/export?format=evidence-csv")
    disposition = resp.headers.get("content-disposition", "")
    assert "evidence" in disposition
    assert "-anon" in disposition


@pytest.mark.asyncio
async def test_export_json_is_nested_not_double_encoded() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetch=[_export_row(job_id=job_id)])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/export?format=json")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    body = resp.json()
    assert isinstance(body, list)
    assert isinstance(body[0]["score_breakdown"], dict)
    assert isinstance(body[0]["evidence"], dict)


@pytest.mark.asyncio
async def test_export_csv_header_is_exactly_the_expected_field_set() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetch=[_export_row(job_id=job_id)])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/export?format=csv")
    reader = csv.reader(io.StringIO(resp.text))
    header = next(reader)
    assert set(header) == set(_CSV_FIELDS)


@pytest.mark.parametrize(
    "forbidden",
    ["current_stage", "current_decision", "decision_notes", "decision_kind"],
)
def test_csv_fields_never_include_review_workflow_columns(forbidden: str) -> None:
    assert forbidden not in _CSV_FIELDS


@pytest.mark.parametrize("role", _ALL_ROLES)
@pytest.mark.asyncio
async def test_export_is_readable_by_every_role(role: Role) -> None:
    job_id = uuid4()
    conn = _mock_conn(fetch=[_export_row(job_id=job_id)])
    app = _build_app(conn, role=role)
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/shortlist/export?format=csv")
    assert resp.status_code == 200


# ── FU-4/D3: ``reveal`` on export is REMOVED — asserted on response bytes ──


@pytest.mark.asyncio
async def test_export_csv_reveal_true_query_param_never_unblinds() -> None:
    """The whole point of D3 for this route: even ``?reveal=true`` produces
    the ANONYMIZED filename and body — there is no code path left in this
    route capable of un-blinding a bulk export. Asserted on the raw CSV
    bytes (candidate name/email/phone absent), not merely the filename
    suffix, so a route that dropped the query param from its signature but
    still somehow threaded a stray ``True`` into ``export_rows`` would still
    fail this test."""
    job_id = uuid4()
    conn = _mock_conn(fetch=[_export_row(job_id=job_id)])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(
            f"/jobs/{job_id}/shortlist/export?format=csv&reveal=true"
        )
    assert resp.status_code == 200
    disposition = resp.headers.get("content-disposition", "")
    assert f"shortlist-{job_id}-anon.csv" in disposition
    raw = resp.text
    assert _NAME not in raw
    assert _EMAIL not in raw
    assert _PHONE not in raw


@pytest.mark.asyncio
async def test_export_json_reveal_true_query_param_never_unblinds() -> None:
    job_id = uuid4()
    conn = _mock_conn(fetch=[_export_row(job_id=job_id)])
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.get(
            f"/jobs/{job_id}/shortlist/export?format=json&reveal=true"
        )
    assert resp.status_code == 200
    raw = resp.text
    assert _NAME not in raw
    assert _EMAIL not in raw
    assert _PHONE not in raw


@pytest.mark.asyncio
async def test_export_route_never_forwards_a_reveal_kwarg_to_the_service(
    monkeypatch: Any,
) -> None:
    """Structural pin: ``shortlist_service.export_rows`` must be called with
    ``reveal=False`` (or no ``reveal`` kwarg at all) regardless of the query
    string — there is no longer a route-level parameter to thread through."""
    job_id = uuid4()
    conn = _mock_conn(fetch=[])
    app = _build_app(conn)
    spy = AsyncMock(return_value=[])
    monkeypatch.setattr(shortlist_routes.shortlist_service, "export_rows", spy)
    async with await _client(app) as client:
        await client.get(f"/jobs/{job_id}/shortlist/export?format=csv&reveal=true")
    spy.assert_awaited_once()
    _, kwargs = spy.await_args
    assert kwargs.get("reveal", False) is False


# ── absence of the cut review-workflow routes ────────────────────────────


@pytest.mark.asyncio
async def test_decision_route_does_not_exist_returns_plain_404() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{uuid4()}/decision", json={})
    assert resp.status_code == 404
    assert resp.status_code not in (401, 403)


@pytest.mark.asyncio
async def test_stage_route_does_not_exist_returns_plain_404() -> None:
    conn = _mock_conn()
    app = _build_app(conn)
    async with await _client(app) as client:
        resp = await client.post(f"/shortlist/{uuid4()}/stage", json={})
    assert resp.status_code == 404
    assert resp.status_code not in (401, 403)
