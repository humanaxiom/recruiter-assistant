"""RED -- slice H3: the résumé export as a background job, against a REAL
Postgres (testcontainers), a REAL filesystem ``BlobStore`` and a FAKE arq.

Flow pinned: ``POST /jobs/{id}/resumes/exports`` inserts a ``resume_export_
requests`` row (``queued``, ``requested_by`` = the session ``cas_username``)
and enqueues ``build_resume_export`` BY NAME; the worker task (called directly
here) builds the ZIP into the BlobStore under ``resume_exports/`` and sets
``ready`` + ``counts`` + ``expires_at``; ``GET /resume-exports/{id}`` shows
status and counts to the requester (or a session ``admin``); ``POST
/resume-exports/{id}/download`` streams the ZIP to the requester ONLY and is
the only thing that writes the ``resume_export`` audit row. ``expire_resume_
exports`` is the cron sweep.

What a real Postgres proves that the unit tests cannot: the table's CHECK /
FK / defaults, that the encrypted ``candidate_name`` round-trips into the term
list inside the worker, that the audit row is written at download and never at
build, and that the sweep really NULLs ``blob_key`` and deletes the blob.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import asyncpg
import fitz  # type: ignore[import-untyped]
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient, Response
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, get_arq, resolve_role, resolve_user
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.schemas.auth import User
from src.services import pii
from src.settings import get_settings
from src.storage.blob_store import BlobStore, get_blob_store

PII_KEY = "integration-test-pii-key-do-not-use-in-prod"
NAMES = ("Testy McTestface", "Quuxa Frobnitz", "Withdrawn Wanda", "Coverly Letterson")
EMAILS = tuple(f"{n.split()[0].lower()}@example.invalid" for n in NAMES)
EMPLOYER = "Zorblax Analytics"
SCHOOL = "Quuxville Institute"
ORIGINAL_NAMES = (
    "mctestface_resume_final.pdf",
    "frobnitz_cv.pdf",
    "wanda_cv.pdf",
    "letterson_cv.pdf",
)
STATES = ("queued", "building", "ready", "failed", "expired")


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=8)
    await init_schema(p)
    async with p.acquire() as conn:
        await conn.execute("TRUNCATE jobs, outbox, audit_log CASCADE")
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture(autouse=True)
def _pii_key() -> Iterator[None]:
    with patch(
        "src.services.pii.get_settings", return_value=SimpleNamespace(pii_key=PII_KEY)
    ):
        yield


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _env(monkeypatch: pytest.MonkeyPatch, **values: int) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key.upper(), str(value))
    get_settings.cache_clear()


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


# ------------------------------------------------------------------ seeding


def _pdf(name: str, email: str, extra: str = "", pages: int = 1) -> bytes:
    doc = fitz.open()
    for n in range(pages):
        page = doc.new_page()
        lines = (
            [
                name,
                f"{email}  604-555-01{len(name) % 90 + 10}",
                "Experienced in recruiting and operations.",
                f"Worked at {EMPLOYER}; studied at {SCHOOL}.",
                extra,
            ]
            if n == 0
            else [f"Continued on page {n + 1}: more operations experience."]
        )
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + i * 16), line, fontsize=11)
    data: bytes = doc.tobytes()
    doc.close()
    return data


def _flat(pdf: bytes) -> str:
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return re.sub(r"\s+", " ", " ".join(p.get_text() for p in doc)).lower()
    finally:
        doc.close()


async def _job(pool: asyncpg.Pool, *, blind: bool = False) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ('Eng', 'Build things.', $1) RETURNING id",
            blind,
        )
    return job_id


async def _user(pool: asyncpg.Pool, role: str = "recruiter") -> User:
    username = f"user-{uuid4().hex[:8]}"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO users (cas_username, display_name, role) "
            "VALUES ($1, $1, $2) RETURNING id, created_at, last_seen_at",
            username,
            role,
        )
    assert row is not None
    return User(
        id=row["id"],
        cas_username=username,
        display_name=username,
        email=None,
        role=role,
        active=True,
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )


async def _resume(
    pool: asyncpg.Pool,
    store: BlobStore,
    job_id: UUID,
    idx: int,
    *,
    parsed: bool = True,
    withdrawn: bool = False,
    cover: bool = False,
    minute: int = 0,
    pages: int = 1,
) -> tuple[bytes, str]:
    """Seed one résumé (idx picks the fake identity); returns (PDF, blob key)."""
    data = _pdf(NAMES[idx], EMAILS[idx], extra=f"marker-{idx}", pages=pages)
    key = f"resumes/{uuid4().hex}.pdf"
    await store.put(key, data, "application/pdf")
    cover_key: str | None = None
    cover_sha: str | None = None
    if cover:
        cover_key = f"covers/{uuid4().hex}.pdf"
        await store.put(cover_key, _pdf(NAMES[idx], EMAILS[idx]), "application/pdf")
        cover_sha = uuid4().hex
    body = {
        "candidate": {"name": NAMES[idx], "email": EMAILS[idx]},
        "experience": [{"company": EMPLOYER, "title": "Analyst"}],
        "education": [{"institution": SCHOOL, "degree": "BSc"}],
    }
    async with pool.acquire() as conn:
        name_ct: bytes | None = None
        if parsed:
            async with conn.transaction():
                await pii.set_pii_key(conn)
                name_ct = await pii.encrypt(conn, NAMES[idx])
        await conn.execute(
            """
            INSERT INTO resumes (
                job_id, blob_key, original_filename, mime_type, file_size_bytes,
                sha256, consent_acknowledged, status, candidate_name, parsed,
                cover_letter_blob_key, cover_letter_sha256, uploaded_at,
                withdrawn_at
            ) VALUES ($1, $2, $3, 'application/pdf', $4, $5, TRUE,
                      $6::resume_status, $7, $8::jsonb, $9, $10,
                      '2026-11-01T10:00:00Z'::timestamptz
                      + make_interval(mins => $11),
                      CASE WHEN $12 THEN now() END)
            """,
            job_id,
            key,
            ORIGINAL_NAMES[idx],
            len(data),
            uuid4().hex,
            "parsed" if parsed else "uploaded",
            name_ct,
            json.dumps(body) if parsed else None,
            cover_key,
            cover_sha,
            minute,
            withdrawn,
        )
    return data, key


async def _insert_export(
    pool: asyncpg.Pool,
    job_id: UUID,
    user: User,
    *,
    status: str,
    blob_key: str | None = None,
    expires_in_min: int | None = None,
) -> UUID:
    async with pool.acquire() as conn:
        export_id: UUID = await conn.fetchval(
            """
            INSERT INTO resume_export_requests
                (job_id, requested_by, status, blob_key, expires_at)
            VALUES ($1, $2, $3, $4,
                    CASE WHEN $5::int IS NULL THEN NULL
                         ELSE now() + make_interval(mins => $5::int) END)
            RETURNING id
            """,
            job_id,
            user.cas_username,
            status,
            blob_key,
            expires_in_min,
        )
    return export_id


async def _row(pool: asyncpg.Pool, export_id: UUID) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM resume_export_requests WHERE id = $1", export_id
        )
    assert row is not None
    return row


def _counts(row: asyncpg.Record) -> dict[str, Any]:
    raw = row["counts"]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return dict(parsed or {})


async def _audit_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return list(
            await conn.fetch(
                "SELECT job_id, actor_user_id, details, context FROM audit_log "
                "WHERE action = 'resume_export'"
            )
        )


# ---------------------------------------------------------------- app / http


def _fake_arq() -> MagicMock:
    arq = MagicMock(name="arq")
    arq.enqueue_job = AsyncMock()
    return arq


def _build_app(
    pool: asyncpg.Pool, store: BlobStore, user: User, arq: MagicMock | None = None
) -> FastAPI:
    from src.api.routes import resume_export

    app = FastAPI()
    app.include_router(resume_export.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_arq] = lambda: arq or _fake_arq()
    app.dependency_overrides[resolve_role] = lambda: Role.ADMIN
    app.dependency_overrides[resolve_user] = lambda: user

    @app.exception_handler(AppError)
    async def _handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _call(app: FastAPI, method: str, path: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path)


async def _start(app: FastAPI, job_id: UUID) -> Response:
    return await _call(app, "POST", f"/jobs/{job_id}/resumes/exports")


async def _status(app: FastAPI, export_id: UUID | str) -> Response:
    return await _call(app, "GET", f"/resume-exports/{export_id}")


async def _download(app: FastAPI, export_id: UUID | str) -> Response:
    return await _call(app, "POST", f"/resume-exports/{export_id}/download")


async def _run_worker(
    pool: asyncpg.Pool, store: BlobStore, export_id: UUID | str
) -> str:
    from src.worker.export_tasks import build_resume_export

    ctx: dict[str, Any] = {"pg_pool": pool, "blob_store": store}
    return await build_resume_export(ctx, str(export_id))


async def _request_and_build(
    pool: asyncpg.Pool, store: BlobStore, user: User, job_id: UUID
) -> tuple[FastAPI, UUID]:
    app = _build_app(pool, store, user)
    resp = await _start(app, job_id)
    assert resp.status_code == 202, resp.text
    export_id = UUID(resp.json()["id"])
    assert await _run_worker(pool, store, export_id) == "ready"
    return app, export_id


def _zip(resp: Response) -> zipfile.ZipFile:
    assert resp.status_code == 200, resp.text
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _manifest(zf: zipfile.ZipFile) -> list[str]:
    return zf.read("MANIFEST.txt").decode("utf-8").splitlines()


# ------------------------------------------------------------------- the DDL


@pytest.mark.asyncio
async def test_table_has_the_pinned_columns_and_types(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT column_name, data_type, is_nullable FROM "
            "information_schema.columns WHERE table_name = 'resume_export_requests'"
        )
    cols = {r["column_name"]: r for r in rows}

    assert {
        "id",
        "job_id",
        "requested_by",
        "status",
        "blob_key",
        "counts",
        "created_at",
        "expires_at",
    } <= set(cols)
    assert cols["id"]["data_type"] == "uuid"
    assert cols["job_id"]["data_type"] == "uuid"
    assert cols["counts"]["data_type"] == "jsonb"
    assert cols["created_at"]["data_type"] == "timestamp with time zone"
    assert cols["expires_at"]["data_type"] == "timestamp with time zone"
    assert cols["blob_key"]["is_nullable"] == "YES"
    assert cols["expires_at"]["is_nullable"] == "YES"


@pytest.mark.asyncio
async def test_first_insert_defaults_to_queued_and_status_is_constrained(
    pool: asyncpg.Pool,
) -> None:
    job_id = await _job(pool)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO resume_export_requests (job_id, requested_by) "
            "VALUES ($1, 'someone') RETURNING id, status, blob_key, created_at",
            job_id,
        )
        assert row is not None
        assert row["status"] == "queued"
        assert row["blob_key"] is None
        assert row["created_at"] is not None
        for state in STATES:
            await conn.execute(
                "UPDATE resume_export_requests SET status = $1 WHERE id = $2",
                state,
                row["id"],
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE resume_export_requests SET status = 'bogus' WHERE id = $1",
                row["id"],
            )


@pytest.mark.asyncio
async def test_deleting_the_job_cascades_to_its_export_rows(
    pool: asyncpg.Pool,
) -> None:
    job_id = await _job(pool)
    user = await _user(pool)
    await _insert_export(pool, job_id, user, status="queued")

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)
        n = await conn.fetchval("SELECT count(*) FROM resume_export_requests")

    assert n == 0


# ----------------------------------------------------------- POST: the request


@pytest.mark.asyncio
async def test_post_queues_a_row_and_enqueues_the_task_by_name(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    user = await _user(pool)
    arq = _fake_arq()

    resp = await _start(_build_app(pool, store, user, arq), job_id)

    assert resp.status_code == 202
    export_id = UUID(resp.json()["id"])
    row = await _row(pool, export_id)
    assert row["status"] == "queued"
    assert row["job_id"] == job_id
    assert row["requested_by"] == user.cas_username
    assert row["blob_key"] is None
    arq.enqueue_job.assert_awaited_once()
    assert arq.enqueue_job.await_args.args[:2] == (
        "build_resume_export",
        str(export_id),
    )
    assert await _audit_rows(pool) == []
    assert await store.list_keys("resume_exports/") == []


@pytest.mark.asyncio
async def test_post_for_an_unknown_job_is_404_and_queues_nothing(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    arq = _fake_arq()
    app = _build_app(pool, store, await _user(pool), arq)

    resp = await _start(app, uuid4())

    assert resp.status_code == 404
    arq.enqueue_job.assert_not_awaited()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM resume_export_requests") == 0


@pytest.mark.asyncio
async def test_post_over_the_applicant_cap_is_422_and_queues_nothing(
    pool: asyncpg.Pool, store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, resume_export_max_applicants=2)
    job_id = await _job(pool)
    for idx in range(3):
        await _resume(pool, store, job_id, idx, minute=idx)
    arq = _fake_arq()

    resp = await _start(_build_app(pool, store, await _user(pool), arq), job_id)

    assert resp.status_code == 422
    arq.enqueue_job.assert_not_awaited()
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM resume_export_requests") == 0


@pytest.mark.asyncio
async def test_withdrawn_applicants_do_not_count_toward_the_cap(
    pool: asyncpg.Pool, store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, resume_export_max_applicants=2)
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0, minute=0)
    await _resume(pool, store, job_id, 1, minute=1)
    await _resume(pool, store, job_id, 2, withdrawn=True, minute=2)

    resp = await _start(_build_app(pool, store, await _user(pool)), job_id)

    assert resp.status_code == 202


# -------------------------------------------------- the worker and the download


@pytest.mark.asyncio
async def test_round_trip_builds_stores_and_serves_the_zip(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    first, _k = await _resume(pool, store, job_id, 0, minute=0)  # parsed
    await _resume(pool, store, job_id, 1, parsed=False, minute=1)  # unparsed
    await _resume(pool, store, job_id, 2, withdrawn=True, minute=2)  # withdrawn
    await _resume(pool, store, job_id, 3, cover=True, minute=3)  # with cover
    user = await _user(pool)

    app, export_id = await _request_and_build(pool, store, user, job_id)

    row = await _row(pool, export_id)
    assert row["status"] == "ready"
    assert row["blob_key"].startswith("resume_exports/")
    assert await store.exists(row["blob_key"])
    counts = _counts(row)
    assert counts["applicants"] == 3
    assert counts["anonymized"] == 2
    assert counts["not_anonymized"] == 1

    resp = await _download(app, export_id)
    zf = _zip(resp)

    assert resp.headers["content-type"].startswith("application/zip")
    assert f"job-{job_id}-resumes.zip" in resp.headers["content-disposition"]
    assert resp.headers["content-disposition"].lower().startswith("attachment")
    assert resp.headers["cache-control"].lower() == "no-store"
    assert set(zf.namelist()) == {
        "MANIFEST.txt",
        "as-is/applicant-001_resume.pdf",
        "anonymized/applicant-001_resume.pdf",
        "as-is/applicant-002_resume.pdf",  # unparsed: as-is only
        "as-is/applicant-003_resume.pdf",
        "as-is/applicant-003_cover_letter.pdf",
        "anonymized/applicant-003_resume.pdf",
        "anonymized/applicant-003_cover_letter.pdf",
    }
    assert zf.read("as-is/applicant-001_resume.pdf") == first
    lines = _manifest(zf)
    assert "applicants: 3" in lines
    assert "anonymized: 2" in lines
    assert "not anonymized: 1" in lines
    assert any("do not forward" in ln.lower() for ln in lines)
    for entry in (n for n in zf.namelist() if n.startswith("anonymized/")):
        flat = _flat(zf.read(entry))
        for secret in (NAMES[0], NAMES[3], *EMAILS, EMPLOYER, SCHOOL, "604-555"):
            assert secret.lower() not in flat, (entry, secret)
        assert "recruiting" in flat
    blob = "\n".join(zf.namelist()) + zf.read("MANIFEST.txt").decode()
    for secret in (*NAMES, *EMAILS, *ORIGINAL_NAMES, "mctestface", "frobnitz"):
        assert secret.lower() not in blob.lower(), secret


@pytest.mark.parametrize(("ttl", "low", "high"), [(None, 58, 61), (5, 3, 6)])
@pytest.mark.asyncio
async def test_ready_row_expires_after_the_configured_ttl(
    pool: asyncpg.Pool,
    store: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
    ttl: int | None,
    low: int,
    high: int,
) -> None:
    if ttl is not None:
        _env(monkeypatch, resume_export_ttl_minutes=ttl)
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)

    _app, export_id = await _request_and_build(pool, store, await _user(pool), job_id)

    row = await _row(pool, export_id)
    left = row["expires_at"] - dt.datetime.now(dt.UTC)
    assert dt.timedelta(minutes=low) < left < dt.timedelta(minutes=high)


@pytest.mark.asyncio
async def test_audit_row_is_written_at_download_not_at_build_and_has_counts_only(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0, minute=0)
    await _resume(pool, store, job_id, 1, parsed=False, minute=1)
    user = await _user(pool)

    app, export_id = await _request_and_build(pool, store, user, job_id)
    assert await _audit_rows(pool) == []  # built, nobody has the file yet
    assert (await _status(app, export_id)).status_code == 200
    assert await _audit_rows(pool) == []  # status polls are not disclosures

    assert (await _download(app, export_id)).status_code == 200

    rows = await _audit_rows(pool)
    assert len(rows) == 1
    assert rows[0]["job_id"] == job_id
    assert rows[0]["actor_user_id"] == user.id
    raw = str(rows[0]["details"]) + str(rows[0]["context"])
    d = rows[0]["details"]
    details = json.loads(d) if isinstance(d, str) else dict(d or {})
    assert details["applicants"] == 2
    assert details["anonymized"] == 1
    assert details["not_anonymized"] == 1
    for secret in (*NAMES, *EMAILS, *ORIGINAL_NAMES, "mctestface", EMPLOYER, SCHOOL):
        assert secret.lower() not in raw.lower(), secret


@pytest.mark.asyncio
async def test_status_shows_state_and_counts_and_no_identity_or_blob_path(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    user = await _user(pool)
    app, export_id = await _request_and_build(pool, store, user, job_id)

    resp = await _status(app, export_id)

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(export_id)
    assert body["status"] == "ready"
    assert body["counts"]["applicants"] == 1
    assert body["expires_at"] is not None
    assert "blob_key" not in body
    assert "resume_exports/" not in resp.text
    for secret in (*NAMES, *EMAILS, *ORIGINAL_NAMES):
        assert secret.lower() not in resp.text.lower(), secret


@pytest.mark.asyncio
async def test_unknown_export_is_404(pool: asyncpg.Pool, store: BlobStore) -> None:
    app = _build_app(pool, store, await _user(pool))

    assert (await _status(app, uuid4())).status_code == 404
    assert (await _download(app, uuid4())).status_code == 404


# ------------------------------------------------------------ requester only


@pytest.mark.asyncio
async def test_only_the_requester_may_see_or_download_an_export(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    owner = await _user(pool)
    other = await _user(pool)
    _app, export_id = await _request_and_build(pool, store, owner, job_id)

    stranger = _build_app(pool, store, other)
    assert (await _status(stranger, export_id)).status_code == 404
    assert (await _download(stranger, export_id)).status_code == 404
    assert await _audit_rows(pool) == []


@pytest.mark.asyncio
async def test_an_admin_may_see_the_status_but_not_download_anothers_export(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    owner = await _user(pool)
    admin = await _user(pool, role="admin")
    _app, export_id = await _request_and_build(pool, store, owner, job_id)

    as_admin = _build_app(pool, store, admin)

    assert (await _status(as_admin, export_id)).status_code == 200
    assert (await _download(as_admin, export_id)).status_code == 404
    assert await _audit_rows(pool) == []


@pytest.mark.asyncio
async def test_latest_is_the_requesters_newest_export_for_that_job(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    owner = await _user(pool)
    other = await _user(pool)
    path = f"/jobs/{job_id}/resumes/exports/latest"
    mine = _build_app(pool, store, owner)
    theirs = _build_app(pool, store, other)
    assert (await _call(mine, "GET", path)).status_code == 404  # none yet

    first = UUID((await _start(mine, job_id)).json()["id"])
    # A live (queued/building) export is reused by a second click, so finish
    # the first one before asking again: "latest" must then be the new row.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE resume_export_requests SET status = 'failed' WHERE id = $1",
            first,
        )
    second = UUID((await _start(mine, job_id)).json()["id"])

    resp = await _call(mine, "GET", path)
    assert resp.status_code == 200
    assert resp.json()["id"] == str(second)
    assert resp.json()["id"] != str(first)
    assert (await _call(theirs, "GET", path)).status_code == 404


# --------------------------------------------------- downloads that are not ready


@pytest.mark.parametrize("state", ["queued", "building", "failed", "expired"])
@pytest.mark.asyncio
async def test_download_of_a_not_ready_export_is_409_and_unaudited(
    pool: asyncpg.Pool, store: BlobStore, state: str
) -> None:
    job_id = await _job(pool)
    user = await _user(pool)
    export_id = await _insert_export(pool, job_id, user, status=state)

    resp = await _download(_build_app(pool, store, user), export_id)

    assert resp.status_code == 409
    assert await _audit_rows(pool) == []


@pytest.mark.asyncio
async def test_download_after_the_ttl_but_before_the_sweep_is_409(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    user = await _user(pool)
    await store.put("resume_exports/stale.zip", b"PK-not-served", "application/zip")
    export_id = await _insert_export(
        pool,
        job_id,
        user,
        status="ready",
        blob_key="resume_exports/stale.zip",
        expires_in_min=-1,
    )

    resp = await _download(_build_app(pool, store, user), export_id)

    assert resp.status_code == 409
    assert b"PK-not-served" not in resp.content
    assert await _audit_rows(pool) == []


@pytest.mark.asyncio
async def test_download_when_the_stored_zip_vanished_is_409_and_unaudited(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    user = await _user(pool)
    app, export_id = await _request_and_build(pool, store, user, job_id)
    await store.delete((await _row(pool, export_id))["blob_key"])

    resp = await _download(app, export_id)

    assert resp.status_code == 409
    assert await _audit_rows(pool) == []


# -------------------------------------------------- worker edge cases


@pytest.mark.asyncio
async def test_a_missing_resume_blob_does_not_fail_the_export(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    _d, gone_key = await _resume(pool, store, job_id, 0, minute=0)
    await _resume(pool, store, job_id, 1, minute=1)
    await store.delete(gone_key)
    user = await _user(pool)

    app, export_id = await _request_and_build(pool, store, user, job_id)

    zf = _zip(await _download(app, export_id))
    lines = _manifest(zf)
    assert "applicants: 2" in lines
    line = next(ln for ln in lines if ln.startswith("applicant-001"))
    assert "file missing" in line.lower()
    assert "as-is/applicant-002_resume.pdf" in zf.namelist()
    assert not [n for n in zf.namelist() if "applicant-001" in n]


@pytest.mark.asyncio
async def test_a_worker_crash_marks_failed_with_no_pii_and_no_blob(
    pool: asyncpg.Pool, store: BlobStore, tmp_path: Path
) -> None:
    class _Boom(BlobStore):
        async def get(self, key: str) -> bytes:
            raise RuntimeError(f"boom {NAMES[0]} {EMAILS[0]}")

    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    user = await _user(pool)
    app = _build_app(pool, store, user)
    export_id = UUID((await _start(app, job_id)).json()["id"])

    out = await _run_worker(pool, _Boom(tmp_path / "blobs"), export_id)

    assert out == "failed"
    row = await _row(pool, export_id)
    assert row["status"] == "failed"
    assert row["blob_key"] is None
    dump = str(dict(row)).lower()
    for secret in (NAMES[0], EMAILS[0], "mctestface", "testy"):
        assert secret.lower() not in dump, secret
    assert await store.list_keys("resume_exports/") == []
    assert (await _download(app, export_id)).status_code == 409


@pytest.mark.asyncio
async def test_the_worker_skips_unknown_and_already_claimed_exports(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    user = await _user(pool)
    building = await _insert_export(pool, job_id, user, status="building")

    assert await _run_worker(pool, store, uuid4()) == "skipped"
    assert await _run_worker(pool, store, building) == "skipped"
    assert (await _row(pool, building))["status"] == "building"


@pytest.mark.asyncio
async def test_a_file_over_the_page_cap_ships_as_is_but_not_anonymized(
    pool: asyncpg.Pool, store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, resume_export_max_pages_per_file=2)
    job_id = await _job(pool)
    big, _k = await _resume(pool, store, job_id, 0, pages=3)
    user = await _user(pool)

    app, export_id = await _request_and_build(pool, store, user, job_id)

    zf = _zip(await _download(app, export_id))
    assert zf.read("as-is/applicant-001_resume.pdf") == big
    assert "anonymized/applicant-001_resume.pdf" not in zf.namelist()
    assert "not anonymized: 1" in _manifest(zf)
    assert _counts(await _row(pool, export_id))["anonymized"] == 0


@pytest.mark.asyncio
async def test_blind_review_job_exports_anonymized_only(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=True)
    await _resume(pool, store, job_id, 0, cover=True, minute=0)
    await _resume(pool, store, job_id, 1, parsed=False, minute=1)
    user = await _user(pool)

    app, export_id = await _request_and_build(pool, store, user, job_id)

    zf = _zip(await _download(app, export_id))
    assert set(zf.namelist()) == {
        "MANIFEST.txt",
        "anonymized/applicant-001_resume.pdf",
        "anonymized/applicant-001_cover_letter.pdf",
    }
    assert "not anonymized: 1" in _manifest(zf)


@pytest.mark.asyncio
async def test_the_old_synchronous_route_is_gone(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    app = _build_app(pool, store, await _user(pool))

    resp = await _call(app, "GET", f"/jobs/{job_id}/resumes/export.zip")

    assert resp.status_code in (404, 405)


# ------------------------------------------------------------------ the sweep


def _backdate(store: BlobStore, key: str, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(store._resolve(key), (then, then))


@pytest.mark.asyncio
async def test_sweep_expires_old_rows_and_deletes_their_blobs(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    from src.worker.export_tasks import expire_resume_exports

    job_id = await _job(pool)
    user = await _user(pool)
    for key in ("resume_exports/old.zip", "resume_exports/fresh.zip"):
        await store.put(key, b"PK-zip", "application/zip")
    old = await _insert_export(
        pool,
        job_id,
        user,
        status="ready",
        blob_key="resume_exports/old.zip",
        expires_in_min=-5,
    )
    fresh = await _insert_export(
        pool,
        job_id,
        user,
        status="ready",
        blob_key="resume_exports/fresh.zip",
        expires_in_min=30,
    )
    ctx: dict[str, Any] = {"pg_pool": pool, "blob_store": store}

    out = await expire_resume_exports(ctx)

    assert isinstance(out, str)
    row = await _row(pool, old)
    assert row["status"] == "expired"
    assert row["blob_key"] is None
    assert not await store.exists("resume_exports/old.zip")
    kept = await _row(pool, fresh)
    assert kept["status"] == "ready"
    assert kept["blob_key"] == "resume_exports/fresh.zip"
    assert await store.exists("resume_exports/fresh.zip")
    assert isinstance(await expire_resume_exports(ctx), str)  # idempotent


@pytest.mark.asyncio
async def test_sweep_removes_orphan_export_blobs_but_not_referenced_or_fresh_ones(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    from src.worker.export_tasks import expire_resume_exports

    job_id = await _job(pool)
    user = await _user(pool)
    for key in (
        "resume_exports/orphan-old.zip",
        "resume_exports/orphan-new.zip",
        "resume_exports/referenced.zip",
    ):
        await store.put(key, b"PK-zip", "application/zip")
    week = 7 * 24 * 3600
    _backdate(store, "resume_exports/orphan-old.zip", week)
    _backdate(store, "resume_exports/referenced.zip", week)
    await _insert_export(
        pool,
        job_id,
        user,
        status="ready",
        blob_key="resume_exports/referenced.zip",
        expires_in_min=30,
    )

    await expire_resume_exports({"pg_pool": pool, "blob_store": store})

    assert not await store.exists("resume_exports/orphan-old.zip")
    assert await store.exists("resume_exports/orphan-new.zip")
    assert await store.exists("resume_exports/referenced.zip")
