"""Slice D1 (RED) -- ``src.api.routes.resume_splits`` against a REAL Postgres
(testcontainers), a REAL filesystem ``BlobStore`` and a FAKE ``ArqRedis``.

The route module does not exist yet; it is imported inside ``_build_app`` so
each test fails individually instead of aborting collection.

What a real Postgres proves that ``tests/unit/test_resume_split_routes.py``
cannot: ``SELECT ... FOR UPDATE`` + version compare-and-set, the encrypted
proposal round trip, that an accepted/discarded row really holds no blob and a
NULL proposal, that accept RECOMPUTES diagnostics from the stored blob (the
stored ``diagnostics`` column is deliberately seeded clean on every blocked
case below, so a route that trusts it would wrongly ingest), and that ingest
goes through the one real ``resume_service`` path.

Contract pinned beyond the unit file: the correct body is
``{"proposal_version": n, "applicants": [{"resume_pages": [...],
"cover_letter_pages": [...]}], "ignored_pages": [...]}``; the accept/replay
response is either a JSON list of ``ResumeUploadResult``-shaped dicts or an
object carrying that list under ``results``.
"""

from __future__ import annotations

import hashlib
import json
import re
import textwrap
from collections.abc import AsyncIterator, Iterator
from io import BytesIO
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
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from src.api.deps import Role, get_arq, resolve_role
from src.errors import AppError
from src.models.ddl import init_schema
from src.models.pool import get_db
from src.schemas.taleo_split import (
    ProposalApplicant,
    ProposalDiagnostics,
    SplitProposal,
)
from src.services import pii
from src.services import split_request_service as srs
from src.storage.blob_store import BlobStore, get_blob_store

PII_KEY = "integration-test-pii-key-do-not-use-in-prod"
_PAD = " ".join(["Experience in recruiting and operations."] * 8)  # > 200 chars
_NAMES = ("Zzyzxqrst Wibblesworth", "Quxlyn Brannigrove", "Plorvik Tamsendale")
_EMAILS = (
    "zzyzxqrst.wibblesworth@example.test",
    "quxlyn.brannigrove@example.test",
    "plorvik.tamsendale@example.test",
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


# ── synthetic combined exports ───────────────────────────────────────────────


def _make_pdf(pages: list[str]) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        lines = [w for ln in text.splitlines() for w in (textwrap.wrap(ln, 80) or [""])]
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + i * 14), line, fontsize=9)
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _resume(email: str, marker: str) -> str:
    return f"Applicant {marker}\n{email}\n604-555-0100\nRESUME\n{_PAD}"


def _more(marker: str) -> str:
    return f"Continued {marker}\nSkills and projects\n{_PAD}"


def _cover(email: str, marker: str) -> str:
    return (
        f"{email}\nDear Hiring Manager,\nI am writing to apply {marker}.\n"
        f"{_PAD}\nSincerely,\nApplicant"
    )


def _short(email: str, marker: str) -> str:
    return f"Applicant {marker}\n{email}\nshort"  # well under 200 chars


def _four_page_export() -> tuple[bytes, SplitProposal]:
    """p1-2 applicant A resume, p3 A cover, p4 applicant B resume."""
    pdf = _make_pdf(
        [
            _resume(_EMAILS[0], "A"),
            _more("A"),
            _cover(_EMAILS[0], "A"),
            _resume(_EMAILS[1], "B"),
        ]
    )
    proposal = SplitProposal(
        applicants=[
            ProposalApplicant(
                candidate_name=_NAMES[0], resume_pages=[1, 2], cover_letter_pages=[3]
            ),
            ProposalApplicant(candidate_name=_NAMES[1], resume_pages=[4]),
        ]
    )
    return pdf, proposal


# ── db / app helpers ─────────────────────────────────────────────────────────


