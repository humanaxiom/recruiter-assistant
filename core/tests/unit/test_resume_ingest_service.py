"""Characterization tests for ``src.services.resume_ingest_service.ingest_files``
(slice A1, RED -- the module does not exist yet, so this file fails at import).

``ingest_files`` is the body of ``src.api.routes.resumes.upload_resumes``
(zip expansion, combined-Taleo-export refusal, manifest parse, orphan-cover
demotion, pairing, ambiguity guard, ``resume_service.upload_resumes``, and the
rejected-row merge) extracted VERBATIM so the in-app Taleo split "accept" step
reuses it -- no second ingest path. Behaviour must be byte-identical.

**Chosen signature** (all arguments after ``blob_store`` are keyword-only)::

    async def ingest_files(
        conn: DbConn,
        blob_store: BlobStore,
        *,
        job_id: UUID,
        files: list[tuple[str, bytes]],      # (filename, bytes) as uploaded,
                                             # BEFORE zip expansion
        manifest_bytes: bytes | None = None, # raw pairing-manifest body
        consent_acknowledged: bool,          # already parsed to a bool
        uploaded_by: str | None = None,
        cover_letter_text: str | None = None,
        cover_letter_file: tuple[str, bytes] | None = None,
    ) -> list[ResumeUploadResult]

Contract pinned here:

* The route keeps everything HTTP: reading ``UploadFile`` bodies, mapping the
  consent string to a bool, and the ``arq.enqueue_job("parse_resume", ...)``
  loop AFTER the call (``ingest_files`` has no arq parameter).
* ``ingest_files`` raises ``FileRejectedError`` for an over-cap batch,
  ``ValueError`` for the cover-ambiguity guard and for ``consent=False`` (the
  route maps ``ValueError`` -> 422, exactly as today), and lets ``ManifestError``
  / ``NotFoundError`` / ``ZipRejected`` propagate.
* The returned list order is: résumé results, then manifest-missing rejected
  rows, then unattached (cover-shaped orphan) rows, then combined-export
  rejected rows.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import fitz  # type: ignore[import-untyped]
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.deps import Role, get_arq, resolve_role
from src.api.routes import resumes as resumes_routes
from src.errors import AppError, FileRejectedError, NotFoundError
from src.models.pool import get_db
from src.schemas.resumes import ResumeUploadResult
from src.services import resume_ingest_service
from src.services.bulk_ingest_service import ManifestError
from src.storage.blob_store import get_blob_store

_PDF_MAGIC = b"%PDF-1.4\nresume content\n" + b"x" * 500


# -- fakes (same shape as tests/unit/test_route_resumes.py) -----------------


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(*, job_exists: bool = True) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    async def _fetchval(query: str, *args: Any) -> Any:
        if query.strip() == "SELECT id FROM jobs WHERE id = $1":
            return uuid4() if job_exists else None
        return None

    conn.fetchval = AsyncMock(side_effect=_fetchval)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _mock_blob_store() -> MagicMock:
    store = MagicMock(name="blob_store")
    store.put = AsyncMock(return_value=None)
    return store


def _pdf(lines_per_page: list[list[str]]) -> bytes:
    doc = fitz.open()
    for lines in lines_per_page:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + i * 14), line, fontsize=10)
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _combined_export_pdf(n_pages: int = 12, n_emails: int = 3) -> bytes:
    pages = [
        [f"Applicant {i}", f"applicant{i % n_emails}@example.invalid", "604-555-0100"]
        for i in range(n_pages)
    ]
    return _pdf(pages)


def _cover_letter_pdf() -> bytes:
    return _pdf(
        [
            [
                "Dear Hiring Manager,",
                "",
                "I am writing to apply for the Software Engineer position.",
                "",
                "Sincerely,",
                "Pat Example",
            ]
        ]
    )


async def _ingest(
    conn: MagicMock,
    store: MagicMock,
    files: list[tuple[str, bytes]],
    **kw: Any,
) -> list[ResumeUploadResult]:
    kw.setdefault("consent_acknowledged", True)
    return await resume_ingest_service.ingest_files(
        conn, store, job_id=uuid4(), files=files, **kw
    )


def _accepted(rs: list[ResumeUploadResult]) -> list[ResumeUploadResult]:
    return [r for r in rs if r.outcome == "accepted"]


def _rejected(rs: list[ResumeUploadResult]) -> list[ResumeUploadResult]:
    return [r for r in rs if r.outcome == "rejected"]


# -- service-level characterization ----------------------------------------


@pytest.mark.asyncio
async def test_combined_export_is_rejected_and_nothing_is_ingested() -> None:
    """Pins resumes.py:153-175 (combined-export refusal -> rejected row)."""
    conn, store = _mock_conn(), _mock_blob_store()
    results = await _ingest(conn, store, [("export.pdf", _combined_export_pdf())])
    assert _accepted(results) == []
    rejected = _rejected(results)
    assert len(rejected) == 1
    assert rejected[0].original_filename == "export.pdf"
    assert rejected[0].resume_id is None
    assert rejected[0].reason is not None
    assert "split it first" in rejected[0].reason
    store.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_combined_export_does_not_poison_rest_of_batch_and_sorts_last() -> None:
    """Pins resumes.py:161-175 + :305 (rejected row appended LAST; the other
    file in the batch is still ingested)."""
    conn, store = _mock_conn(), _mock_blob_store()
    results = await _ingest(
        conn,
        store,
        [("export.pdf", _combined_export_pdf()), ("a.pdf", _PDF_MAGIC)],
    )
    assert [r.original_filename for r in _accepted(results)] == ["a.pdf"]
    assert results[-1].original_filename == "export.pdf"
    assert results[-1].outcome == "rejected"


@pytest.mark.asyncio
async def test_combined_export_inside_zip_is_also_rejected() -> None:
    """Pins resumes.py:132-139 + :161 (zip entries are expanded BEFORE the
    combined-export check)."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(zipfile.ZipInfo("export.pdf"), _combined_export_pdf())
        zf.writestr(zipfile.ZipInfo("ok.pdf"), _PDF_MAGIC)
    conn, store = _mock_conn(), _mock_blob_store()
    results = await _ingest(conn, store, [("batch.zip", buf.getvalue())])
    assert [r.original_filename for r in _accepted(results)] == ["ok.pdf"]
    assert [r.original_filename for r in _rejected(results)] == ["export.pdf"]


