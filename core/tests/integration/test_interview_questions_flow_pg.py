"""Integration (real Postgres, real ASGI app, real CAS session cookies) -- RED
for interview-question generator slice S2: the route flow, the worker task and
the properties that only a real database can prove.

* POST really claims (pending row, creator), enqueues ids only, audits without
  any question text, and refuses a second POST with 409.
* A hiring manager can read their assigned job's questions (404 when not
  assigned) but never write; an auditor's read is logged, nobody else's.
* The worker, run against a real shortlist row, persists ``ready`` questions
  whose prompt held no identifier even on a BLIND job, and records the typed
  refusals/failures.
* **A shortlist regenerate (``persist_shortlist`` deletes and reinserts every
  row, minting new ``shortlist_entries.id``s) leaves the stored questions
  intact**, reachable through the NEW entry id, with ``stale`` recomputed from
  the new entry's gaps.
* A WITHDRAWN candidate's questions are unreachable (GET and POST 404) and
  come back on reinstatement.

Routes: POST 202 ``{"status": "pending"}`` / 409 / 403 (non-writer) / 404;
GET 200 (``status`` ``"none"`` when no row) / 404 (unknown, unassigned or
withdrawn). See ``tests/unit/test_route_interview_questions.py`` for the full
choice list.

Modules/routes that do not exist yet are reached lazily, so each test fails on
its own with the real reason (404 route / ModuleNotFoundError).
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from fastapi import FastAPI
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
from src.schemas.matching import (
    DEFAULT_WEIGHTS,
    EvidenceObjectIngest,
    PipelineMeta,
    RequirementEvidenceIngest,
    ScoreBreakdown,
)
from src.services import cas_service
from src.services import pii as pii_service
from src.services.interview_questions import InterviewQuestionsIngest
from src.services.shortlist_service import persist_shortlist
from src.settings import Settings

_JD = "We need a senior backend engineer. " * 3
TEST_PII_KEY = "integration-test-pii-key-do-not-use-in-prod"

NAME = "Jane Smith"
EMAIL = "jane.smith@example.test"
PHONE = "604-555-0192"
QUOTE = "Deployed a pod once in a lab"
QUOTE_WITH_PII = (
    f"{NAME} deployed a pod once in a lab; contact {EMAIL} or {PHONE} for details"
)
REQ_PARTIAL = "Kubernetes operations"
REQ_MISSING = "Budget ownership"
Q1 = "Walk me through the lab deployment -- what broke first?"
Q2 = "Describe a budget you were accountable for."


def _svc() -> Any:
    return importlib.import_module("src.services.interview_question_service")


def _worker_task() -> Any:
    return importlib.import_module(
        "src.worker.interview_tasks"
    ).generate_interview_questions


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


def _patch_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    monkeypatch.setattr(pii_service, "get_settings", lambda: settings)


class _App:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.arq = MagicMock(enqueue_job=AsyncMock())
        app = FastAPI()
        app.include_router(auth_routes.router)
        app.include_router(shortlist_routes.router)

        async def _db() -> AsyncIterator[Any]:
            async with pool.acquire() as conn:
                yield conn

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_arq] = lambda: self.arq

        @app.exception_handler(AppError)
        async def _handler(_request: Any, exc: AppError) -> JSONResponse:
            return JSONResponse(
                status_code=exc.status,
                content={"detail": exc.message, "code": exc.code},
            )

        self.app = app

    def client(self) -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://test"
        )


async def _login(
    pool: asyncpg.Pool,
    client: AsyncClient,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    role: str,
) -> tuple[uuid.UUID, str, str]:
    username = f"{role}-{uuid.uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        user_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO users (cas_username, display_name, email, role) "
            "VALUES ($1, $1, NULL, $2) RETURNING id",
            username,
            role,
        )
    monkeypatch.setattr(
        cas_service, "validate_ticket", AsyncMock(return_value=username)
    )
    resp = await client.get("/auth/cas/validate", params={"ticket": "t"})
    assert resp.status_code == 302
    sid = resp.cookies.get(settings.session_cookie_name)
    assert sid is not None
    # The jar would otherwise ride along beside the explicit per-request cookie
    # (and a second login in one test would leave two ra_session values).
    client.cookies.clear()
    return user_id, username, sid


async def _job(pool: asyncpg.Pool, *, blind: bool = False) -> uuid.UUID:
    async with pool.acquire() as conn:
        job_id: uuid.UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ('Eng', $1, $2) RETURNING id",
            _JD,
            blind,
        )
    return job_id


async def _resume(pool: asyncpg.Pool, job_id: uuid.UUID) -> uuid.UUID:
    """A résumé whose name, email and phone are really pgcrypto-encrypted."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await pii_service.set_pii_key(conn)
            name = await pii_service.encrypt(conn, NAME)
            email = await pii_service.encrypt(conn, EMAIL)
            phone = await pii_service.encrypt(conn, PHONE)
            resume_id: uuid.UUID = await conn.fetchval(
                """
                INSERT INTO resumes (
                    job_id, blob_key, original_filename, mime_type,
                    file_size_bytes, sha256, consent_acknowledged, status,
                    candidate_name, candidate_email, candidate_phone
                ) VALUES ($1, $2, 'resume.pdf', 'application/pdf', 1024, $3,
                          TRUE, 'parsed', $4, $5, $6)
                RETURNING id
                """,
                job_id,
                f"resumes/{uuid.uuid4().hex}.pdf",
                uuid.uuid4().hex,
                name,
                email,
                phone,
            )
    return resume_id


