"""RED -- round 3: the export job fixes and the rebuilt copy, against a REAL
Postgres (testcontainers), a REAL filesystem ``BlobStore`` and a FAKE arq.

Pinned (offline halves live in ``tests/unit/test_resume_export_round3_jobs.py``
and ``tests/unit/test_resume_export_rebuild_*.py``):

* every ``anonymized/`` entry is a fresh text-only PDF (generic producer, one
  built-in font, the redaction marker where identity was);
* a résumé with a TYPED cover letter (``cover_letter_text``) and no cover file
  gets the manifest note ``cover letter typed in the app; not exported``;
* a second ``POST /jobs/{id}/resumes/exports`` while the requester already has a
  ``queued`` or ``building`` row for the job returns THAT row (202, same id) and
  enqueues nothing; a terminal row does not block a new request;
* on an arq retry (``ctx["job_try"] > 1``) the worker accepts a ``building`` row;
  otherwise a ``building`` row is still skipped, and a terminal row never is
  rebuilt;
* the row stores the job's ``blind_review`` at claim (new nullable BOOLEAN
  column, added with ``ADD COLUMN IF NOT EXISTS``); the download is 409 when the
  job is now blind and the stored value is false or NULL; the audit row records
  the STORED value;
* the applicant cap (``resume_export_max_applicants``) is applied at build time
  as well: over the cap the worker either fails the export or builds at most
  ``cap`` applicants -- it never builds more.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
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
NOTE = "cover letter typed in the app; not exported"
TYPED = "I am keen to join your team and bring my analytics background"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine") as pg:
        yield re.sub(r"\+\w+", "", pg.get_connection_url(), count=1)


@pytest.fixture
async def pool(pg_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await asyncpg.create_pool(dsn=pg_dsn, min_size=2, max_size=10)
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


def _pdf(name: str, email: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    lines = [
        name,
        f"{email}  604-555-0142",
        "Experienced in recruiting and operations.",
        f"Worked at {EMPLOYER}; studied at {SCHOOL}.",
    ]
    for i, line in enumerate(lines):
        page.insert_text((72, 72 + i * 16), line, fontsize=11)
    data: bytes = doc.tobytes()
    doc.close()
    return data


async def _job(pool: asyncpg.Pool, *, blind: bool = False) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ('Eng', 'Build things.', $1) RETURNING id",
            blind,
        )
    return job_id


async def _set_blind(pool: asyncpg.Pool, job_id: UUID, blind: bool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET blind_review = $2 WHERE id = $1", job_id, blind
        )


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
    typed_cover: str | None = None,
    cover: bool = False,
    withdrawn: bool = False,
    minute: int = 0,
) -> None:
    data = _pdf(NAMES[idx], EMAILS[idx])
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
        async with conn.transaction():
            await pii.set_pii_key(conn)
            name_ct = await pii.encrypt(conn, NAMES[idx])
            typed_ct = await pii.encrypt(conn, typed_cover) if typed_cover else None
            await conn.execute(
                """
                INSERT INTO resumes (
                    job_id, blob_key, original_filename, mime_type,
                    file_size_bytes, sha256, consent_acknowledged, status,
                    candidate_name, parsed, cover_letter_blob_key,
                    cover_letter_sha256, cover_letter_text, uploaded_at,
                    withdrawn_at
                ) VALUES ($1, $2, 'cv.pdf', 'application/pdf', $3, $4, TRUE,
                          'parsed'::resume_status, $5, $6::jsonb, $7, $8, $9,
                          '2026-11-01T10:00:00Z'::timestamptz
                          + make_interval(mins => $10),
                          CASE WHEN $11 THEN now() END)
                """,
                job_id,
                key,
                len(data),
                uuid4().hex,
                name_ct,
                json.dumps(body),
                cover_key,
                cover_sha,
                typed_ct,
                minute,
                withdrawn,
            )


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


async def _row_count(pool: asyncpg.Pool, job_id: UUID, user: User) -> int:
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM resume_export_requests "
            "WHERE job_id = $1 AND requested_by = $2",
            job_id,
            user.cas_username,
        )
    return int(n)


def _counts(row: asyncpg.Record) -> dict[str, Any]:
    raw = row["counts"]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return dict(parsed or {})


async def _audit_details(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT details FROM audit_log WHERE action = 'resume_export'"
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        d = r["details"]
        out.append(json.loads(d) if isinstance(d, str) else dict(d or {}))
    return out


# ---------------------------------------------------------------- app / http


def _fake_arq() -> MagicMock:
    arq = MagicMock(name="arq")
    arq.enqueue_job = AsyncMock()
    return arq


def _build_app(
    pool: asyncpg.Pool, store: BlobStore, user: User, arq: MagicMock | None = None
) -> FastAPI:
    from src.api.routes import resume_export

    held = arq if arq is not None else _fake_arq()
    app = FastAPI()
    app.include_router(resume_export.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_arq] = lambda: held
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


async def _download(app: FastAPI, export_id: UUID | str) -> Response:
    return await _call(app, "POST", f"/resume-exports/{export_id}/download")


async def _run_worker(
    pool: asyncpg.Pool, store: BlobStore, export_id: UUID | str, **extra: Any
) -> str:
    from src.worker.export_tasks import build_resume_export

    ctx: dict[str, Any] = {"pg_pool": pool, "blob_store": store, **extra}
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


def _line(zf: zipfile.ZipFile, label: str) -> str:
    return next(ln for ln in _manifest(zf) if ln.startswith(label))


# ------------------------------------------------------- the rebuilt copy


@pytest.mark.asyncio
async def test_anonymized_entries_are_fresh_text_only_pdfs(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    from src.services.resume_export_service import PDF_PRODUCER, REDACTION_MARKER

    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0, cover=True)
    app, export_id = await _request_and_build(pool, store, await _user(pool), job_id)

    zf = _zip(await _download(app, export_id))

    entries = [n for n in zf.namelist() if n.startswith("anonymized/")]
    assert len(entries) == 2
    for entry in entries:
        doc = fitz.open(stream=zf.read(entry), filetype="pdf")
        try:
            assert doc.metadata["producer"] == PDF_PRODUCER
            fonts = {f[3] for n in range(doc.page_count) for f in doc.get_page_fonts(n)}
            assert fonts == {"Helvetica"}
            text = " ".join(str(p.get_text()) for p in doc)
            assert REDACTION_MARKER in text
            assert "recruiting" in text.lower()
            assert NAMES[0].lower() not in text.lower()
            for page in doc:
                assert page.get_images(full=True) == []
                assert page.get_drawings() == []
        finally:
            doc.close()


# ------------------------------------------------------ typed cover letters


@pytest.mark.parametrize("blind", [False, True])
@pytest.mark.asyncio
async def test_a_typed_cover_letter_is_noted_and_never_exported(
    pool: asyncpg.Pool, store: BlobStore, blind: bool
) -> None:
    job_id = await _job(pool, blind=blind)
    await _resume(pool, store, job_id, 0, typed_cover=TYPED, minute=0)
    await _resume(pool, store, job_id, 1, minute=1)
    app, export_id = await _request_and_build(pool, store, await _user(pool), job_id)

    zf = _zip(await _download(app, export_id))

    assert NOTE in _line(zf, "applicant-001")
    assert NOTE not in _line(zf, "applicant-002")
    assert not [n for n in zf.namelist() if "cover" in n]
    everything = b"".join(zf.read(n) for n in zf.namelist()).lower()
    assert b"keen to join" not in everything
    assert "anonymized: 2" in _manifest(zf)
    assert _counts(await _row(pool, export_id))["anonymized"] == 2


@pytest.mark.asyncio
async def test_a_cover_letter_file_is_exported_and_needs_no_typed_note(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0, cover=True)
    app, export_id = await _request_and_build(pool, store, await _user(pool), job_id)

    zf = _zip(await _download(app, export_id))

    assert "as-is/applicant-001_cover_letter.pdf" in zf.namelist()
    assert "typed in the app" not in "\n".join(_manifest(zf))


# ---------------------------------------------------------------- double click


@pytest.mark.parametrize("state", ["queued", "building"])
@pytest.mark.asyncio
async def test_a_second_request_returns_the_live_row_and_enqueues_nothing(
    pool: asyncpg.Pool, store: BlobStore, state: str
) -> None:
    job_id = await _job(pool)
    user = await _user(pool)
    live = await _insert_export(pool, job_id, user, status=state)
    arq = _fake_arq()

    resp = await _start(_build_app(pool, store, user, arq), job_id)

    assert resp.status_code == 202
    assert resp.json()["id"] == str(live)
    assert resp.json()["status"] == state
    arq.enqueue_job.assert_not_awaited()
    assert await _row_count(pool, job_id, user) == 1


@pytest.mark.asyncio
async def test_two_clicks_in_a_row_make_one_row_and_one_enqueue(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    user = await _user(pool)
    arq = _fake_arq()
    app = _build_app(pool, store, user, arq)

    first = await _start(app, job_id)
    second = await _start(app, job_id)

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert arq.enqueue_job.await_count == 1
    assert await _row_count(pool, job_id, user) == 1


@pytest.mark.asyncio
async def test_simultaneous_clicks_make_one_row_and_one_enqueue(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    user = await _user(pool)
    arq = _fake_arq()
    app = _build_app(pool, store, user, arq)

    responses = await asyncio.gather(*(_start(app, job_id) for _ in range(6)))

    assert {r.status_code for r in responses} == {202}
    assert len({r.json()["id"] for r in responses}) == 1
    assert arq.enqueue_job.await_count == 1
    assert await _row_count(pool, job_id, user) == 1


@pytest.mark.parametrize("state", ["ready", "failed", "expired"])
@pytest.mark.asyncio
async def test_a_finished_export_does_not_block_a_new_request(
    pool: asyncpg.Pool, store: BlobStore, state: str
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    user = await _user(pool)
    done = await _insert_export(
        pool,
        job_id,
        user,
        status=state,
        blob_key="resume_exports/old.zip" if state == "ready" else None,
        expires_in_min=30 if state == "ready" else None,
    )
    arq = _fake_arq()

    resp = await _start(_build_app(pool, store, user, arq), job_id)

    assert resp.status_code == 202
    assert resp.json()["id"] != str(done)
    assert resp.json()["status"] == "queued"
    arq.enqueue_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_another_requesters_or_jobs_live_row_does_not_block(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    other_job = await _job(pool)
    await _resume(pool, store, job_id, 0)
    me = await _user(pool)
    someone = await _user(pool)
    await _insert_export(pool, job_id, someone, status="queued")
    await _insert_export(pool, other_job, me, status="building")
    arq = _fake_arq()

    resp = await _start(_build_app(pool, store, me, arq), job_id)

    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    arq.enqueue_job.assert_awaited_once()
    assert await _row_count(pool, job_id, me) == 1


# ------------------------------------------------------------ worker restarts


@pytest.mark.parametrize("job_try", [2, 3])
@pytest.mark.asyncio
async def test_a_retried_build_accepts_a_row_that_is_already_building(
    pool: asyncpg.Pool, store: BlobStore, job_try: int
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    user = await _user(pool)
    export_id = await _insert_export(pool, job_id, user, status="building")

    out = await _run_worker(pool, store, export_id, job_try=job_try)

    assert out == "ready"
    row = await _row(pool, export_id)
    assert row["status"] == "ready"
    assert row["blob_key"].startswith("resume_exports/")
    zf = _zip(await _download(_build_app(pool, store, user), export_id))
    assert "anonymized/applicant-001_resume.pdf" in zf.namelist()


@pytest.mark.asyncio
async def test_a_retry_of_a_queued_row_builds_it_too(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    user = await _user(pool)
    export_id = await _insert_export(pool, job_id, user, status="queued")

    assert await _run_worker(pool, store, export_id, job_try=2) == "ready"


@pytest.mark.parametrize("extra", [{}, {"job_try": 1}])
@pytest.mark.asyncio
async def test_a_first_try_still_skips_a_row_that_is_already_building(
    pool: asyncpg.Pool, store: BlobStore, extra: dict[str, Any]
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    export_id = await _insert_export(pool, job_id, await _user(pool), status="building")

    assert await _run_worker(pool, store, export_id, **extra) == "skipped"
    assert (await _row(pool, export_id))["status"] == "building"


@pytest.mark.parametrize("state", ["ready", "failed", "expired"])
@pytest.mark.asyncio
async def test_a_retry_never_rebuilds_a_finished_export(
    pool: asyncpg.Pool, store: BlobStore, state: str
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)
    ready = state == "ready"
    export_id = await _insert_export(
        pool,
        job_id,
        await _user(pool),
        status=state,
        blob_key="resume_exports/kept.zip" if ready else None,
        expires_in_min=30 if ready else None,
    )

    assert await _run_worker(pool, store, export_id, job_try=2) == "skipped"
    row = await _row(pool, export_id)
    assert row["status"] == state
    assert row["blob_key"] == ("resume_exports/kept.zip" if ready else None)


# ------------------------------------------ blind review changed after the build


@pytest.mark.parametrize("blind", [False, True])
@pytest.mark.asyncio
async def test_the_claim_stores_the_jobs_blind_review(
    pool: asyncpg.Pool, store: BlobStore, blind: bool
) -> None:
    job_id = await _job(pool, blind=blind)
    await _resume(pool, store, job_id, 0)

    _app, export_id = await _request_and_build(pool, store, await _user(pool), job_id)

    assert (await _row(pool, export_id))["blind_review"] is blind


@pytest.mark.asyncio
async def test_a_job_made_blind_after_the_build_makes_the_download_409(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=False)
    await _resume(pool, store, job_id, 0)
    app, export_id = await _request_and_build(pool, store, await _user(pool), job_id)
    await _set_blind(pool, job_id, True)

    resp = await _download(app, export_id)

    assert resp.status_code == 409
    assert await _audit_details(pool) == []


@pytest.mark.asyncio
async def test_a_job_unblinded_after_a_blind_build_still_downloads_and_audits_true(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=True)
    await _resume(pool, store, job_id, 0)
    app, export_id = await _request_and_build(pool, store, await _user(pool), job_id)
    await _set_blind(pool, job_id, False)

    resp = await _download(app, export_id)

    assert resp.status_code == 200
    details = await _audit_details(pool)
    assert len(details) == 1
    assert details[0]["blind_review"] is True


@pytest.mark.asyncio
async def test_the_audit_records_the_stored_false_for_a_non_blind_build(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=False)
    await _resume(pool, store, job_id, 0)
    app, export_id = await _request_and_build(pool, store, await _user(pool), job_id)

    assert (await _download(app, export_id)).status_code == 200

    details = await _audit_details(pool)
    assert len(details) == 1
    assert details[0]["blind_review"] is False


async def _legacy_ready(
    pool: asyncpg.Pool, store: BlobStore, job_id: UUID, user: User
) -> UUID:
    """A ready row built before the column existed: ``blind_review`` is NULL."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("MANIFEST.txt", "applicants: 0\n")
    await store.put("resume_exports/legacy.zip", buf.getvalue(), "application/zip")
    return await _insert_export(
        pool,
        job_id,
        user,
        status="ready",
        blob_key="resume_exports/legacy.zip",
        expires_in_min=30,
    )