async def _job(pool: asyncpg.Pool, *, blind: bool = False) -> UUID:
    async with pool.acquire() as conn:
        job_id: UUID = await conn.fetchval(
            "INSERT INTO jobs (title, description_raw, blind_review) "
            "VALUES ('Eng', 'Build things.', $1) RETURNING id",
            blind,
        )
    return job_id


async def _seed_proposed(
    pool: asyncpg.Pool,
    store: BlobStore,
    job_id: UUID,
    pdf: bytes,
    proposal: SplitProposal,
    *,
    blind: bool = False,
) -> UUID:
    """A row in 'proposed' with the stored diagnostics deliberately CLEAN."""
    async with pool.acquire() as conn:
        split_id = await srs.create(
            conn,
            store,
            job_id=job_id,
            filename="export.pdf",
            data=pdf,
            consent_acknowledged=True,
            created_by="rec",
        )
        claim = await srs.claim(conn, split_id)
        assert claim is not None
        stored = await srs.store_proposal(
            conn,
            split_id,
            proposal,
            diagnostics=ProposalDiagnostics(),
            method="heuristic",
            fallback_reason="llm_unavailable",
            blind_review=blind,
            page_count=len(fitz.open(stream=pdf, filetype="pdf")),
        )
        assert stored
    return split_id


def _build_app(pool: asyncpg.Pool, store: BlobStore, arq: MagicMock) -> FastAPI:
    from src.api.routes import resume_splits  # RED: module does not exist yet

    app = FastAPI()
    app.include_router(resume_splits.router)

    async def _get_db_override() -> AsyncIterator[Any]:
        async with pool.acquire() as conn:
            yield conn

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_arq] = lambda: arq
    app.dependency_overrides[resolve_role] = lambda: Role.ADMIN

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _arq() -> MagicMock:
    return MagicMock(enqueue_job=AsyncMock())


async def _row(pool: asyncpg.Pool, split_id: UUID) -> asyncpg.Record:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM resume_split_requests WHERE id = $1", split_id
        )
    assert row is not None
    return row


async def _resume_rows(pool: asyncpg.Pool, job_id: UUID) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM resumes WHERE job_id = $1 ORDER BY original_filename",
            job_id,
        )


def _results(body: Any) -> list[dict[str, Any]]:
    items = body["results"] if isinstance(body, dict) else body
    assert isinstance(items, list)
    return [i for i in items if isinstance(i, dict)]


def _parse_calls(arq: MagicMock) -> list[Any]:
    return [c for c in arq.enqueue_job.await_args_list if c.args[0] == "parse_resume"]


async def _accept(app: FastAPI, split_id: UUID, version: int) -> Any:
    async with await _client(app) as client:
        return await client.post(
            f"/resume-splits/{split_id}/accept", json={"proposal_version": version}
        )


async def _version(pool: asyncpg.Pool, split_id: UUID) -> int:
    return int((await _row(pool, split_id))["proposal_version"])