def _evidence(
    *, partial_quote: str = QUOTE, missing_text: str = REQ_MISSING
) -> EvidenceObjectIngest:
    return EvidenceObjectIngest(
        requirements=[
            RequirementEvidenceIngest(
                requirement="5 years of Python",
                status="met",
                evidence="Built Python services since 2016",
                confidence=0.9,
            ),
            RequirementEvidenceIngest(
                requirement=REQ_PARTIAL,
                status="partial",
                evidence=partial_quote,
                confidence=0.5,
            ),
            RequirementEvidenceIngest(
                requirement=missing_text, status="missing", evidence="", confidence=0.1
            ),
        ]
    )


async def _shortlist(
    pool: asyncpg.Pool,
    job_id: uuid.UUID,
    resume_id: uuid.UUID,
    *,
    evidence: EvidenceObjectIngest | None = None,
    evaluated: bool = True,
) -> uuid.UUID:
    """Persist (replace) the job's shortlist the way the ranking worker does;
    returns the NEW entry id. Calling it twice is a shortlist regenerate."""
    result = ShortlistResult(
        job_id=job_id,
        entries=[
            ShortlistResultEntry(
                resume_id=resume_id,
                rank=1,
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
                evidence=evidence if evidence is not None else _evidence(),
                evidence_evaluated=evaluated,
            )
        ],
        pipeline_meta=PipelineMeta(
            model_gen="test-gen",
            model_emb="test-emb",
            prompt_versions={"shortlist_evidence": "shortlist_evidence_v1"},
            weights=DEFAULT_WEIGHTS,
            git_sha="abc123",
            generated_at=dt.datetime.now(dt.UTC),
            timings_ms={},
        ),
    )
    async with pool.acquire() as conn:
        async with conn.transaction():
            await persist_shortlist(conn, result)
        entry_id: uuid.UUID = await conn.fetchval(
            "SELECT id FROM shortlist_entries WHERE job_id = $1 AND resume_id = $2",
            job_id,
            resume_id,
        )
    return entry_id


async def _assign(pool: asyncpg.Pool, job_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        admin: uuid.UUID = await conn.fetchval(
            "INSERT INTO users (cas_username, role) VALUES ($1, 'admin') RETURNING id",
            f"admin-{uuid.uuid4().hex[:8]}",
        )
        await conn.execute(
            "INSERT INTO job_assignees (job_id, user_id, assigned_by) "
            "VALUES ($1, $2, $3)",
            job_id,
            user_id,
            admin,
        )


async def _audit(pool: asyncpg.Pool, action: str) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM audit_log WHERE action = $1 ORDER BY occurred_at", action
        )


