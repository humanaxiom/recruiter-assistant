"""Résumé batch ingest, extracted from ``routes/resumes.py::upload_resumes``.

Zip expansion, combined-Taleo-export refusal, manifest parse, orphan-cover
demotion, pairing, the ambiguity guard, ``resume_service.upload_resumes`` and
the rejected-row merge. HTTP concerns (reading ``UploadFile`` bodies, mapping
errors to status codes, the arq enqueue loop) stay in the route; the in-app
Taleo split "accept" step reuses this function so there is one ingest path.

Raises ``FileRejectedError`` (over-cap batch), ``ValueError`` (ambiguity guard,
consent gate), and lets ``ManifestError`` / ``NotFoundError`` / ``ZipRejected``
propagate.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID

from src.errors import FileRejectedError
from src.pipeline.parsing.extract import extract_text
from src.schemas.resumes import ResumeUploadResult
from src.services import DbConn, bulk_ingest_service, combined_export, resume_service
from src.services.zip_upload import _MAX_ZIP_ENTRIES, expand_zip_entries
from src.storage.blob_store import BlobStore

_MAX_UPLOAD_FILES = _MAX_ZIP_ENTRIES


async def ingest_files(
    conn: DbConn,
    blob_store: BlobStore,
    *,
    job_id: UUID,
    files: list[tuple[str, bytes]],
    manifest_bytes: bytes | None = None,
    consent_acknowledged: bool,
    uploaded_by: str | None = None,
    cover_letter_text: str | None = None,
    cover_letter_file: tuple[str, bytes] | None = None,
    extract: Callable[..., Any] | None = None,
    max_files: int = _MAX_UPLOAD_FILES,
) -> list[ResumeUploadResult]:
    # ``extract`` lets the route inject ITS ``extract_text`` binding so the
    # pre-extraction route tests (which patch ``routes.resumes.extract_text``)
    # keep exercising the real path; default is this module's own binding.
    if len(files) > max_files:
        raise FileRejectedError(
            f"upload has {len(files)} files; the per-request cap is {max_files}",
            count=len(files),
        )

    expanded: list[tuple[str, bytes]] = []
    for filename, data in files:
        if filename.lower().endswith(".zip"):
            expanded.extend(expand_zip_entries(data))
        else:
            expanded.append((filename, data))

    # Combined-Taleo-export detection (refuse, don't mis-ingest); off the event
    # loop (PyMuPDF is sync/CPU-bound).
    combined_rejected: list[ResumeUploadResult] = []
    kept: list[tuple[str, bytes]] = []
    for name, data in expanded:
        reason: str | None = None
        if name.lower().endswith(".pdf"):
            reason = await asyncio.to_thread(
                combined_export.looks_like_combined_export, data, "application/pdf"
            )
        if reason is not None:
            combined_rejected.append(
                ResumeUploadResult(
                    original_filename=name, outcome="rejected", reason=reason
                )
            )
        else:
            kept.append((name, data))
    expanded = kept

    manifest: dict[str, str | None] | None = None
    if manifest_bytes is not None:
        manifest = bulk_ingest_service.parse_pairing_manifest(manifest_bytes)

    # Orphaned cover-NAMED files must never be promoted to a résumé when their
    # actual text reads as a cover letter (ADR-017 amendment). Extraction runs
    # ONLY on leftover cover-named files (security audit F2); ``None`` means
    # "could not extract" and is never treated as "not cover-shaped" (F3).
    orphan_covers = bulk_ingest_service.leftover_cover_files(
        expanded, manifest=manifest
    )
    cover_text_map: dict[str, str | None] = {}
    for cover_name, cover_data in orphan_covers:
        try:
            cover_mime = resume_service.detect_mime(cover_name, cover_data)
            extracted = await asyncio.to_thread(
                extract or extract_text, cover_data, cover_mime
            )
            cover_text_map[bulk_ingest_service.basename_lower(cover_name)] = (
                extracted.full_text
            )
        except Exception:
            cover_text_map[bulk_ingest_service.basename_lower(cover_name)] = None

    def _is_cover_content(f: tuple[str, bytes]) -> bool | None:
        key = bulk_ingest_service.basename_lower(f[0])
        text = cover_text_map.get(key, "")
        if text is None:
            return None
        return combined_export.is_cover_letter_text(text)

    pairing = bulk_ingest_service.pair_applicants(
        expanded, manifest=manifest, is_cover_content=_is_cover_content
    )
    resume_files: list[tuple[str, bytes]] = []
    cover_letter_map: dict[str, tuple[str, bytes]] = {}
    warnings_map: dict[str, list[str]] = {}
    for pair in pairing.pairs:
        key = bulk_ingest_service.basename_lower(pair.resume[0])
        resume_files.append(pair.resume)
        if pair.cover_letter is not None:
            cover_letter_map[key] = pair.cover_letter
        if pair.note:
            warnings_map.setdefault(key, []).append(pair.note)

    # Ambiguity guard: never silently pick between per-résumé pairing and a
    # singular batch cover letter.
    has_singular_cover = cover_letter_file is not None or bool(
        cover_letter_text and cover_letter_text.strip()
    )
    if cover_letter_map and has_singular_cover:
        raise ValueError(
            "ambiguous: both per-résumé cover pairing and a single batch "
            "cover letter were supplied"
        )

    results = await resume_service.upload_resumes(
        conn,
        blob_store,
        job_id=job_id,
        files=resume_files,
        consent_acknowledged=consent_acknowledged,
        uploaded_by=uploaded_by,
        cover_letter_text=cover_letter_text,
        cover_letter_file=cover_letter_file,
        cover_letter_map=cover_letter_map or None,
        warnings_map=warnings_map or None,
    )

    for missing_name, reason_text in pairing.rejected:
        results.append(
            ResumeUploadResult(
                original_filename=missing_name, outcome="rejected", reason=reason_text
            )
        )
    for unattached_name, unattached_reason in pairing.unattached:
        results.append(
            ResumeUploadResult(
                original_filename=unattached_name,
                outcome="rejected",
                reason=unattached_reason,
            )
        )
    results.extend(combined_rejected)
    return results