# ── upload ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_creates_queued_row_blob_and_enqueues_after_commit(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, _ = _four_page_export()
    seen: list[tuple[str, str]] = []

    async def _enqueue(fn: str, split: str, *_a: Any, **_k: Any) -> None:
        # A DIFFERENT connection must already see the committed row.
        async with pool.acquire() as other:
            status = await other.fetchval(
                "SELECT status FROM resume_split_requests WHERE id = $1::uuid", split
            )
        seen.append((fn, str(status)))

    arq = MagicMock(enqueue_job=AsyncMock(side_effect=_enqueue))
    app = _build_app(pool, store, arq)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/resumes/split-upload",
            files=[("files", ("export.pdf", pdf, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 202
    assert seen == [("segment_taleo_export", "queued")]
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM resume_split_requests")
    assert len(rows) == 1
    assert str(rows[0]["id"]) in resp.text
    assert rows[0]["source_blob_key"].startswith("split_sources/")
    assert await store.exists(rows[0]["source_blob_key"])


@pytest.mark.parametrize(
    "payload",
    [b"just some text, not a pdf", b"%PDF-1.4\n" + b"\x00junk" * 50],
)
@pytest.mark.asyncio
async def test_upload_of_non_pdf_stores_nothing_and_enqueues_nothing(
    pool: asyncpg.Pool, store: BlobStore, payload: bytes
) -> None:
    job_id = await _job(pool)
    arq = _arq()
    app = _build_app(pool, store, arq)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{job_id}/resumes/split-upload",
            files=[("files", ("x.pdf", payload, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert 400 <= resp.status_code < 500
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM resume_split_requests") == 0
    assert await store.list_keys() == []
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_to_unknown_job_is_404_and_stores_nothing(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    pdf, _ = _four_page_export()
    arq = _arq()
    app = _build_app(pool, store, arq)
    async with await _client(app) as client:
        resp = await client.post(
            f"/jobs/{uuid4()}/resumes/split-upload",
            files=[("files", ("export.pdf", pdf, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert resp.status_code == 404
    assert await store.list_keys() == []
    arq.enqueue_job.assert_not_awaited()


# ── GET: shape, privacy ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_the_proposal_with_page_lists_and_version(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.get(f"/resume-splits/{split_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "proposed"
    assert body["proposal_version"] == await _version(pool, split_id)
    assert body["method"] == "heuristic"
    assert body["fallback_reason"] == "llm_unavailable"
    applicants = body["proposal"]["applicants"]
    assert [a["resume_pages"] for a in applicants] == [[1, 2], [4]]
    assert [a["cover_letter_pages"] for a in applicants] == [[3], []]
    # Non-blind: the (manager-visible) guessed name is shown; contrast for the
    # blind-review test below.
    assert _NAMES[0] in resp.text


@pytest.mark.asyncio
async def test_blind_review_get_has_no_candidate_names_anywhere(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=True)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal, blind=True)
    # Simulate a row that DID persist names (legacy / hand-edited): the
    # response must still not carry them on a blind-review job.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await pii.set_pii_key(conn)
            cipher = await pii.encrypt(conn, proposal.model_dump_json())
            await conn.execute(
                "UPDATE resume_split_requests SET proposal = $2 WHERE id = $1",
                split_id,
                cipher,
            )
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.get(f"/resume-splits/{split_id}")
    assert resp.status_code == 200
    assert "Applicant 1" in resp.text and "Applicant 2" in resp.text
    for name in _NAMES[:2]:
        assert name not in resp.text
        assert name.split()[0] not in resp.text
    for email in _EMAILS:
        assert email not in resp.text


@pytest.mark.parametrize("verb", ["GET", "accept", "correct", "discard"])
@pytest.mark.asyncio
async def test_unknown_split_is_404(
    pool: asyncpg.Pool, store: BlobStore, verb: str
) -> None:
    app = _build_app(pool, store, _arq())
    url = f"/resume-splits/{uuid4()}"
    async with await _client(app) as client:
        if verb == "GET":
            resp = await client.get(url)
        elif verb == "discard":
            resp = await client.post(f"{url}/discard")
        else:
            resp = await client.post(f"{url}/{verb}", json={"proposal_version": 1})
    assert resp.status_code == 404


# ── correct ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_correct_bumps_version_and_changes_the_stored_proposal(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    v0 = await _version(pool, split_id)
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{split_id}/correct",
            json={
                "proposal_version": v0,
                "applicants": [
                    {"resume_pages": [1, 2, 3], "cover_letter_pages": []},
                    {"resume_pages": [4], "cover_letter_pages": []},
                ],
                "ignored_pages": [],
            },
        )
        assert resp.status_code == 200
        got = (await client.get(f"/resume-splits/{split_id}")).json()
    assert await _version(pool, split_id) == v0 + 1
    assert got["proposal_version"] == v0 + 1
    assert got["proposal"]["applicants"][0]["resume_pages"] == [1, 2, 3]
    assert got["proposal"]["applicants"][0]["cover_letter_pages"] == []


@pytest.mark.asyncio
async def test_correct_with_stale_version_is_409_and_changes_nothing(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    v0 = await _version(pool, split_id)
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{split_id}/correct",
            json={
                "proposal_version": v0 - 1,
                "applicants": [{"resume_pages": [1, 2, 3, 4]}],
                "ignored_pages": [],
            },
        )
    assert resp.status_code == 409
    assert await _version(pool, split_id) == v0


@pytest.mark.asyncio
async def test_correct_with_out_of_range_page_is_4xx_and_changes_nothing(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    v0 = await _version(pool, split_id)
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.post(
            f"/resume-splits/{split_id}/correct",
            json={
                "proposal_version": v0,
                "applicants": [{"resume_pages": [1, 2, 3, 4, 99]}],
                "ignored_pages": [],
            },
        )
    assert 400 <= resp.status_code < 500 and resp.status_code != 409
    assert await _version(pool, split_id) == v0


# ── accept: happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_accept_ingests_resumes_pairs_covers_and_releases_blob(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    blob_key = (await _row(pool, split_id))["source_blob_key"]
    arq = _arq()
    app = _build_app(pool, store, arq)

    resp = await _accept(app, split_id, await _version(pool, split_id))

    assert resp.status_code == 200
    results = _results(resp.json())
    accepted = [r for r in results if r["outcome"] == "accepted"]
    assert len(accepted) == 2
    by_name = {r["original_filename"]: r for r in accepted}
    assert set(by_name) == {"applicant-001_resume.pdf", "applicant-002_resume.pdf"}
    assert (
        by_name["applicant-001_resume.pdf"]["cover_letter_filename"]
        == "applicant-001_cover_letter.pdf"
    )
    assert by_name["applicant-002_resume.pdf"]["cover_letter_filename"] is None
    # The rows are real and name-free on a NON-blind job too.
    rows = await _resume_rows(pool, job_id)
    assert [r["original_filename"] for r in rows] == sorted(by_name)
    assert rows[0]["cover_letter_blob_key"] is not None
    assert rows[1]["cover_letter_blob_key"] is None
    for r in rows:
        fname = r["original_filename"].lower()
        assert not any(n.split()[0].lower() in fname for n in _NAMES)
    # One parse task per ingested résumé, enqueued after the commit.
    assert len(_parse_calls(arq)) == 2
    # Retention: blob gone, proposal NULL, terminal state, result stored.
    row = await _row(pool, split_id)
    assert row["status"] == "accepted"
    assert row["source_blob_key"] is None
    assert row["proposal"] is None
    assert row["ingest_result"] is not None
    assert not await store.exists(blob_key)
    assert [k for k in await store.list_keys() if k.startswith("split_sources/")] == []


@pytest.mark.asyncio
async def test_accept_excludes_cover_only_and_low_text_and_discloses_them(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf = _make_pdf(
        [
            _resume(_EMAILS[0], "A"),  # 1  ok
            _cover(_EMAILS[1], "B"),  # 2  cover-only applicant
            _short(_EMAILS[2], "C"),  # 3  low-text applicant
            _resume("dana.fourth@example.test", "D"),  # 4  ok
        ]
    )
    proposal = SplitProposal(
        applicants=[
            ProposalApplicant(resume_pages=[1]),
            ProposalApplicant(cover_letter_pages=[2]),
            ProposalApplicant(resume_pages=[3]),
            ProposalApplicant(resume_pages=[4]),
        ]
    )
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    arq = _arq()
    app = _build_app(pool, store, arq)

    resp = await _accept(app, split_id, await _version(pool, split_id))

    assert resp.status_code == 200
    results = _results(resp.json())
    assert sum(1 for r in results if r["outcome"] == "accepted") == 2
    assert sum(1 for r in results if r["outcome"] == "rejected") == 2
    names = [r["original_filename"] for r in await _resume_rows(pool, job_id)]
    assert names == ["applicant-001_resume.pdf", "applicant-004_resume.pdf"]
    assert len(_parse_calls(arq)) == 2


@pytest.mark.asyncio
async def test_accept_of_more_than_25_applicants_with_covers_is_not_capped_at_50(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    n = 30  # 60 slices (resume + cover each) -- above the route's 50-file cap
    pages: list[str] = []
    applicants: list[ProposalApplicant] = []
    for i in range(n):
        email = f"person{i:02d}@example.test"
        pages += [_resume(email, f"R{i}"), _cover(email, f"C{i}")]
        applicants.append(
            ProposalApplicant(resume_pages=[2 * i + 1], cover_letter_pages=[2 * i + 2])
        )
    split_id = await _seed_proposed(
        pool, store, job_id, _make_pdf(pages), SplitProposal(applicants=applicants)
    )
    arq = _arq()
    app = _build_app(pool, store, arq)

    resp = await _accept(app, split_id, await _version(pool, split_id))

    assert resp.status_code == 200
    assert len(await _resume_rows(pool, job_id)) == n
    assert len(_parse_calls(arq)) == n


# ── accept: concurrency, idempotency, blocking ───────────────────────────────


@pytest.mark.asyncio
async def test_accept_with_stale_version_is_409_and_leaves_everything_untouched(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    v0 = await _version(pool, split_id)
    blob_key = (await _row(pool, split_id))["source_blob_key"]
    arq = _arq()
    app = _build_app(pool, store, arq)

    resp = await _accept(app, split_id, v0 - 1)

    assert resp.status_code == 409
    row = await _row(pool, split_id)
    assert row["status"] == "proposed"
    assert row["proposal"] is not None
    assert await store.exists(blob_key)
    assert await _resume_rows(pool, job_id) == []
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_double_accept_is_idempotent_no_second_ingest_or_enqueue(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    version = await _version(pool, split_id)
    arq = _arq()
    app = _build_app(pool, store, arq)

    first = await _accept(app, split_id, version)
    second = await _accept(app, split_id, version)

    assert first.status_code == 200
    assert second.status_code == 200
    assert _results(second.json()) == _results(first.json())
    assert len(await _resume_rows(pool, job_id)) == 2
    assert len(_parse_calls(arq)) == 2


@pytest.mark.parametrize("case", ["missing", "duplicated", "merged"])
@pytest.mark.asyncio
async def test_blocked_accept_is_409_and_leaves_blob_status_and_proposal(
    pool: asyncpg.Pool, store: BlobStore, case: str
) -> None:
    job_id = await _job(pool)
    pdf = _make_pdf(
        [
            _resume(_EMAILS[0], "A"),
            _more("A"),
            _resume(_EMAILS[1], "B"),
        ]
    )
    if case == "missing":  # page 3 assigned to nobody
        proposal = SplitProposal(applicants=[ProposalApplicant(resume_pages=[1, 2])])
    elif case == "duplicated":  # page 2 on two applicants
        proposal = SplitProposal(
            applicants=[
                ProposalApplicant(resume_pages=[1, 2]),
                ProposalApplicant(resume_pages=[2, 3]),
            ]
        )
    else:  # merged: one applicant spans two different emails (pages 1 and 3)
        proposal = SplitProposal(applicants=[ProposalApplicant(resume_pages=[1, 2, 3])])
    # Stored diagnostics are seeded CLEAN: only a recompute from the blob can
    # see the defect.
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    blob_key = (await _row(pool, split_id))["source_blob_key"]
    arq = _arq()
    app = _build_app(pool, store, arq)

    resp = await _accept(app, split_id, await _version(pool, split_id))

    assert resp.status_code == 409
    row = await _row(pool, split_id)
    assert row["status"] == "proposed"
    assert row["proposal"] is not None
    assert row["source_blob_key"] == blob_key
    assert await store.exists(blob_key)
    assert await _resume_rows(pool, job_id) == []
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignored_pages_count_as_accounted_for_on_accept(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf = _make_pdf([_resume(_EMAILS[0], "A"), "Blank separator page"])
    proposal = SplitProposal(
        applicants=[ProposalApplicant(resume_pages=[1])], ignored_pages=[2]
    )
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    app = _build_app(pool, store, _arq())

    resp = await _accept(app, split_id, await _version(pool, split_id))

    assert resp.status_code == 200
    assert len(await _resume_rows(pool, job_id)) == 1


# ── discard ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discard_deletes_blob_and_nulls_proposal(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    blob_key = (await _row(pool, split_id))["source_blob_key"]
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.post(f"/resume-splits/{split_id}/discard")
    assert 200 <= resp.status_code < 300
    row = await _row(pool, split_id)
    assert row["status"] == "discarded"
    assert row["source_blob_key"] is None
    assert row["proposal"] is None
    assert not await store.exists(blob_key)
    assert await _resume_rows(pool, job_id) == []


@pytest.mark.asyncio
async def test_discard_of_queued_split_is_allowed(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, _ = _four_page_export()
    async with pool.acquire() as conn:
        split_id = await srs.create(
            conn,
            store,
            job_id=job_id,
            filename="export.pdf",
            data=pdf,
            consent_acknowledged=True,
            created_by="rec",
        )
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.post(f"/resume-splits/{split_id}/discard")
    assert 200 <= resp.status_code < 300
    assert (await _row(pool, split_id))["status"] == "discarded"
    assert await store.list_keys() == []


@pytest.mark.asyncio
async def test_discard_of_accepted_or_discarded_split_is_409(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    accepted = await _seed_proposed(pool, store, job_id, pdf, proposal)
    app = _build_app(pool, store, _arq())
    ok = await _accept(app, accepted, await _version(pool, accepted))
    assert ok.status_code == 200
    discarded = await _seed_proposed(pool, store, job_id, pdf + b"\n%2", proposal)
    async with await _client(app) as client:
        first = await client.post(f"/resume-splits/{discarded}/discard")
        assert first.status_code < 300
        again = await client.post(f"/resume-splits/{discarded}/discard")
        after_accept = await client.post(f"/resume-splits/{accepted}/discard")
    assert again.status_code == 409
    assert after_accept.status_code == 409
    assert (await _row(pool, accepted))["status"] == "accepted"


# ── audit: counts / pages / sha256 only ──────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_rows_exist_for_each_action_and_carry_no_pii(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        up = await client.post(
            f"/jobs/{job_id}/resumes/split-upload",
            files=[("files", ("wibbles_export.pdf", pdf, "application/pdf"))],
            data={"consent_acknowledged": "true"},
        )
    assert up.status_code == 202
    uploaded = (await _all_split_ids(pool))[0]
    # Move the uploaded row to 'proposed' the way the worker would.
    async with pool.acquire() as conn:
        assert await srs.claim(conn, uploaded) is not None
        assert await srs.store_proposal(
            conn,
            uploaded,
            proposal,
            diagnostics=ProposalDiagnostics(),
            method="heuristic",
            fallback_reason="llm_unavailable",
            blind_review=False,
            page_count=4,
        )
    v = await _version(pool, uploaded)
    async with await _client(app) as client:
        corr = await client.post(
            f"/resume-splits/{uploaded}/correct",
            json={
                "proposal_version": v,
                "applicants": [
                    {"resume_pages": [1, 2], "cover_letter_pages": [3]},
                    {"resume_pages": [4], "cover_letter_pages": []},
                ],
                "ignored_pages": [],
            },
        )
    assert corr.status_code == 200
    assert (await _accept(app, uploaded, v + 1)).status_code == 200
    discard_target = await _seed_proposed(pool, store, job_id, pdf + b"\n%9", proposal)
    async with await _client(app) as client:
        await client.post(f"/resume-splits/{discard_target}/discard")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, details, context FROM audit_log "
            "WHERE action LIKE 'resume_split_%'"
        )
    actions = {r["action"] for r in rows}
    assert actions == {
        "resume_split_upload",
        "resume_split_correct",
        "resume_split_accept",
        "resume_split_discard",
    }
    forbidden = [*_NAMES, *_EMAILS, "wibbles", "Experience in recruiting", "RESUME"]
    for r in rows:
        blob = json.dumps(r["details"]) + str(r["context"])
        for needle in forbidden:
            assert needle not in blob, (r["action"], needle)
    upload_blob = next(
        json.dumps(r["details"]) for r in rows if r["action"] == "resume_split_upload"
    )
    assert hashlib.sha256(pdf).hexdigest() in upload_blob


async def _all_split_ids(pool: asyncpg.Pool) -> list[UUID]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM resume_split_requests ORDER BY created_at"
        )
    return [r["id"] for r in rows]


# ── C+D review fixes (RED) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blob_survives_when_the_accept_transaction_does_not_commit(
    pool: asyncpg.Pool, store: BlobStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 5: the combined PDF is deleted only AFTER the commit. A failure
    after ``mark_accepted`` inside the transaction must roll the row back to
    'proposed' AND leave the blob on disk, or the manager has lost the source
    of a proposal that still says it can be accepted."""
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    row0 = await _row(pool, split_id)
    blob_key = row0["source_blob_key"]
    real = srs.mark_accepted

    async def _then_fail(*args: Any, **kwargs: Any) -> bool:
        await real(*args, **kwargs)  # the UPDATE ran inside the transaction
        raise RuntimeError("commit failed")

    monkeypatch.setattr(srs, "mark_accepted", _then_fail)
    arq = _arq()
    app = _build_app(pool, store, arq)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/resume-splits/{split_id}/accept",
            json={"proposal_version": int(row0["proposal_version"])},
        )

    assert resp.status_code >= 500
    row = await _row(pool, split_id)
    assert row["status"] == "proposed"
    assert row["proposal"] is not None
    assert row["source_blob_key"] == blob_key
    assert await store.exists(blob_key)
    assert await _resume_rows(pool, job_id) == []
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_accept_still_deletes_the_blob_after_commit(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    blob_key = (await _row(pool, split_id))["source_blob_key"]
    app = _build_app(pool, store, _arq())
    resp = await _accept(app, split_id, await _version(pool, split_id))
    assert resp.status_code == 200
    assert (await _row(pool, split_id))["status"] == "accepted"
    assert not await store.exists(blob_key)


@pytest.mark.asyncio
async def test_misfiled_cover_accept_is_409_with_blob_and_proposal_intact(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    """Item 1 against a real database and a real stored PDF."""
    job_id = await _job(pool)
    pdf = _make_pdf(
        [
            _resume(_EMAILS[0], "A"),
            _resume(_EMAILS[1], "B"),  # B's contact page ...
            _more("B"),
        ]
    )
    proposal = SplitProposal(
        applicants=[
            ProposalApplicant(resume_pages=[1], cover_letter_pages=[2]),  # ... in A
            ProposalApplicant(resume_pages=[3]),
        ]
    )
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    blob_key = (await _row(pool, split_id))["source_blob_key"]
    arq = _arq()
    app = _build_app(pool, store, arq)

    resp = await _accept(app, split_id, await _version(pool, split_id))

    assert resp.status_code == 409
    row = await _row(pool, split_id)
    assert row["status"] == "proposed"
    assert row["proposal"] is not None
    assert await store.exists(blob_key)
    assert await _resume_rows(pool, job_id) == []
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreadable_source_blob_is_409_and_the_row_is_untouched(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    """Item 4 against a real row whose blob has vanished."""
    job_id = await _job(pool)
    pdf, proposal = _four_page_export()
    split_id = await _seed_proposed(pool, store, job_id, pdf, proposal)
    blob_key = (await _row(pool, split_id))["source_blob_key"]
    await store.delete(blob_key)
    v0 = await _version(pool, split_id)
    app = _build_app(pool, store, _arq())

    resp = await _accept(app, split_id, v0)

    assert resp.status_code == 409
    assert "source export unavailable" in resp.text.lower()
    row = await _row(pool, split_id)
    assert row["status"] == "proposed"
    assert int(row["proposal_version"]) == v0


# ── slice E5 (RED): GET /jobs/{job_id}/resume-splits ─────────────────────────


async def _seed_listing(
    pool: asyncpg.Pool, store: BlobStore, job_id: UUID
) -> dict[str, UUID]:
    """One row per status; created_at is staggered so 'newest first' is
    testable. Every row's filename is ``f-<status>.pdf``."""
    pdf, proposal = _four_page_export()
    ids: dict[str, UUID] = {}
    for status in ("accepted", "queued", "failed", "discarded", "segmenting"):
        async with pool.acquire() as conn:
            ids[status] = await srs.create(
                conn,
                store,
                job_id=job_id,
                filename="export.pdf",
                data=pdf + status.encode(),
                consent_acknowledged=True,
                created_by="rec",
            )
    ids["proposed"] = await _seed_proposed(pool, store, job_id, pdf, proposal)
    ages = {
        "accepted": 60,
        "discarded": 50,
        "failed": 40,
        "queued": 30,
        "segmenting": 20,
        "proposed": 10,
    }
    async with pool.acquire() as conn:
        for status, split_id in ids.items():
            await conn.execute(
                "UPDATE resume_split_requests SET status = $2, "
                "source_filename = $3, "
                "created_at = now() - make_interval(mins => $4) WHERE id = $1",
                split_id,
                status,
                f"f-{status}.pdf",
                ages[status],
            )
    return ids


@pytest.mark.asyncio
async def test_listing_returns_only_non_terminal_splits_newest_first(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    ids = await _seed_listing(pool, store, job_id)
    other_job = await _job(pool)
    await _seed_listing(pool, store, other_job)  # another job's rows never leak
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/resume-splits")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    assert [i["id"] for i in items] == [
        str(ids["proposed"]),
        str(ids["segmenting"]),
        str(ids["queued"]),
        str(ids["failed"]),
    ]
    assert [i["status"] for i in items] == [
        "proposed",
        "segmenting",
        "queued",
        "failed",
    ]
    for item in items:
        assert isinstance(item["created_at"], str)
        assert "page_count" in item and "method" in item
    assert items[0]["method"] == "heuristic"
    assert items[0]["page_count"] == 4
    assert [i["source_filename"] for i in items] == [
        "f-proposed.pdf",
        "f-segmenting.pdf",
        "f-queued.pdf",
        "f-failed.pdf",
    ]


@pytest.mark.asyncio
async def test_listing_never_carries_names_or_proposal_contents(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    await _seed_listing(pool, store, job_id)
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/resume-splits")
    assert resp.status_code == 200
    assert len(resp.json()) == 4
    for name in _NAMES:
        assert name not in resp.text
    for item in resp.json():
        assert "proposal" not in item
        assert "candidate_name" not in item
        assert "applicants" not in item


@pytest.mark.asyncio
async def test_listing_on_a_blind_job_has_no_filename(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool, blind=True)
    await _seed_listing(pool, store, job_id)
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/resume-splits")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 4
    assert "f-" not in resp.text and ".pdf" not in resp.text
    for item in items:
        assert item.get("source_filename") is None


@pytest.mark.asyncio
async def test_listing_with_no_splits_is_an_empty_list(
    pool: asyncpg.Pool, store: BlobStore
) -> None:
    job_id = await _job(pool)
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{job_id}/resume-splits")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_listing_unknown_job_is_404(pool: asyncpg.Pool, store: BlobStore) -> None:
    app = _build_app(pool, store, _arq())
    async with await _client(app) as client:
        resp = await client.get(f"/jobs/{uuid4()}/resume-splits")
    assert resp.status_code == 404
    assert resp.json()["code"] == "resource.not_found"  # not a missing route
