"""Integration (real Postgres) -- RED for the human decision record, slice S3:
the three decision routes driven through a real ASGI app and a real CAS session
cookie, mirroring ``test_shortlist_scoping_pg.py``'s fixtures.

Pinned against real SQL (a mocked-conn route test cannot):

* an ASSIGNED hiring_manager records (201) and reads; an UNASSIGNED one gets
  404 and no row exists (ADR-020 s5: no existence oracle); after unassignment
  they can no longer record and no longer see the prior decision.
* auditor -> 403, role-less -> 403, deactivated -> 403; no decision row written.
* a WITHDRAWN candidate -> 404 on record/read, the decision history is
  preserved and hidden from ``latest``, and reinstating restores it.
* the audit row is written in the SAME transaction as the decision: details are
  exactly the contract (never the rationale), a forced INSERT failure leaves no
  audit row, and a forced audit failure leaves no decision row. 403/404/422 and
  an idempotent repeat write no audit row.

No ``api_key_*`` is configured, so ``resolve_role`` always resolves ADMIN and
only the CAS session's ``role`` decides (the shared-BFF-key scenario).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api import deps
from src.api.deps import get_arq
from src.api.routes import auth as auth_routes
from src.api.routes import shortlist as shortlist_routes
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.pipeline.matching.orchestrator import ShortlistResult, ShortlistResultEntry
from src.schemas.matching import DEFAULT_WEIGHTS, PipelineMeta, ScoreBreakdown
from src.services import audit_service
from src.services import pii as pii_service
from src.services.shortlist_service import persist_shortlist
from src.settings import Settings

_JD = "We need a senior backend engineer. " * 3
TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"
_RATIONALE = "Strong systems background, fits the team."
_SECRET_RATIONALE = "Jane Smith phoned me directly and sounded great"


def _routers() -> list[APIRouter]:
    routers = [shortlist_routes.router]
    try:
        mod = importlib.import_module("src.api.routes.decisions")
    except ImportError:
        return routers
    routers.append(mod.router)
    return routers


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(pool)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE job_assignees, sessions, users, audit_log, jobs, "
            "resumes, shortlist_entries, outbox CASCADE"
        )
    try:
        yield pool
    finally:
        async with pool.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS decision_records_fail ON decision_records"
            )
        await pool.close()


def _settings() -> Settings:
    return Settings(
        cas_enabled=True,
        cas_server_url="https://cas.example.edu/cas",
        session_cookie_name="ra_session",
        session_ttl_hours=8,
        default_admin_cas_username="nobody-uses-this-default",
        skill_hash_salt="test-salt",
        pii_key=TEST_PII_KEY,
    )


def _build_app(pool: asyncpg.Pool) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_routes.router)
    for router in _routers():
        app.include_router(router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_arq] = lambda: MagicMock(enqueue_job=AsyncMock())

    @app.exception_handler(AppError)
    async def _handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI, *, raise_exceptions: bool = True) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=raise_exceptions),
        base_url="http://test",
    )


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    monkeypatch.setattr(pii_service, "get_settings", lambda: settings)


async def _insert_job(pool: asyncpg.Pool) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw) VALUES ('Eng', $1) RETURNING id",
            _JD,
        )
    return job_id


async def _insert_user(
    pool: asyncpg.Pool, *, cas_username: str, role: str | None
) -> uuid.UUID:
    async with pool.acquire() as conn:
        user_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO users (cas_username, display_name, email, role) "
            "VALUES ($1, $2, NULL, $3) RETURNING id",
            cas_username,
            cas_username,
            role,
        )
    return user_id


async def _assign(
    pool: asyncpg.Pool, *, job_id: uuid.UUID, user_id: uuid.UUID, by: uuid.UUID
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO job_assignees (job_id, user_id, assigned_by) "
            "VALUES ($1, $2, $3)",
            job_id,
            user_id,
            by,
        )


async def _unassign(
    pool: asyncpg.Pool, *, job_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM job_assignees WHERE job_id = $1 AND user_id = $2",
            job_id,
            user_id,
        )


async def _login(
    pool: asyncpg.Pool,
    client: AsyncClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    role: str | None,
    prefix: str | None = None,
) -> tuple[uuid.UUID, str, str]:
    username = f"{prefix or role or 'norole'}-{uuid.uuid4().hex[:8]}"
    user_id = await _insert_user(pool, cas_username=username, role=role)
    monkeypatch.setattr(
        auth_routes.cas_service, "validate_ticket", AsyncMock(return_value=username)
    )
    resp = await client.get("/auth/cas/validate", params={"ticket": "any-ticket"})
    assert resp.status_code == 302
    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None
    return user_id, sid, username


async def _insert_resume(pool: asyncpg.Pool, job_id: uuid.UUID) -> uuid.UUID:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await pii_service.set_pii_key(conn)
            name_enc = await pii_service.encrypt(conn, "Jane Smith")
            resume_id: uuid.UUID = await conn.fetchval(
                """
                INSERT INTO resumes (
                    job_id, blob_key, original_filename, mime_type,
                    file_size_bytes, sha256, consent_acknowledged, status,
                    candidate_name
                ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3,
                           TRUE, 'parsed', $4)
                RETURNING id
                """,
                job_id,
                f"resumes/{uuid.uuid4().hex}.pdf",
                hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
                name_enc,
            )
    return resume_id


def _meta() -> PipelineMeta:
    return PipelineMeta(
        model_gen="test-gen",
        model_emb="test-emb",
        prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
        weights=DEFAULT_WEIGHTS,
        git_sha="abc123",
        generated_at=dt.datetime.now(dt.UTC),
        timings_ms={},
    )


async def _seed_entry(
    pool: asyncpg.Pool, *, job_id: uuid.UUID, resume_id: uuid.UUID
) -> uuid.UUID:
    result = ShortlistResult(
        job_id=job_id,
        entries=[
            ShortlistResultEntry(
                resume_id=resume_id,
                rank=2,
                score_final=0.9,
                score_structured=0.8,
                score_evidence=0.7,
                breakdown=ScoreBreakdown(
                    skill=0.8,
                    experience=0.6,
                    education=0.4,
                    seniority=0.5,
                    vector=0.3,
                    structured=0.55,
                ),
                evidence=None,
            )
        ],
        pipeline_meta=_meta(),
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            await persist_shortlist(conn, result)
        row = await conn.fetchrow(
            "SELECT id FROM shortlist_entries WHERE job_id=$1 AND resume_id=$2",
            job_id,
            resume_id,
        )
    assert row is not None
    entry_id: uuid.UUID = row["id"]
    return entry_id


async def _world(pool: asyncpg.Pool) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    job_id = await _insert_job(pool)
    resume_id = await _insert_resume(pool, job_id)
    entry_id = await _seed_entry(pool, job_id=job_id, resume_id=resume_id)
    return job_id, resume_id, entry_id


async def _n_decisions(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        n: int = await conn.fetchval("SELECT count(*) FROM decision_records")
    return n


async def _audit_rows(pool: asyncpg.Pool, action: str) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM audit_log WHERE action = $1 ORDER BY occurred_at", action
        )


def _post(
    client: AsyncClient, entry_id: uuid.UUID, sid: str, settings: Settings, **body: Any
) -> Any:
    payload = {"decision": "accept", "rationale": _RATIONALE} | body
    return client.post(
        f"/shortlist/{entry_id}/human-decisions",
        json=payload,
        cookies={settings.session_cookie_name: sid},
    )


def _get(client: AsyncClient, path: str, sid: str, settings: Settings) -> Any:
    return client.get(path, cookies={settings.session_cookie_name: sid})


# -- who may record --------------------------------------------------------


@pytest.mark.parametrize("role", ["admin", "recruiter"])
@pytest.mark.asyncio
async def test_admin_and_recruiter_record_on_any_job(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    _, resume_id, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        _, sid, username = await _login(
            pg_pool, client, settings, monkeypatch, role=role
        )
        resp = await _post(client, entry_id, sid, settings)
    assert resp.status_code == 201
    body = resp.json()
    assert body["decided_by"] == username
    assert body["decided_by_role"] == role
    assert body["resume_id"] == str(resume_id)
    assert body["snap_rank"] == 2
    assert await _n_decisions(pg_pool) == 1


@pytest.mark.asyncio
async def test_assigned_hiring_manager_records_and_reads(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id, resume_id, entry_id = await _world(pg_pool)
    admin_id = await _insert_user(pg_pool, cas_username="seed-admin", role="admin")
    async with await _client(_build_app(pg_pool)) as client:
        hm_id, sid, username = await _login(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=job_id, user_id=hm_id, by=admin_id)
        post = await _post(client, entry_id, sid, settings, decision="hold")
        hist = await _get(
            client, f"/shortlist/{entry_id}/human-decisions", sid, settings
        )
        latest = await _get(client, f"/jobs/{job_id}/decisions/latest", sid, settings)
    assert post.status_code == 201
    assert post.json()["decided_by_role"] == "hiring_manager"
    assert hist.status_code == 200
    assert hist.json()["latest"]["decision"] == "hold"
    assert latest.status_code == 200
    got = [(b["resume_id"], b["decision"], b["decided_by"]) for b in latest.json()]
    assert got == [(str(resume_id), "hold", username)]
    async with pg_pool.acquire() as conn:
        stored = await conn.fetchval("SELECT decided_by_user_id FROM decision_records")
    assert stored == hm_id


@pytest.mark.asyncio
async def test_unassigned_hiring_manager_gets_404_and_no_row_is_written(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id, _, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        _, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        post = await _post(client, entry_id, sid, settings)
        hist = await _get(
            client, f"/shortlist/{entry_id}/human-decisions", sid, settings
        )
        latest = await _get(client, f"/jobs/{job_id}/decisions/latest", sid, settings)
    assert post.status_code == 404
    assert hist.status_code == 404
    assert latest.status_code == 200 and latest.json() == []
    assert await _n_decisions(pg_pool) == 0
    assert await _audit_rows(pg_pool, "record_decision") == []


@pytest.mark.asyncio
async def test_unassigned_404_is_indistinguishable_from_a_missing_entry(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    _, _, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        _, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        real = await _post(client, entry_id, sid, settings)
        ghost = await _post(client, uuid.uuid4(), sid, settings)
    assert real.status_code == ghost.status_code == 404
    assert real.json() == ghost.json()


@pytest.mark.asyncio
async def test_after_unassignment_the_hiring_manager_can_neither_record_nor_see(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id, _, entry_id = await _world(pg_pool)
    admin_id = await _insert_user(pg_pool, cas_username="seed-admin", role="admin")
    async with await _client(_build_app(pg_pool)) as client:
        hm_id, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=job_id, user_id=hm_id, by=admin_id)
        first = await _post(client, entry_id, sid, settings)
        await _unassign(pg_pool, job_id=job_id, user_id=hm_id)
        second = await _post(client, entry_id, sid, settings, decision="reject")
        hist = await _get(
            client, f"/shortlist/{entry_id}/human-decisions", sid, settings
        )
        latest = await _get(client, f"/jobs/{job_id}/decisions/latest", sid, settings)
    assert first.status_code == 201
    assert second.status_code == 404
    assert hist.status_code == 404
    assert latest.json() == []
    assert await _n_decisions(pg_pool) == 1  # the earlier one is preserved


@pytest.mark.asyncio
async def test_another_job_assignment_does_not_grant_access(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    _, _, entry_id = await _world(pg_pool)
    other_job = await _insert_job(pg_pool)
    admin_id = await _insert_user(pg_pool, cas_username="seed-admin", role="admin")
    async with await _client(_build_app(pg_pool)) as client:
        hm_id, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        await _assign(pg_pool, job_id=other_job, user_id=hm_id, by=admin_id)
        resp = await _post(client, entry_id, sid, settings)
    assert resp.status_code == 404
    assert await _n_decisions(pg_pool) == 0


@pytest.mark.asyncio
async def test_auditor_cannot_record_but_can_read_and_the_reads_are_logged(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id, resume_id, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        auditor_id, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        post = await _post(client, entry_id, sid, settings)
        hist = await _get(
            client, f"/shortlist/{entry_id}/human-decisions", sid, settings
        )
        latest = await _get(client, f"/jobs/{job_id}/decisions/latest", sid, settings)
    assert post.status_code == 403
    assert hist.status_code == 200 and latest.status_code == 200
    assert await _n_decisions(pg_pool) == 0
    assert await _audit_rows(pg_pool, "record_decision") == []
    (row,) = await _audit_rows(pg_pool, "read_decisions")
    assert row["actor_user_id"] == auditor_id
    assert row["subject_type"] == "resume" and row["subject_id"] == resume_id
    assert row["job_id"] == job_id
    (row2,) = await _audit_rows(pg_pool, "read_decisions_latest")
    assert row2["subject_id"] == job_id


@pytest.mark.asyncio
async def test_a_session_with_no_role_is_403_and_writes_nothing(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    _, _, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        _, sid, _ = await _login(pg_pool, client, settings, monkeypatch, role=None)
        resp = await _post(client, entry_id, sid, settings)
    assert resp.status_code == 403
    assert await _n_decisions(pg_pool) == 0


@pytest.mark.parametrize("role", ["recruiter", "hiring_manager"])
@pytest.mark.asyncio
async def test_a_deactivated_account_is_403_and_writes_nothing(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id, _, entry_id = await _world(pg_pool)
    admin_id = await _insert_user(pg_pool, cas_username="seed-admin", role="admin")
    async with await _client(_build_app(pg_pool)) as client:
        user_id, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role=role
        )
        await _assign(pg_pool, job_id=job_id, user_id=user_id, by=admin_id)
        async with pg_pool.acquire() as conn:
            await conn.execute("UPDATE users SET active = FALSE WHERE id = $1", user_id)
        resp = await _post(client, entry_id, sid, settings)
    assert resp.status_code == 403
    assert await _n_decisions(pg_pool) == 0


@pytest.mark.asyncio
async def test_no_cookie_at_all_is_403_on_post(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    _, _, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        resp = await client.post(
            f"/shortlist/{entry_id}/human-decisions",
            json={"decision": "accept", "rationale": _RATIONALE},
        )
    assert resp.status_code == 403
    assert await _n_decisions(pg_pool) == 0


# -- body / identity -------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"rationale": "too short"},
        {"decision": "approve"},
        {"decided_by": "mallory"},
        {"rationale": "x" * 1001},
    ],
)
@pytest.mark.asyncio
async def test_a_bad_body_is_422_with_no_row_and_no_audit(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    _, _, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        _, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        resp = await _post(client, entry_id, sid, settings, **overrides)
    assert resp.status_code == 422
    assert await _n_decisions(pg_pool) == 0
    assert await _audit_rows(pg_pool, "record_decision") == []


# -- idempotency / supersession -------------------------------------------


@pytest.mark.asyncio
async def test_repeat_is_200_with_the_same_row_and_no_second_audit(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    _, _, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        _, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        first = await _post(client, entry_id, sid, settings)
        again = await _post(client, entry_id, sid, settings)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json() == again.json()
    assert await _n_decisions(pg_pool) == 1
    assert len(await _audit_rows(pg_pool, "record_decision")) == 1


@pytest.mark.asyncio
async def test_a_new_decision_supersedes_and_history_keeps_both(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id, resume_id, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        _, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        first = await _post(client, entry_id, sid, settings, decision="hold")
        second = await _post(client, entry_id, sid, settings, decision="reject")
        hist = await _get(
            client, f"/shortlist/{entry_id}/human-decisions", sid, settings
        )
        latest = await _get(client, f"/jobs/{job_id}/decisions/latest", sid, settings)
    assert (first.status_code, second.status_code) == (201, 201)
    body = hist.json()
    assert body["latest"]["decision"] == "reject"
    assert [d["decision"] for d in body["history"]] == ["reject", "hold"]
    assert [(b["resume_id"], b["decision"]) for b in latest.json()] == [
        (str(resume_id), "reject")
    ]
    rows = await _audit_rows(pg_pool, "record_decision")
    assert len(rows) == 2
    superseded = json.loads(rows[1]["details"])["superseded_decision_id"]
    assert superseded == first.json()["id"]


# -- withdrawn candidates --------------------------------------------------


@pytest.mark.asyncio
async def test_withdrawn_candidate_is_404_history_hidden_and_reinstate_restores(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id, resume_id, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        _, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        assert (await _post(client, entry_id, sid, settings)).status_code == 201

        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE resumes SET withdrawn_at = now() WHERE id = $1", resume_id
            )
        rec = await _post(client, entry_id, sid, settings, decision="reject")
        hist = await _get(
            client, f"/shortlist/{entry_id}/human-decisions", sid, settings
        )
        latest = await _get(client, f"/jobs/{job_id}/decisions/latest", sid, settings)
        assert rec.status_code == 404
        assert hist.status_code == 404
        assert latest.status_code == 200 and latest.json() == []
        assert await _n_decisions(pg_pool) == 1  # preserved, not deleted

        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE resumes SET withdrawn_at = NULL WHERE id = $1", resume_id
            )
        hist2 = await _get(
            client, f"/shortlist/{entry_id}/human-decisions", sid, settings
        )
        latest2 = await _get(client, f"/jobs/{job_id}/decisions/latest", sid, settings)
    assert hist2.status_code == 200
    assert [d["decision"] for d in hist2.json()["history"]] == ["accept"]
    assert [b["decision"] for b in latest2.json()] == ["accept"]


# -- audit shape / same-transaction guarantees ----------------------------


@pytest.mark.asyncio
async def test_audit_row_has_the_contract_details_and_never_the_rationale(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id, resume_id, entry_id = await _world(pg_pool)
    async with await _client(_build_app(pg_pool)) as client:
        user_id, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        resp = await _post(client, entry_id, sid, settings, rationale=_SECRET_RATIONALE)
    assert resp.status_code == 201
    (row,) = await _audit_rows(pg_pool, "record_decision")
    assert row["actor_kind"] == "user" and row["actor_user_id"] == user_id
    assert row["subject_type"] == "resume" and row["subject_id"] == resume_id
    assert row["job_id"] == job_id
    details = json.loads(row["details"])
    assert details == {
        "decision_id": resp.json()["id"],
        "decision": "accept",
        "rank": 2,
        "score_final": pytest.approx(0.9),
        "superseded_decision_id": None,
        "blind_review": False,
    }
    flat = json.dumps({k: str(v) for k, v in dict(row).items()})
    assert "Jane Smith" not in flat and _SECRET_RATIONALE not in flat


@pytest.mark.asyncio
async def test_forced_insert_failure_leaves_no_audit_row(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    _, _, entry_id = await _world(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "CREATE OR REPLACE FUNCTION decision_records_fail_fn() RETURNS trigger "
            "AS $$ BEGIN RAISE EXCEPTION 'forced failure'; END $$ LANGUAGE plpgsql"
        )
        await conn.execute(
            "CREATE TRIGGER decision_records_fail BEFORE INSERT ON decision_records "
            "FOR EACH ROW EXECUTE FUNCTION decision_records_fail_fn()"
        )
    async with await _client(_build_app(pg_pool), raise_exceptions=False) as client:
        _, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        resp = await _post(client, entry_id, sid, settings)
    assert resp.status_code == 500
    assert await _n_decisions(pg_pool) == 0
    assert await _audit_rows(pg_pool, "record_decision") == []


@pytest.mark.asyncio
async def test_forced_audit_failure_rolls_the_decision_back(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    _, _, entry_id = await _world(pg_pool)
    real = audit_service.record_audit

    async def _boom(conn: Any, **kw: Any) -> None:
        if kw.get("action") == "record_decision":
            raise RuntimeError("audit write failed")
        await real(conn, **kw)

    monkeypatch.setattr(audit_service, "record_audit", _boom)
    async with await _client(_build_app(pg_pool), raise_exceptions=False) as client:
        _, sid, _ = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        resp = await _post(client, entry_id, sid, settings)
    assert resp.status_code == 500
    assert await _n_decisions(pg_pool) == 0, "decision committed without its audit row"