async def _iq_row(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM interview_questions")


class _FakeLLM:
    def __init__(self, *questions: dict[str, Any]) -> None:
        self.questions = list(questions)
        self.messages: list[list[dict[str, str]]] = []

    async def chat_json(self, messages: Any, schema: Any, **_kw: Any) -> Any:
        self.messages.append(list(messages))
        return InterviewQuestionsIngest.model_validate({"questions": self.questions})

    def blob(self) -> str:
        return "\n".join(m["content"] for msgs in self.messages for m in msgs)


def _standard_llm() -> _FakeLLM:
    return _FakeLLM(
        {"requirement_index": 1, "question": Q1, "probes_quote": QUOTE},
        {"requirement_index": 2, "question": Q2, "probes_quote": None},
    )


async def _run_worker(
    pool: asyncpg.Pool, job_id: uuid.UUID, resume_id: uuid.UUID, llm: Any
) -> Any:
    ctx = {"pg_pool": pool, "llm": llm, "job_try": 1}
    return await _worker_task()(ctx, str(job_id), str(resume_id))


async def _claim_and_run(
    pool: asyncpg.Pool, job_id: uuid.UUID, resume_id: uuid.UUID, llm: Any
) -> Any:
    async with pool.acquire() as conn:
        await _svc().claim(conn, job_id, resume_id, "alice")
    return await _run_worker(pool, job_id, resume_id, llm)


_URL = "/shortlist/{}/interview-questions"


# ── POST ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_claims_enqueues_ids_only_and_audits(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    entry_id = await _shortlist(pg_pool, job_id, resume_id)

    async with harness.client() as client:
        user_id, username, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        resp = await client.post(
            _URL.format(entry_id), cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 202
    assert resp.json()["status"] == "pending"
    rows = await _iq_row(pg_pool)
    assert len(rows) == 1
    assert (rows[0]["job_id"], rows[0]["resume_id"]) == (job_id, resume_id)
    assert rows[0]["status"] == "pending"
    assert rows[0]["created_by"] == username
    harness.arq.enqueue_job.assert_awaited_once_with(
        "generate_interview_questions", str(job_id), str(resume_id)
    )

    audit = await _audit(pg_pool, "generate_interview_questions")
    assert len(audit) == 1
    assert audit[0]["actor_kind"] == "user"
    assert audit[0]["actor_user_id"] == user_id
    assert audit[0]["subject_type"] == "shortlist_entry"
    assert audit[0]["subject_id"] == entry_id
    assert audit[0]["job_id"] == job_id
    details = audit[0]["details"]
    blob = details if isinstance(details, str) else json.dumps(details)
    for forbidden in (Q1, Q2, QUOTE, REQ_PARTIAL, REQ_MISSING, NAME, EMAIL, PHONE):
        assert forbidden not in blob


@pytest.mark.asyncio
async def test_a_second_post_while_pending_is_409_and_does_nothing(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    entry_id = await _shortlist(pg_pool, job_id, resume_id)

    async with harness.client() as client:
        _, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        cookies = {settings.session_cookie_name: sid}
        first = await client.post(_URL.format(entry_id), cookies=cookies)
        second = await client.post(_URL.format(entry_id), cookies=cookies)

    assert (first.status_code, second.status_code) == (202, 409)
    assert harness.arq.enqueue_job.await_count == 1
    assert len(await _audit(pg_pool, "generate_interview_questions")) == 1
    assert len(await _iq_row(pg_pool)) == 1


@pytest.mark.asyncio
async def test_post_after_the_stall_window_is_accepted_again(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    entry_id = await _shortlist(pg_pool, job_id, resume_id)

    async with harness.client() as client:
        _, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        cookies = {settings.session_cookie_name: sid}
        assert (
            await client.post(_URL.format(entry_id), cookies=cookies)
        ).status_code == 202
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE interview_questions "
                "SET requested_at = now() - interval '25 minutes'"
            )
        resp = await client.post(_URL.format(entry_id), cookies=cookies)

    assert resp.status_code == 202
    assert harness.arq.enqueue_job.await_count == 2


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
@pytest.mark.asyncio
async def test_non_writers_cannot_post_even_when_assigned(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    entry_id = await _shortlist(pg_pool, job_id, resume_id)

    async with harness.client() as client:
        user_id, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role=role
        )
        await _assign(pg_pool, job_id, user_id)
        resp = await client.post(
            _URL.format(entry_id), cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 403
    assert await _iq_row(pg_pool) == []
    harness.arq.enqueue_job.assert_not_awaited()
    assert await _audit(pg_pool, "generate_interview_questions") == []


@pytest.mark.asyncio
async def test_post_for_an_unknown_entry_is_404_and_writes_nothing(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)

    async with harness.client() as client:
        _, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        resp = await client.post(
            _URL.format(uuid.uuid4()), cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 404
    assert await _iq_row(pg_pool) == []
    harness.arq.enqueue_job.assert_not_awaited()


# ── GET ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_with_nothing_generated_is_200_none(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    entry_id = await _shortlist(pg_pool, job_id, resume_id)

    async with harness.client() as client:
        _, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        resp = await client.get(
            _URL.format(entry_id), cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "none"
    assert body["questions"] == []
    assert body["stale"] is False


@pytest.mark.asyncio
async def test_post_then_worker_then_get_serves_the_questions(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    entry_id = await _shortlist(pg_pool, job_id, resume_id)

    async with harness.client() as client:
        _, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        cookies = {settings.session_cookie_name: sid}
        assert (
            await client.post(_URL.format(entry_id), cookies=cookies)
        ).status_code == 202
        pending = (await client.get(_URL.format(entry_id), cookies=cookies)).json()
        assert await _run_worker(pg_pool, job_id, resume_id, _standard_llm()) == "ready"
        ready = (await client.get(_URL.format(entry_id), cookies=cookies)).json()

    assert pending["status"] == "pending"
    assert pending["questions"] == []
    assert ready["status"] == "ready"
    assert ready["reason"] is None
    assert ready["stale"] is False
    assert ready["gaps_sha256"] == ready["current_gaps_sha256"] is not None
    assert ready["generated_at"] is not None
    assert ready["questions"] == [
        {
            "requirement_index": 1,
            "requirement": REQ_PARTIAL,
            "question": Q1,
            "probes_quote": QUOTE,
        },
        {
            "requirement_index": 2,
            "requirement": REQ_MISSING,
            "question": Q2,
            "probes_quote": None,
        },
    ]


@pytest.mark.asyncio
async def test_hiring_manager_reads_only_an_assigned_jobs_questions(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    entry_id = await _shortlist(pg_pool, job_id, resume_id)
    assert await _claim_and_run(pg_pool, job_id, resume_id, _standard_llm()) == "ready"

    async with harness.client() as client:
        manager_id, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="hiring_manager"
        )
        cookies = {settings.session_cookie_name: sid}
        unassigned = await client.get(_URL.format(entry_id), cookies=cookies)
        await _assign(pg_pool, job_id, manager_id)
        assigned = await client.get(_URL.format(entry_id), cookies=cookies)

    assert unassigned.status_code == 404
    assert Q1 not in unassigned.text and Q2 not in unassigned.text
    assert assigned.status_code == 200
    assert assigned.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_auditor_reads_are_logged_and_nobody_elses(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    entry_id = await _shortlist(pg_pool, job_id, resume_id)
    await _claim_and_run(pg_pool, job_id, resume_id, _standard_llm())

    async with harness.client() as client:
        rec_id, _, rec_sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        assert (
            await client.get(
                _URL.format(entry_id), cookies={settings.session_cookie_name: rec_sid}
            )
        ).status_code == 200
        assert await _audit(pg_pool, "read_interview_questions") == []

        aud_id, _, aud_sid = await _login(
            pg_pool, client, settings, monkeypatch, role="auditor"
        )
        resp = await client.get(
            _URL.format(entry_id), cookies={settings.session_cookie_name: aud_sid}
        )
        missing = await client.get(
            _URL.format(uuid.uuid4()), cookies={settings.session_cookie_name: aud_sid}
        )

    assert resp.status_code == 200
    assert missing.status_code == 404
    rows = await _audit(pg_pool, "read_interview_questions")
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == aud_id
    assert rows[0]["subject_type"] == "shortlist_entry"
    assert rows[0]["subject_id"] == entry_id
    assert rec_id != aud_id


# ── regenerate survival / stale ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_shortlist_regenerate_keeps_the_questions_under_the_new_entry_id(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    old_entry = await _shortlist(pg_pool, job_id, resume_id)
    await _claim_and_run(pg_pool, job_id, resume_id, _standard_llm())

    new_entry = await _shortlist(pg_pool, job_id, resume_id)  # same gaps
    assert new_entry != old_entry

    async with harness.client() as client:
        _, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        cookies = {settings.session_cookie_name: sid}
        via_new = await client.get(_URL.format(new_entry), cookies=cookies)
        via_old = await client.get(_URL.format(old_entry), cookies=cookies)

    rows = await _iq_row(pg_pool)
    assert len(rows) == 1 and rows[0]["status"] == "ready"
    assert via_old.status_code == 404
    assert via_new.status_code == 200
    body = via_new.json()
    assert body["status"] == "ready"
    assert [q["question"] for q in body["questions"]] == [Q1, Q2]
    assert body["stale"] is False


@pytest.mark.asyncio
async def test_a_regenerate_that_moves_the_gaps_marks_the_questions_stale(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    await _shortlist(pg_pool, job_id, resume_id)
    await _claim_and_run(pg_pool, job_id, resume_id, _standard_llm())

    new_entry = await _shortlist(
        pg_pool,
        job_id,
        resume_id,
        evidence=_evidence(missing_text="Headcount planning"),
    )

    async with harness.client() as client:
        _, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        body = (
            await client.get(
                _URL.format(new_entry), cookies={settings.session_cookie_name: sid}
            )
        ).json()

    assert body["status"] == "ready"
    assert body["stale"] is True
    assert body["gaps_sha256"] != body["current_gaps_sha256"]
    # The OLD requirement text is what the stored questions were written for.
    assert body["questions"][1]["requirement"] == REQ_MISSING


@pytest.mark.asyncio
async def test_a_pending_claim_survives_a_regenerate_and_still_blocks_a_post(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    await _shortlist(pg_pool, job_id, resume_id)
    async with pg_pool.acquire() as conn:
        assert await _svc().claim(conn, job_id, resume_id, "alice") is True
    new_entry = await _shortlist(pg_pool, job_id, resume_id)

    async with harness.client() as client:
        _, _, sid = await _login(
            pg_pool, client, settings, monkeypatch, role="recruiter"
        )
        resp = await client.post(
            _URL.format(new_entry), cookies={settings.session_cookie_name: sid}
        )

    assert resp.status_code == 409


# ── withdrawn candidates ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_withdrawn_candidates_questions_are_hidden_until_reinstated(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    harness = _App(pg_pool)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    entry_id = await _shortlist(pg_pool, job_id, resume_id)
    await _claim_and_run(pg_pool, job_id, resume_id, _standard_llm())

    async with harness.client() as client:
        _, _, sid = await _login(pg_pool, client, settings, monkeypatch, role="admin")
        cookies = {settings.session_cookie_name: sid}
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE resumes SET withdrawn_at = now(), "
                "withdrawal_reason = 'candidate withdrew' WHERE id = $1",
                resume_id,
            )
        hidden = await client.get(_URL.format(entry_id), cookies=cookies)
        blocked = await client.post(_URL.format(entry_id), cookies=cookies)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE resumes SET withdrawn_at = NULL, withdrawal_reason = NULL "
                "WHERE id = $1",
                resume_id,
            )
        back = await client.get(_URL.format(entry_id), cookies=cookies)

    assert hidden.status_code == 404
    assert Q1 not in hidden.text and Q2 not in hidden.text
    assert blocked.status_code == 404
    assert back.status_code == 200
    assert [q["question"] for q in back.json()["questions"]] == [Q1, Q2]
    assert harness.arq.enqueue_job.await_count == 0


@pytest.mark.asyncio
async def test_the_worker_records_entry_gone_for_a_withdrawn_resume(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    await _shortlist(pg_pool, job_id, resume_id)
    async with pg_pool.acquire() as conn:
        await _svc().claim(conn, job_id, resume_id, "alice")
        await conn.execute(
            "UPDATE resumes SET withdrawn_at = now() WHERE id = $1", resume_id
        )
    llm = _standard_llm()

    assert await _run_worker(pg_pool, job_id, resume_id, llm) == "failed"

    async with pg_pool.acquire() as conn:
        rec = await _svc().get(conn, job_id, resume_id)
    assert (rec.status, rec.reason) == ("failed", "entry_gone")
    assert llm.messages == []


# ── worker ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_worker_records_entry_gone_when_the_shortlist_lost_the_resume(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    other = await _resume(pg_pool, job_id)
    await _shortlist(pg_pool, job_id, resume_id)
    async with pg_pool.acquire() as conn:
        await _svc().claim(conn, job_id, resume_id, "alice")
    await _shortlist(pg_pool, job_id, other)  # regenerate WITHOUT resume_id
    llm = _standard_llm()

    assert await _run_worker(pg_pool, job_id, resume_id, llm) == "failed"

    async with pg_pool.acquire() as conn:
        rec = await _svc().get(conn, job_id, resume_id)
    assert (rec.status, rec.reason) == ("failed", "entry_gone")
    assert llm.messages == []


@pytest.mark.parametrize("blind", [False, True])
@pytest.mark.asyncio
async def test_the_prompt_holds_no_identifier_and_a_leaking_question_is_dropped(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch, blind: bool
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id = await _job(pg_pool, blind=blind)
    resume_id = await _resume(pg_pool, job_id)
    # On a blind job the quote is redacted by get_one; on a non-blind one it is
    # used verbatim, so only the blind case may carry identifiers INTO it.
    await _shortlist(
        pg_pool,
        job_id,
        resume_id,
        evidence=_evidence(partial_quote=QUOTE_WITH_PII if blind else QUOTE),
    )
    llm = _FakeLLM(
        {"requirement_index": 2, "question": Q2, "probes_quote": None},
        {"requirement_index": 2, "question": f"{NAME}, how did you own a budget?"},
        {"requirement_index": 2, "question": f"Is {EMAIL} still your address?"},
        {"requirement_index": 2, "question": f"Can we reach you on {PHONE}?"},
    )

    assert await _claim_and_run(pg_pool, job_id, resume_id, llm) == "ready"

    blob = llm.blob().casefold()
    assert blob, "the model was never called"
    for forbidden in ("jane", "smith", EMAIL, "example.test", PHONE, "555-0192"):
        assert forbidden.casefold() not in blob, forbidden
    async with pg_pool.acquire() as conn:
        rec = await _svc().get(conn, job_id, resume_id)
    assert [q["question"] for q in rec.questions] == [Q2]


@pytest.mark.parametrize(
    "evaluated,reason", [(False, "not_evaluated")], ids=["past-the-evidence-cliff"]
)
@pytest.mark.asyncio
async def test_the_worker_records_a_typed_refusal_without_calling_the_model(
    pg_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
    evaluated: bool,
    reason: str,
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    await _shortlist(pg_pool, job_id, resume_id, evaluated=evaluated)
    llm = _standard_llm()

    assert await _claim_and_run(pg_pool, job_id, resume_id, llm) == "refused"

    async with pg_pool.acquire() as conn:
        rec = await _svc().get(conn, job_id, resume_id)
    assert (rec.status, rec.reason) == ("refused", reason)
    assert llm.messages == []


@pytest.mark.asyncio
async def test_the_worker_records_failed_no_questions_for_an_empty_result(
    pg_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    _patch_settings(monkeypatch, settings)
    job_id = await _job(pg_pool)
    resume_id = await _resume(pg_pool, job_id)
    await _shortlist(pg_pool, job_id, resume_id)

    assert await _claim_and_run(pg_pool, job_id, resume_id, _FakeLLM()) == "failed"

    async with pg_pool.acquire() as conn:
        rec = await _svc().get(conn, job_id, resume_id)
    assert (rec.status, rec.reason) == ("failed", "no_questions")
    assert rec.questions == []
