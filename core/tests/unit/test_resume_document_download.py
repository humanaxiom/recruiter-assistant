"""RED — serving the source PDFs (sponsor §O4).

*"The list should have Link to PDF coverletter and resume."*

There is currently **no blob download route in the entire API**: résumés and
cover letters are written to ``BlobStore`` and never served. This adds the
first route in the product that returns a raw candidate document, which makes
it the first new PII-egress surface since the pilot went live — so the whole
of this file is about bounding it.

**Every download is audited, blind or not.** ADR-016's option-C reasoning: the
audit trail is the control. A blind job's document plainly discloses identity
(the name is inside the PDF), so a download there is a reveal by another
route and must leave the same evidence. A non-blind job's document is *still*
a candidate record leaving the system, so it is audited too — one rule, no
branch, nothing to get wrong later.

**POST, not GET**, which is the decision most likely to look wrong. A download
link wants to be an ``href``; an ``href`` is prefetchable. Browsers, link
scanners and mail clients follow GETs speculatively, and each one would
manufacture an audit row attributing a disclosure to a person who never
clicked, and pull candidate PII into a cache. ``POST /resumes/{id}/reveal``
is POST-only for exactly this reason and this route inherits it.

**The ordering is the same as reveal's** — human-gate, scoping-404, audit,
then serve — so a blocked request never writes an audit row and never touches
a blob, and an unauthenticated caller cannot use the 403/404 split as an
existence oracle.

Nothing here exists yet — RED half of the TDD cycle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.errors import NotFoundError


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _conn(row: dict[str, Any] | None) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _store(data: bytes = b"%PDF-1.7 fake") -> MagicMock:
    return MagicMock(get=AsyncMock(return_value=data))


_ROW: dict[str, Any] = {
    "blob_key": "resumes/abc.pdf",
    "mime_type": "application/pdf",
    "original_filename": "Jane_Smith_Resume.pdf",
    "cover_letter_blob_key": "cover_letters/def.docx",
}


async def _read(kind: str, row: dict[str, Any] | None = _ROW, **kw: Any) -> Any:
    from src.services import resume_service

    return await resume_service.read_document(
        _conn(row),
        _store(kw.pop("data", b"%PDF-1.7 fake")),
        uuid4(),
        kind=kind,  # type: ignore[arg-type]
        user_id=kw.pop("user_id", None),
    )


# ------------------------------------------------------------ what comes back


@pytest.mark.asyncio
async def test_the_resume_document_is_returned_with_its_stored_mime() -> None:
    doc = await _read("resume")
    assert doc.data == b"%PDF-1.7 fake"
    assert doc.media_type == "application/pdf"


@pytest.mark.asyncio
async def test_the_cover_letter_mime_is_derived_from_its_key() -> None:
    """``resumes`` stores a ``mime_type`` for the résumé and NOTHING for the
    cover letter — there is no second column. The extension on the
    server-generated ``cover_letters/{uuid}.{ext}`` key is the only record of
    the type, so it is derived from there rather than guessed as PDF."""
    doc = await _read("cover_letter")
    assert doc.media_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


@pytest.mark.asyncio
async def test_an_unknown_extension_falls_back_to_a_generic_type() -> None:
    """Never guess ``application/pdf`` for an unrecognised key. A browser told
    a DOCX is a PDF renders garbage, and the recruiter's conclusion is "the
    product corrupted the file"."""
    row = {**_ROW, "cover_letter_blob_key": "cover_letters/def.bin"}
    doc = await _read("cover_letter", row)
    assert doc.media_type == "application/octet-stream"


@pytest.mark.asyncio
async def test_a_missing_kind_is_a_404_not_an_empty_file() -> None:
    """Most résumés have no cover letter. Returning an empty body with a 200
    would hand the recruiter a zero-byte file and no explanation."""
    row = {**_ROW, "cover_letter_blob_key": None}
    with pytest.raises(NotFoundError):
        await _read("cover_letter", row)


@pytest.mark.asyncio
async def test_a_missing_resume_is_a_404() -> None:
    with pytest.raises(NotFoundError):
        await _read("resume", None)


# ------------------------------------------------------------ the filename