@pytest.mark.asyncio
async def test_manifest_pairs_cover_letter_to_its_resume() -> None:
    """Pins resumes.py:182-186 + :239-251 (manifest beats filename convention;
    cover_letter_filename surfaces on the accepted row)."""
    manifest = json.dumps(
        {
            "applicants": [
                {
                    "resume_file": "jane_resume.pdf",
                    "cover_letter_file": "jane_cover.pdf",
                    "cover_letter_flag": "Yes",
                }
            ]
        }
    ).encode()
    conn, store = _mock_conn(), _mock_blob_store()
    results = await _ingest(
        conn,
        store,
        [("jane_resume.pdf", _PDF_MAGIC), ("jane_cover.pdf", _PDF_MAGIC)],
        manifest_bytes=manifest,
    )
    accepted = _accepted(results)
    assert len(accepted) == 1
    assert accepted[0].original_filename == "jane_resume.pdf"
    assert accepted[0].cover_letter_filename == "jane_cover.pdf"
    assert accepted[0].resume_id is not None
    # The cover is NOT ingested as its own résumé.
    assert [r.original_filename for r in results] == ["jane_resume.pdf"]


@pytest.mark.asyncio
async def test_malformed_manifest_raises_manifest_error_before_persisting() -> None:
    """Pins resumes.py:184 (ManifestError propagates; nothing persisted)."""
    conn, store = _mock_conn(), _mock_blob_store()
    with pytest.raises(ManifestError):
        await _ingest(conn, store, [("a.pdf", _PDF_MAGIC)], manifest_bytes=b"{not json")
    store.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_manifest_naming_missing_file_surfaces_rejected_row() -> None:
    """Pins resumes.py:285-292 (pairing.rejected -> rejected rows)."""
    manifest = json.dumps(
        {
            "applicants": [
                {
                    "resume_file": "ghost_resume.pdf",
                    "cover_letter_file": None,
                    "cover_letter_flag": "No",
                }
            ]
        }
    ).encode()
    conn, store = _mock_conn(), _mock_blob_store()
    results = await _ingest(
        conn, store, [("a.pdf", _PDF_MAGIC)], manifest_bytes=manifest
    )
    assert [r.original_filename for r in _accepted(results)] == ["a.pdf"]
    missing = [
        r for r in _rejected(results) if r.original_filename == "ghost_resume.pdf"
    ]
    assert len(missing) == 1


