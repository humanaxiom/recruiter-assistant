"""RED -- slice H: ``GET /jobs/{job_id}/resumes/export.zip`` against a REAL
Postgres (testcontainers) and a REAL filesystem ``BlobStore``.

The router is expected at ``src.api.routes.resume_export`` (attribute
``router``); it is imported inside ``_build_app`` so each test fails alone.

What a real Postgres proves that the unit tests cannot: the encrypted
``candidate_name`` really round-trips into the redaction term list, the
``withdrawn_at`` filter and creation ordering are real SQL, and the audit row
is really written with counts only.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg
import fitz  # type: ignore[import-untyped]
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient, Response
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, resolve_role
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.services import pii
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


@pytest.fixture
def store(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


def _pdf(name: str, email: str, extra: str = "") -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    lines = [
        name,
        f"{email}  604-555-01{len(name) % 90 + 10}",
        "Experienced in recruiting and operations.",
        f"Worked at {EMPLOYER}; studied at {SCHOOL}.",
        extra,
    ]
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
) -> bytes:
    """Seed one résumé (idx picks the fake identity); returns its PDF bytes."""
    data = _pdf(NAMES[idx], EMAILS[idx], extra=f"marker-{idx}")
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
    return data


def _build_app(
    pool: asyncpg.Pool, store: BlobStore, role: Role = Role.ADMIN
) -> FastAPI:
    from src.api.routes import resume_export  # RED: module does not exist yet

    app = FastAPI()
    app.include_router(resume_export.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[resolve_role] = lambda: role

    @app.exception_handler(AppError)
    async def _handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _download(app: FastAPI, job_id: UUID | str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(f"/jobs/{job_id}/resumes/export.zip")


def _zip(resp: Response) -> zipfile.ZipFile:
    assert resp.status_code == 200, resp.text
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _manifest(zf: zipfile.ZipFile) -> list[str]:
    return zf.read("MANIFEST.txt").decode("utf-8").splitlines()


@pytest.mark.asyncio
async def test_export_zip_contents_for_a_mixed_job(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    first = await _resume(pool, store, job_id, 0, minute=0)  # parsed
    await _resume(pool, store, job_id, 1, parsed=False, minute=1)  # unparsed
    await _resume(pool, store, job_id, 2, withdrawn=True, minute=2)  # withdrawn
    await _resume(pool, store, job_id, 3, cover=True, minute=3)  # with cover

    resp = await _download(_build_app(pool, store), job_id)
    zf = _zip(resp)

    assert resp.headers["content-type"].startswith("application/zip")
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
    assert "applicants: 3" in lines  # the withdrawn résumé is excluded
    assert "anonymized: 2" in lines
    assert "not anonymized: 1" in lines
    line = next(ln for ln in lines if ln.startswith("applicant-002"))
    assert "not anonymized" in line.lower()


@pytest.mark.asyncio
async def test_export_anonymized_copies_hold_no_identity_and_names_are_clean(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0, minute=0)
    await _resume(pool, store, job_id, 3, cover=True, minute=1)

    zf = _zip(await _download(_build_app(pool, store), job_id))

    anon = [n for n in zf.namelist() if n.startswith("anonymized/")]
    assert len(anon) == 3
    for entry in anon:
        flat = _flat(zf.read(entry))
        for secret in (NAMES[0], NAMES[3], *EMAILS, EMPLOYER, SCHOOL, "604-555"):
            assert secret.lower() not in flat, (entry, secret)
        assert "recruiting" in flat
    blob = "\n".join(zf.namelist()) + zf.read("MANIFEST.txt").decode()
    for secret in (*NAMES, *EMAILS, *ORIGINAL_NAMES, "mctestface", "frobnitz"):
        assert secret.lower() not in blob.lower(), secret


@pytest.mark.asyncio
async def test_export_numbering_follows_upload_order(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    # Insert the LATER one first; numbering must still follow uploaded_at.
    await _resume(pool, store, job_id, 1, minute=30)
    await _resume(pool, store, job_id, 0, minute=5)

    zf = _zip(await _download(_build_app(pool, store), job_id))

    assert "marker-0" in _flat(zf.read("as-is/applicant-001_resume.pdf"))
    assert "marker-1" in _flat(zf.read("as-is/applicant-002_resume.pdf"))


@pytest.mark.asyncio
async def test_blind_review_job_exports_anonymized_only(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=True)
    await _resume(pool, store, job_id, 0, cover=True, minute=0)
    await _resume(pool, store, job_id, 1, parsed=False, minute=1)

    zf = _zip(await _download(_build_app(pool, store), job_id))

    assert not [n for n in zf.namelist() if n.startswith("as-is/")]
    assert set(zf.namelist()) == {
        "MANIFEST.txt",
        "anonymized/applicant-001_resume.pdf",
        "anonymized/applicant-001_cover_letter.pdf",
    }
    assert "not anonymized: 1" in _manifest(zf)


@pytest.mark.asyncio
async def test_export_response_headers_carry_no_candidate_data(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)

    resp = await _download(_build_app(pool, store), job_id)

    disposition = resp.headers["content-disposition"]
    assert disposition.lower().startswith("attachment")
    assert f"job-{job_id}-resumes.zip" in disposition
    for secret in ("mctestface", "testy", "example"):
        assert secret not in disposition.lower()


@pytest.mark.asyncio
async def test_export_of_an_empty_job_is_a_manifest_only_zip(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    zf = _zip(await _download(_build_app(pool, store), job_id))
    assert zf.namelist() == ["MANIFEST.txt"]
    assert "applicants: 0" in _manifest(zf)


@pytest.mark.asyncio
async def test_export_for_an_unknown_job_is_404(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    resp = await _download(_build_app(pool, store), uuid4())
    assert resp.status_code == 404


@pytest.mark.parametrize("role", [Role.HIRING_MANAGER, Role.AUDITOR])
@pytest.mark.asyncio
async def test_export_is_403_for_non_writer_roles(
    pool: asyncpg.Pool, store: BlobStore, role: Role
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0)

    resp = await _download(_build_app(pool, store, role), job_id)

    assert resp.status_code == 403
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM audit_log WHERE action = 'resume_export'"
        )
    assert n == 0


@pytest.mark.asyncio
async def test_export_writes_one_audit_row_with_counts_only(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0, minute=0)
    await _resume(pool, store, job_id, 1, parsed=False, minute=1)
    await _resume(pool, store, job_id, 2, withdrawn=True, minute=2)

    resp = await _download(_build_app(pool, store), job_id)
    assert resp.status_code == 200

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT job_id, details, context FROM audit_log "
            "WHERE action = 'resume_export'"
        )
    assert len(rows) == 1
    assert rows[0]["job_id"] == job_id
    raw = str(rows[0]["details"]) + str(rows[0]["context"])
    d = rows[0]["details"]
    details = json.loads(d) if isinstance(d, str) else dict(d or {})
    assert 2 in details.values()  # applicants exported (withdrawn excluded)
    for secret in (*NAMES, *EMAILS, *ORIGINAL_NAMES, "mctestface", EMPLOYER, SCHOOL):
        assert secret.lower() not in raw.lower(), secret


@pytest.mark.asyncio
async def test_export_keeps_nothing_new_in_the_blob_store(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _resume(pool, store, job_id, 0, cover=True)
    before = sorted(await store.list_keys())

    await _download(_build_app(pool, store), job_id)

    assert sorted(await store.list_keys()) == before