@pytest.mark.asyncio
async def test_a_legacy_row_with_no_stored_value_fails_closed_on_a_blind_job(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=True)
    user = await _user(pool)
    export_id = await _legacy_ready(pool, store, job_id, user)

    resp = await _download(_build_app(pool, store, user), export_id)

    assert resp.status_code == 409
    assert await _audit_details(pool) == []


@pytest.mark.asyncio
async def test_a_legacy_row_on_a_non_blind_job_downloads_and_audits_false(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=False)
    user = await _user(pool)
    export_id = await _legacy_ready(pool, store, job_id, user)

    resp = await _download(_build_app(pool, store, user), export_id)

    assert resp.status_code == 200
    details = await _audit_details(pool)
    assert len(details) == 1
    assert details[0]["blind_review"] is False


@pytest.mark.asyncio
async def test_the_column_is_a_nullable_boolean_and_is_added_to_an_old_table(
    pool: asyncpg.Pool,
) -> None:
    query = (
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'resume_export_requests' AND column_name = 'blind_review'"
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(query)
        assert row is not None
        assert row["data_type"] == "boolean"
        assert row["is_nullable"] == "YES"
        # An existing table that predates the column.
        await conn.execute(
            "ALTER TABLE resume_export_requests DROP COLUMN IF EXISTS blind_review"
        )
        assert await conn.fetchrow(query) is None

    await init_schema(pool)
    await init_schema(pool)  # and it is idempotent

    async with pool.acquire() as conn:
        row = await conn.fetchrow(query)
    assert row is not None
    assert row["data_type"] == "boolean"


# ------------------------------------------------------ the build-time cap


@pytest.mark.asyncio
async def test_over_the_cap_at_build_time_never_builds_more_than_the_cap(
    pool: asyncpg.Pool, store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, resume_export_max_applicants=2)
    job_id = await _job(pool)
    for idx in range(3):
        await _resume(pool, store, job_id, idx, minute=idx)
    # Inserted directly: the POST would have refused it with a 422.
    export_id = await _insert_export(pool, job_id, await _user(pool), status="queued")

    out = await _run_worker(pool, store, export_id)

    row = await _row(pool, export_id)
    if out == "ready":
        assert _counts(row)["applicants"] <= 2
        assert await store.exists(row["blob_key"])
    else:
        assert out == "failed"
        assert row["status"] == "failed"
        assert row["blob_key"] is None
        assert await store.list_keys("resume_exports/") == []


@pytest.mark.asyncio
async def test_exactly_at_the_cap_builds_everyone_and_withdrawn_do_not_count(
    pool: asyncpg.Pool, store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, resume_export_max_applicants=2)
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0, minute=0)
    await _resume(pool, store, job_id, 1, minute=1)
    await _resume(pool, store, job_id, 2, withdrawn=True, minute=2)
    export_id = await _insert_export(pool, job_id, await _user(pool), status="queued")

    assert await _run_worker(pool, store, export_id) == "ready"
    assert _counts(await _row(pool, export_id))["applicants"] == 2