@pytest.mark.asyncio
async def test_the_download_filename_does_not_leak_the_candidate() -> None:
    """``original_filename`` is candidate-supplied and routinely
    "Jane_Smith_Resume.pdf".

    The document's CONTENT discloses identity, and the download is audited
    precisely because it does — but the filename is a different surface: it
    lands in a downloads folder, a shared drive, an email attachment and a
    screenshot, detached from the audit row that justified it. So the served
    name is derived from the résumé id, and the extension is preserved so the
    file still opens.
    """
    doc = await _read("resume")
    assert "Jane" not in doc.filename
    assert "Smith" not in doc.filename
    assert doc.filename.endswith(".pdf")


# ------------------------------------------------------------- the audit row


@pytest.mark.asyncio
async def test_every_download_is_audited_including_a_non_blind_job() -> None:
    """One rule, no branch. A blind job's document discloses identity, so the
    download is a reveal by another route; a non-blind job's document is still
    a candidate record leaving the system. Auditing only the blind case would
    put the branch in the one place a future edit is most likely to get it
    backwards."""
    from src.services import audit_service, resume_service

    recorded: list[dict[str, Any]] = []

    async def _record(_conn: Any, **kw: Any) -> None:
        recorded.append(kw)

    original = audit_service.record_audit
    audit_service.record_audit = _record  # type: ignore[assignment]
    try:
        await resume_service.read_document(
            _conn(_ROW),
            _store(),
            uuid4(),
            kind="resume",
            user_id=None,
            actor_kind="user",
            actor_user_id=uuid4(),
            actor_service=None,
        )
    finally:
        audit_service.record_audit = original  # type: ignore[assignment]

    assert [r["action"] for r in recorded] == ["download_document"]
    assert recorded[0]["subject_type"] == "resume"
    assert recorded[0]["details"]["kind"] == "resume"


@pytest.mark.asyncio
async def test_a_blocked_request_reads_no_blob_and_writes_no_audit() -> None:
    """The ordering guarantee, inherited from reveal: scoping-404 comes BEFORE
    the audit write and before any blob read. A caller denied by row scoping
    must leave no trace and cause no disclosure."""
    from src.services import audit_service, resume_service

    recorded: list[dict[str, Any]] = []

    async def _record(_conn: Any, **kw: Any) -> None:
        recorded.append(kw)

    store = _store()
    original = audit_service.record_audit
    audit_service.record_audit = _record  # type: ignore[assignment]
    try:
        with pytest.raises(NotFoundError):
            await resume_service.read_document(
                _conn(None), store, uuid4(), kind="resume", user_id=uuid4()
            )
    finally:
        audit_service.record_audit = original  # type: ignore[assignment]

    assert recorded == []
    store.get.assert_not_awaited()


# ------------------------------------------------------------- the route shape


def _api_routes() -> list[Any]:
    """Every real API route, flattened.

    ``app.routes`` is NOT flat: routers are included as wrappers carrying an
    ``original_router``, so a top-level scan sees only Starlette's own
    /docs and /openapi.json. Same walker as
    ``test_write_route_session_gate.py`` -- written the naive way first here,
    which reported "no /document route is registered" while the route was
    registered perfectly well.
    """
    from src.api.main import app

    out: list[Any] = []

    def _walk(rs: Any) -> None:
        for r in rs:
            nested = getattr(r, "original_router", None)
            if nested is not None:
                _walk(nested.routes)
                continue
            if hasattr(r, "methods") and hasattr(r, "dependant"):
                out.append(r)

    _walk(app.routes)
    return out


def test_the_route_is_post_only() -> None:
    """A download link wants to be an href, and an href is prefetchable --
    browsers, link scanners and mail clients follow GETs speculatively. Each
    would manufacture an audit row attributing a disclosure to someone who
    never clicked, and pull candidate PII into a cache. ``POST
    /resumes/{id}/reveal`` is POST-only for this reason; this inherits it."""
    document = [r for r in _api_routes() if r.path.endswith("/document")]
    assert document, "no /document route is registered at all"
    methods = {m for r in document for m in (r.methods or set())}
    assert "GET" not in methods
    assert "POST" in methods
