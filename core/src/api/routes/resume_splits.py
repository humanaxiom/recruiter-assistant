"""In-app split of a combined Taleo export into per-applicant résumés + covers.

``POST /jobs/{id}/resumes/split-upload`` stores the combined PDF and queues a
segmentation task; the manager then reviews / corrects the proposal under
``/resume-splits/{id}`` and accepts it, which slices the stored PDF and feeds
the slices through the ONE ingest path (``resume_ingest_service.ingest_files``).

Every route is admin/recruiter on BOTH the API-key role and the CAS session
role (same gate as ``routes/resumes.py`` writes). Audit rows carry counts,
page numbers and the source sha256 only -- never a filename, name or text.
Blind-review jobs never emit candidate names or emails in any response.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Annotated, Any
from uuid import UUID

import fitz  # type: ignore[import-untyped]
from arq.connections import ArqRedis
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from src.api.deps import (
    Role,
    actor_fields_from_user,
    get_arq,
    require_role,
    require_session_role,
    resolve_user,
)
from src.errors import ConflictError, FileRejectedError, NotFoundError
from src.models.pool import Db
from src.schemas.auth import User
from src.schemas.resumes import ResumeUploadResult
from src.schemas.taleo_split import (
    ProposalApplicant,
    ProposalDiagnostics,
    SplitProposal,
)
from src.services import (
    audit_service,
    resume_ingest_service,
    split_request_service,
    taleo_split_service,
)
from src.settings import get_settings
from src.storage.blob_store import BlobStore, get_blob_store

router = APIRouter()

_WRITERS: tuple[Role, ...] = (Role.ADMIN, Role.RECRUITER)
_GATES = [
    Depends(require_role(*_WRITERS)),
    Depends(require_session_role(*_WRITERS)),
]
_MIB = 1024 * 1024

_SELECT_SQL = """
SELECT s.*, j.blind_review
FROM resume_split_requests s JOIN jobs j ON j.id = s.job_id
WHERE s.id = $1
"""
_LOCK_SQL = _SELECT_SQL + " FOR UPDATE OF s"


class _VersionBody(BaseModel):
    proposal_version: int


class _CorrectApplicant(BaseModel):
    candidate_name: str = Field(default="", max_length=200)
    resume_pages: list[int] = Field(default_factory=list)
    cover_letter_pages: list[int] = Field(default_factory=list)


class _CorrectBody(BaseModel):
    proposal_version: int
    applicants: list[_CorrectApplicant] = Field(default_factory=list)
    ignored_pages: list[int] = Field(default_factory=list)


def _json_value(raw: Any) -> Any:
    return json.loads(raw) if isinstance(raw, str | bytes) else raw


def _inspect_pdf(data: bytes) -> int:
    """Page count of a readable, unencrypted PDF; ``ValueError`` otherwise."""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("file is not a readable PDF") from exc
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise ValueError("encrypted PDFs cannot be split")
        return int(doc.page_count)
    finally:
        doc.close()


def _view(
    row: Any,
    proposal: SplitProposal | None,
    diagnostics: ProposalDiagnostics,
    *,
    version: int | None = None,
) -> dict[str, Any]:
    blind = bool(row["blind_review"])
    applicants: list[dict[str, Any]] = []
    if proposal is not None:
        for i, a in enumerate(proposal.applicants, start=1):
            applicants.append(
                {
                    "label": f"Applicant {i}",
                    "candidate_name": "" if blind else a.candidate_name,
                    "resume_pages": list(a.resume_pages),
                    "cover_letter_pages": list(a.cover_letter_pages),
                }
            )
    return {
        "id": str(row["id"]),
        "job_id": str(row["job_id"]),
        "status": row["status"],
        "proposal_version": version if version is not None else row["proposal_version"],
        "method": row["method"],
        "fallback_reason": row["fallback_reason"],
        "failure_reason": row["failure_reason"],
        "page_count": row["page_count"],
        "source_filename": None if blind else row["source_filename"],
        "blind_review": blind,
        "created_at": row["created_at"],
        "proposal": (
            None
            if proposal is None
            else {"applicants": applicants, "ignored_pages": proposal.ignored_pages}
        ),
        "diagnostics": diagnostics.model_dump(),
    }


async def _audit(
    db: Db,
    user: User | None,
    *,
    action: str,
    split_id: UUID,
    job_id: UUID | None,
    details: dict[str, Any],
) -> None:
    actor_kind, actor_user_id, actor_service = actor_fields_from_user(user)
    await audit_service.record_audit(
        db,
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        actor_service=actor_service,
        action=action,
        subject_type="resume_split_request",
        subject_id=split_id,
        job_id=job_id,
        details=details,
    )


def _not_found(split_id: UUID) -> NotFoundError:
    return NotFoundError(
        f"resume split {split_id} not found", resume_split_id=str(split_id)
    )


@router.post(
    "/jobs/{job_id}/resumes/split-upload",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=_GATES,
)
async def split_upload(
    job_id: UUID,
    db: Db,
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    arq: Annotated[ArqRedis, Depends(get_arq)],
    user: Annotated[User | None, Depends(resolve_user)],
    files: Annotated[list[UploadFile], File()],
    consent_acknowledged: Annotated[str, Form()],
) -> dict[str, Any]:
    """Validate every file BEFORE storing any, then store + queue each one."""
    settings = get_settings()
    if consent_acknowledged.strip().lower() != "true":
        raise HTTPException(
            status_code=422, detail="consent must be acknowledged before upload"
        )
    if not files:
        raise HTTPException(status_code=422, detail="no file supplied")
    if len(files) > settings.split_max_files:
        raise FileRejectedError(
            f"upload has {len(files)} files; the per-request cap is "
            f"{settings.split_max_files}",
            count=len(files),
        )
    if await db.fetchval("SELECT 1 FROM jobs WHERE id = $1", job_id) is None:
        raise NotFoundError(f"job {job_id} not found", job_id=str(job_id))

    max_bytes = settings.split_max_file_mib * _MIB
    checked: list[tuple[str, bytes, int]] = []
    for f in files:
        name = f.filename or "export.pdf"
        if not name.lower().endswith(".pdf"):
            raise HTTPException(status_code=415, detail="only PDF files can be split")
        data = await f.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise FileRejectedError(
                f"file exceeds the {settings.split_max_file_mib} MiB cap",
                max_mib=settings.split_max_file_mib,
            )
        if not data.startswith(b"%PDF-"):
            raise HTTPException(status_code=415, detail="file is not a PDF")
        try:
            pages = await asyncio.to_thread(_inspect_pdf, data)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if pages > settings.split_max_pages:
            raise FileRejectedError(
                f"export has {pages} pages; the cap is {settings.split_max_pages}",
                pages=pages,
            )
        checked.append((name, data, pages))

    created_by = user.cas_username if user is not None else None
    ids: list[UUID] = []
    for name, data, pages in checked:
        split_id = await split_request_service.create(
            db,
            blob_store,
            job_id=job_id,
            filename=name,
            data=data,
            consent_acknowledged=True,
            created_by=created_by,
        )
        ids.append(split_id)
        await _audit(
            db,
            user,
            action="resume_split_upload",
            split_id=split_id,
            job_id=job_id,
            details={
                "pages": pages,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        )
    # Enqueue only after every row is created (and committed: no wrapping tx).
    for split_id in ids:
        await arq.enqueue_job("segment_taleo_export", str(split_id))
    return {
        "splits": [{"id": str(i), "status": "queued"} for i in ids],
        "split_ids": [str(i) for i in ids],
    }


@router.get("/resume-splits/{split_id}", dependencies=_GATES)
async def get_split(split_id: UUID, db: Db) -> dict[str, Any]:
    row = await db.fetchrow(_SELECT_SQL, split_id)
    if row is None:
        raise _not_found(split_id)
    proposal: SplitProposal | None = None
    if row["proposal"] is not None:
        proposal = await split_request_service.load_proposal(db, split_id)
    raw = _json_value(row["diagnostics"])
    diagnostics = ProposalDiagnostics.model_validate(raw if raw else {})
    return _view(row, proposal, diagnostics)


@router.post("/resume-splits/{split_id}/correct", dependencies=_GATES)
async def correct_split(
    split_id: UUID,
    body: _CorrectBody,
    db: Db,
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    user: Annotated[User | None, Depends(resolve_user)],
) -> dict[str, Any]:
    async with db.transaction():
        row = await db.fetchrow(_LOCK_SQL, split_id)
        if row is None:
            raise _not_found(split_id)
        if row["status"] != "proposed":
            raise ConflictError(
                f"split is '{row['status']}', not 'proposed'", status=row["status"]
            )
        if body.proposal_version != row["proposal_version"]:
            raise ConflictError(
                "the proposal changed since it was loaded; reload and retry",
                current_version=row["proposal_version"],
            )
        blob = await blob_store.get(row["source_blob_key"])
        texts = await asyncio.to_thread(taleo_split_service.extract_page_texts, blob)
        page_count = len(texts)
        every_page = list(body.ignored_pages)
        for a in body.applicants:
            every_page.extend(a.resume_pages)
            every_page.extend(a.cover_letter_pages)
        if any(not 1 <= p <= page_count for p in every_page):
            raise HTTPException(
                status_code=422, detail=f"pages must be within 1..{page_count}"
            )
        blind = bool(row["blind_review"])
        proposal = SplitProposal(
            applicants=[
                ProposalApplicant(
                    candidate_name="" if blind else a.candidate_name,
                    resume_pages=a.resume_pages,
                    cover_letter_pages=a.cover_letter_pages,
                )
                for a in body.applicants
            ],
            ignored_pages=body.ignored_pages,
        )
        diagnostics = await asyncio.to_thread(
            taleo_split_service.validate_proposal, proposal, texts
        )
        version = await split_request_service.store_correction(
            db,
            split_id,
            proposal,
            expected_version=body.proposal_version,
            diagnostics=diagnostics,
            blind_review=blind,
        )
        if version is None:
            raise ConflictError("the proposal changed; reload and retry")
    await _audit(
        db,
        user,
        action="resume_split_correct",
        split_id=split_id,
        job_id=row["job_id"],
        details={
            "applicants": len(proposal.applicants),
            "ignored_pages": len(proposal.ignored_pages),
            "proposal_version": version,
        },
    )
    return _view(row, proposal, diagnostics, version=version)


@router.post("/resume-splits/{split_id}/accept", dependencies=_GATES)
async def accept_split(
    split_id: UUID,
    body: _VersionBody,
    db: Db,
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    arq: Annotated[ArqRedis, Depends(get_arq)],
    user: Annotated[User | None, Depends(resolve_user)],
) -> list[dict[str, Any]]:
    """Slice the stored export and ingest it. Idempotent: an already-accepted
    split replays its stored result and ingests nothing."""
    settings = get_settings()
    results: list[ResumeUploadResult]
    async with db.transaction():
        row = await db.fetchrow(_LOCK_SQL, split_id)
        if row is None:
            raise _not_found(split_id)
        if row["status"] == "accepted":
            stored = _json_value(row["ingest_result"])
            return list(stored) if stored else []
        if row["status"] != "proposed":
            raise ConflictError(
                f"split is '{row['status']}', not 'proposed'", status=row["status"]
            )
        if body.proposal_version != row["proposal_version"]:
            raise ConflictError(
                "the proposal changed since it was loaded; reload and retry",
                current_version=row["proposal_version"],
            )
        proposal = await split_request_service.load_proposal(db, split_id)
        if proposal is None:
            raise ConflictError("the split has no stored proposal")
        blob = await blob_store.get(row["source_blob_key"])
        texts = await asyncio.to_thread(taleo_split_service.extract_page_texts, blob)
        # Recompute from the stored blob; the stored diagnostics are advisory.
        diagnostics = await asyncio.to_thread(
            taleo_split_service.validate_proposal, proposal, texts
        )
        if diagnostics.blocking:
            raise ConflictError(
                "the proposal has missing, duplicated or merged pages",
                missing=diagnostics.missing,
                duplicated=diagnostics.duplicated,
                merged=diagnostics.merged,
            )
        excluded_cover = set(diagnostics.cover_only)
        excluded_low = set(diagnostics.low_text)
        slices = await asyncio.to_thread(
            taleo_split_service.build_slices,
            blob,
            proposal,
            excluded_cover | excluded_low,
        )
        if not slices:
            raise ConflictError("no applicant has enough résumé text to ingest")
        files: list[tuple[str, bytes]] = []
        for s in slices:
            files.append((s.resume_filename, s.resume_pdf))
            if s.cover_filename is not None and s.cover_pdf is not None:
                files.append((s.cover_filename, s.cover_pdf))
        manifest = taleo_split_service.build_pairing_manifest(slices)
        uploaded_by = user.cas_username if user is not None else None
        results = await resume_ingest_service.ingest_files(
            db,
            blob_store,
            job_id=row["job_id"],
            files=files,
            manifest_bytes=manifest,
            consent_acknowledged=bool(row["consent_acknowledged"]),
            uploaded_by=uploaded_by,
            max_files=2 * settings.split_max_pages,
        )
        for idx in sorted(excluded_cover):
            results.append(
                ResumeUploadResult(
                    original_filename=f"applicant-{idx:03d}_cover_letter.pdf",
                    outcome="rejected",
                    reason="applicant has only cover-letter pages; not ingested",
                )
            )
        for idx in sorted(excluded_low - excluded_cover):
            results.append(
                ResumeUploadResult(
                    original_filename=f"applicant-{idx:03d}_resume.pdf",
                    outcome="rejected",
                    reason="too little extractable text; not ingested",
                )
            )
        dumped = [r.model_dump(mode="json") for r in results]
        if not await split_request_service.mark_accepted(
            db,
            blob_store,
            split_id,
            accepted_by=uploaded_by,
            ingest_result=dumped,
        ):
            raise ConflictError("the split was changed by another request")

    await _audit(
        db,
        user,
        action="resume_split_accept",
        split_id=split_id,
        job_id=row["job_id"],
        details={
            "applicants": len(proposal.applicants),
            "ingested": sum(1 for r in results if r.outcome == "accepted"),
            "rejected": sum(1 for r in results if r.outcome == "rejected"),
            "pages": len(texts),
            "sha256": row["source_sha256"],
        },
    )
    for r in results:
        if r.outcome == "accepted" and r.resume_id is not None:
            await arq.enqueue_job("parse_resume", str(r.resume_id))
    return dumped


@router.post("/resume-splits/{split_id}/discard", dependencies=_GATES)
async def discard_split(
    split_id: UUID,
    db: Db,
    blob_store: Annotated[BlobStore, Depends(get_blob_store)],
    user: Annotated[User | None, Depends(resolve_user)],
) -> dict[str, str]:
    row = await db.fetchrow(_SELECT_SQL, split_id)
    if row is None:
        raise _not_found(split_id)
    if not await split_request_service.discard(db, blob_store, split_id):
        raise ConflictError(
            f"split is '{row['status']}' and cannot be discarded", status=row["status"]
        )
    await _audit(
        db,
        user,
        action="resume_split_discard",
        split_id=split_id,
        job_id=row["job_id"],
        details={"pages": row["page_count"], "sha256": row["source_sha256"]},
    )
    return {"id": str(split_id), "status": "discarded"}


__all__ = ["router"]