@pytest.mark.asyncio
async def test_convention_pair_and_cover_shaped_orphan_is_unattached() -> None:
    """Pins resumes.py:202-226 + :297-304 (orphan cover-shaped file is a
    rejected 'unattached' row, never ingested as a résumé)."""
    conn, store = _mock_conn(), _mock_blob_store()
    results = await _ingest(
        conn,
        store,
        [
            ("x_resume.pdf", _PDF_MAGIC),
            ("x_cover_letter.pdf", _PDF_MAGIC),
            ("y_cover_letter.pdf", _cover_letter_pdf()),
        ],
    )
    accepted = _accepted(results)
    assert [r.original_filename for r in accepted] == ["x_resume.pdf"]
    assert accepted[0].cover_letter_filename == "x_cover_letter.pdf"
    rejected = _rejected(results)
    assert [r.original_filename for r in rejected] == ["y_cover_letter.pdf"]
    assert rejected[0].reason is not None
    assert "must never be ranked as a résumé" in rejected[0].reason
    # one résumé blob + its cover blob; the orphan was never stored
    keys = [c.args[0] for c in store.put.await_args_list]
    assert len(keys) == 2


@pytest.mark.asyncio
async def test_unreadable_orphan_cover_is_unattached_not_ingested() -> None:
    """Pins resumes.py:206-219 (fail CLOSED: extraction failure -> None ->
    unattached with 'could not be read')."""
    doc = fitz.open()
    doc.new_page()
    buf = BytesIO()
    doc.save(
        buf,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
        permissions=int(fitz.PDF_PERM_PRINT),
    )
    doc.close()
    conn, store = _mock_conn(), _mock_blob_store()
    results = await _ingest(conn, store, [("mystery_cover_letter.pdf", buf.getvalue())])
    assert _accepted(results) == []
    rejected = _rejected(results)
    assert len(rejected) == 1
    assert rejected[0].reason is not None
    assert "could not be read" in rejected[0].reason
    store.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_extraction_runs_only_for_orphan_cover_named_files() -> None:
    """Pins resumes.py:202-204 (security audit F2): extract_text is called
    from the NEW module, once, for the orphan only."""
    files: list[tuple[str, bytes]] = []
    for name in ("jane", "bob"):
        files.append((f"{name}_resume.pdf", _PDF_MAGIC))
        files.append((f"{name}_cover_letter.pdf", _PDF_MAGIC))
    files.append(("orphan_cover_letter.pdf", _PDF_MAGIC))
    fake = MagicMock(full_text="plain résumé-shaped text, not a cover letter")
    conn, store = _mock_conn(), _mock_blob_store()
    with patch.object(resume_ingest_service, "extract_text", return_value=fake) as m:
        results = await _ingest(conn, store, files)
    assert m.call_count == 1
    assert len(_accepted(results)) == 3  # jane, bob + orphan demoted to a résumé


@pytest.mark.asyncio
async def test_pairing_plus_singular_cover_is_ambiguous_value_error() -> None:
    """Pins resumes.py:253-265 (ambiguity guard; was HTTP 422, is now
    ValueError which the route maps to 422)."""
    conn, store = _mock_conn(), _mock_blob_store()
    with pytest.raises(ValueError, match="ambiguous"):
        await _ingest(
            conn,
            store,
            [("x_resume.pdf", _PDF_MAGIC), ("x_cover_letter.pdf", _PDF_MAGIC)],
            cover_letter_text="a pasted batch cover letter",
        )
    store.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_consent_false_raises_value_error_before_io() -> None:
    """Pins resumes.py:267-283 (consent gate -> ValueError from the service)."""
    conn, store = _mock_conn(), _mock_blob_store()
    with pytest.raises(ValueError):
        await _ingest(conn, store, [("a.pdf", _PDF_MAGIC)], consent_acknowledged=False)
    store.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_over_cap_batch_raises_file_rejected_before_reading() -> None:
    """Pins resumes.py:123-130 (count cap, FileRejectedError)."""
    conn, store = _mock_conn(), _mock_blob_store()
    too_many = [
        (f"r{i}.pdf", _PDF_MAGIC) for i in range(resumes_routes._MAX_UPLOAD_FILES + 1)
    ]
    with pytest.raises(FileRejectedError):
        await _ingest(conn, store, too_many)
    store.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_job_raises_not_found() -> None:
    """Pins resume_service.upload_resumes's job pre-check being reached
    through ingest_files (route :270)."""
    conn, store = _mock_conn(job_exists=False), _mock_blob_store()
    with pytest.raises(NotFoundError):
        await _ingest(conn, store, [("a.pdf", _PDF_MAGIC)])


@pytest.mark.asyncio
async def test_uploaded_by_and_job_id_reach_resume_service() -> None:
    """Pins resumes.py:268-281 (kwargs forwarded to resume_service)."""
    job_id = uuid4()
    conn, store = _mock_conn(), _mock_blob_store()
    spy = AsyncMock(return_value=[])
    with patch.object(resume_ingest_service.resume_service, "upload_resumes", spy):
        await resume_ingest_service.ingest_files(
            conn,
            store,
            job_id=job_id,
            files=[("a.pdf", _PDF_MAGIC)],
            consent_acknowledged=True,
            uploaded_by="alice",
            cover_letter_text="pasted",
        )
    kwargs = spy.await_args.kwargs
    assert kwargs["job_id"] == job_id
    assert kwargs["uploaded_by"] == "alice"
    assert kwargs["cover_letter_text"] == "pasted"
    assert kwargs["consent_acknowledged"] is True
    assert kwargs["files"] == [("a.pdf", _PDF_MAGIC)]


# -- route delegation ------------------------------------------------------


def _build_app(conn: MagicMock, arq: MagicMock, store: MagicMock) -> FastAPI:
    app = FastAPI()
    app.include_router(resumes_routes.router)

    async def _db() -> AsyncIterator[Any]:
        yield conn

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_arq] = lambda: arq
    app.dependency_overrides[resolve_role] = lambda: Role.ADMIN

    @app.exception_handler(AppError)
    async def _h(_request: Any, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.message, "code": exc.code}
        )

    return app


@pytest.mark.asyncio
async def test_route_delegates_to_ingest_files_and_enqueues_each_accepted() -> None:
    """Pins resumes.py:307-311: the route is a thin caller; the enqueue loop
    stays in the route, once per ACCEPTED result with a resume_id."""
    r1, r2 = uuid4(), uuid4()
    fake_results = [
        ResumeUploadResult(original_filename="a.pdf", outcome="accepted", resume_id=r1),
        ResumeUploadResult(original_filename="b.pdf", outcome="accepted", resume_id=r2),
        ResumeUploadResult(original_filename="c.pdf", outcome="duplicate"),
        ResumeUploadResult(original_filename="d.pdf", outcome="rejected", reason="x"),
    ]
    spy = AsyncMock(return_value=fake_results)
    conn, store = _mock_conn(), _mock_blob_store()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq, store)
    job_id = uuid4()
    manifest = b'{"applicants": []}'
    with patch.object(resume_ingest_service, "ingest_files", spy):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/jobs/{job_id}/resumes",
                files=[
                    ("files", ("a.pdf", _PDF_MAGIC, "application/pdf")),
                    (
                        "pairing_manifest",
                        ("manifest.json", manifest, "application/json"),
                    ),
                ],
                data={"consent_acknowledged": " TRUE ", "cover_letter_text": "hi"},
            )
    assert resp.status_code == 202
    assert [r["original_filename"] for r in resp.json()] == [
        "a.pdf",
        "b.pdf",
        "c.pdf",
        "d.pdf",
    ]
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["job_id"] == job_id
    assert kwargs["files"] == [("a.pdf", _PDF_MAGIC)]
    assert kwargs["manifest_bytes"] == manifest
    assert kwargs["consent_acknowledged"] is True
    assert kwargs["cover_letter_text"] == "hi"
    enqueued = [c.args for c in arq.enqueue_job.await_args_list]
    assert enqueued == [("parse_resume", str(r1)), ("parse_resume", str(r2))]


@pytest.mark.asyncio
async def test_route_maps_service_value_error_to_422_and_enqueues_nothing() -> None:
    """Pins resumes.py:282-283 (ValueError -> 422) for the extracted body."""
    spy = AsyncMock(side_effect=ValueError("ambiguous: nope"))
    conn, store = _mock_conn(), _mock_blob_store()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq, store)
    with patch.object(resume_ingest_service, "ingest_files", spy):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                f"/jobs/{uuid4()}/resumes",
                files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
                data={"consent_acknowledged": "true"},
            )
    assert resp.status_code == 422
    assert "ambiguous" in resp.json()["detail"]
    arq.enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_unparsed_consent_string_is_passed_as_false() -> None:
    """Pins resumes.py:267 (anything but 'true' -> False)."""
    spy = AsyncMock(return_value=[])
    conn, store = _mock_conn(), _mock_blob_store()
    arq = MagicMock(enqueue_job=AsyncMock())
    app = _build_app(conn, arq, store)
    with patch.object(resume_ingest_service, "ingest_files", spy):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.post(
                f"/jobs/{uuid4()}/resumes",
                files=[("files", ("a.pdf", _PDF_MAGIC, "application/pdf"))],
                data={"consent_acknowledged": "nope"},
            )
    assert spy.await_args.kwargs["consent_acknowledged"] is False
