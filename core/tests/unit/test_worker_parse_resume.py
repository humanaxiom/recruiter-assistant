"""Unit tests for ``src.worker.resume_tasks`` — all I/O mocked.

Same real-ctx-keys discipline as ``test_worker_parse_job.py``: ``ctx`` is
built from ``pg_pool`` / ``llm`` / ``embedder`` / ``blob_store`` (the keys
``src/worker/main.py::startup`` actually sets), never hris's ``ctx["minio"]``/
``ctx["minio_bucket_resumes_raw"]``.

``blob_store`` is the Phase-1 ``BlobStore`` — ``.get(key)`` is already async
and takes a single relative key, no bucket. Every test that reaches the blob
fetch asserts it goes through ``blob_store.get(meta["blob_key"])`` and that
nothing calls a MinIO-style ``get_object``.

Covers:
* ``parse_resume`` control flow: missing/stale/blob-failure/extract-failure/
  zero-chunks/core-LLM-failure/happy-path/race-guard/ResumeParsed-validation-
  failure (finding 2b)/outbox-payload-excludes-candidate (finding 5).
* ``_drop_smeared_years``: the >=6-shared-exact-value boundary (6 triggers,
  5 does not), unrelated skills untouched.
* ``_extract_skills_merged``: deterministic-scan-floor MERGED with a
  non-fatal best-effort LLM call, first-non-null-wins on canonical
  collision, capped at 80.
* ``_parse_cover_letter``: absent (no I/O), blob branch (mime from
  extension), text branch (``set_pii_key`` before ``decrypt``), and a
  non-fatal LLM failure on the cover letter (must not fail the résumé
  parse).

``src.worker.resume_tasks`` does not exist yet — RED half of the TDD cycle;
this whole file is expected to fail at collection (ImportError).

── Round 2 (merge-blocking re-audit) additions ─────────────────────────────
* Finding 3 (MAJOR): a PERMANENT embedding failure (``LLMOutputInvalidError``
  from ``embedder.embed`` — a dim/count mismatch) must funnel through
  ``record_parse_failure`` -> "failed", exactly like the core-LLM failure
  does, not escape ``parse_resume`` uncaught. Covers both the summary-embed
  call and the chunk-embed call (``_embed_batched``) separately.
* Finding 6 (MEDIUM): the outbox payload must not carry raw chunk TEXT
  (``parsed.chunks[].text`` / ``parsed.cover_letter_chunks[].text``) — header
  chunks contain the candidate's name/email/phone verbatim, so shipping
  chunk text in the unencrypted ``outbox`` jsonb still leaks identity even
  though round 1 dropped the structured ``candidate`` block.

── Round 3 (merge-blocking re-audit) additions ─────────────────────────────
* F1 (HIGH): the TEXT handed to the embedder (chunk batch + summary) must
  never carry the candidate's name/email/phone/location verbatim — a header
  chunk's embedding (``chunk_embs``) and a name-opening LLM summary's
  embedding (``summary_emb``) both ride the (unencrypted) outbox and get
  projected into Neo4j in Phase 4, so they are PII-equivalent even though
  round 2 already dropped raw chunk TEXT from the outbox payload. Covers the
  pure ``_redact_candidate_pii`` helper AND end-to-end embed-call-capture
  assertions, and pins that ``resumes.parsed`` (the system of record) keeps
  the FULL, unredacted chunk text/summary — only the embedder's input is
  scrubbed.
* F2 (MEDIUM): the outbox ``parsed`` dict must not carry the cleartext
  ``summary`` field either (belt-and-braces alongside F1's embedding scrub).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from src.pipeline.llm import LLMOutputInvalidError
from src.pipeline.parsing import MIME_DOCX, EncryptedPdfError, UnsupportedMimeError
from src.schemas import (
    CandidateInfo,
    CoverLetterParsed,
    Experience,
    ResumeChunk,
    ResumeCore,
    ResumeParsed,
    ResumeSkill,
    ResumeSkillDetail,
    ResumeSkillDetails,
)
from src.storage.blob_store import BlobNotFound
from src.worker.resume_tasks import (
    _drop_smeared_years,
    _extract_skills_merged,
    _parse_cover_letter,
    parse_resume,
)

# ── shared helpers ──────────────────────────────────────────────────────


def _flat_call_args(mock_call: Any) -> list[Any]:
    """Positional + keyword args of a single mock call, order-agnostic."""
    return list(mock_call.args) + list(mock_call.kwargs.values())


def _acm(return_value: Any = None) -> MagicMock:
    """A MagicMock usable as ``async with x() as y: ...``."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_conn(fetchrow_result: Any) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _make_ctx(
    conn: MagicMock,
    *,
    llm: MagicMock | None = None,
    embedder: MagicMock | None = None,
    blob_store: MagicMock | None = None,
) -> dict[str, Any]:
    pool = MagicMock(name="pg_pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return {
        "pg_pool": pool,
        "llm": llm if llm is not None else MagicMock(chat_json=AsyncMock()),
        "embedder": embedder if embedder is not None else MagicMock(embed=AsyncMock()),
        "blob_store": (
            blob_store if blob_store is not None else MagicMock(get=AsyncMock())
        ),
    }


def _meta_row(
    *,
    job_id: UUID,
    blob_key: str = "resumes/abc.pdf",
    mime_type: str = "application/pdf",
    status: str = "uploaded",
    cover_letter_blob_key: str | None = None,
    cover_letter_text: bytes | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "blob_key": blob_key,
        "mime_type": mime_type,
        "status": status,
        "cover_letter_blob_key": cover_letter_blob_key,
        "cover_letter_text": cover_letter_text,
    }


def _fake_prompt(version: str = "resume_core_v1") -> MagicMock:
    prompt = MagicMock()
    prompt.messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]
    prompt.version = version
    return prompt


def _make_validation_error() -> ValidationError:
    """A real ``pydantic.ValidationError`` instance, the same exception type
    ``ResumeParsed.model_validate`` raises on malformed input."""

    class _Sentinel(BaseModel):
        x: int

    try:
        _Sentinel.model_validate({"x": "not-an-int"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")  # pragma: no cover


# ── parse_resume: missing / stale ──────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_resume_row_returns_missing() -> None:
    conn = _make_conn(None)
    ctx = _make_ctx(conn)

    result = await parse_resume(ctx, str(uuid4()))

    assert result == "missing"


@pytest.mark.parametrize("status", ["parsed", "failed"])
@pytest.mark.asyncio
async def test_status_not_uploaded_or_parsing_returns_stale_no_io(status: str) -> None:
    conn = _make_conn(_meta_row(job_id=uuid4(), status=status))
    llm = MagicMock(chat_json=AsyncMock())
    blob_store = MagicMock(get=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)

    result = await parse_resume(ctx, str(uuid4()))

    assert result == "stale"
    llm.chat_json.assert_not_called()
    blob_store.get.assert_not_called()


# ── parse_resume: blob / extract failures ──────────────────────────────


@pytest.mark.asyncio
async def test_blob_fetch_failure_records_failure_and_returns_failed() -> None:
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(side_effect=BlobNotFound("gone")))
    llm = MagicMock(chat_json=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)

    with patch(
        "src.worker.resume_tasks.resume_service.record_parse_failure",
        new_callable=AsyncMock,
    ) as record_failure:
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    assert resume_id in _flat_call_args(record_failure.await_args)
    llm.chat_json.assert_not_called()


@pytest.mark.parametrize(
    "exc", [UnsupportedMimeError("bad mime"), EncryptedPdfError("locked")]
)
@pytest.mark.asyncio
async def test_extract_text_failure_records_failure_and_returns_failed(
    exc: Exception,
) -> None:
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    llm = MagicMock(chat_json=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)

    with (
        patch("src.worker.resume_tasks.extract_text", MagicMock(side_effect=exc)),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    llm.chat_json.assert_not_called()


@pytest.mark.asyncio
async def test_zero_chunks_records_failure_before_any_llm_call() -> None:
    """Must fail before spending an LLM pass on unusable input."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    llm = MagicMock(chat_json=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=[])),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    flat = _flat_call_args(record_failure.await_args)
    assert any(
        isinstance(a, str) and "chunk" in a.lower() for a in flat
    ), "record_parse_failure reason should mention 'no chunks produced'"
    llm.chat_json.assert_not_called()


@pytest.mark.asyncio
async def test_core_llm_failure_records_failure_and_returns_failed() -> None:
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMOutputInvalidError("bad json")))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store)
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Experienced engineer.")
    ]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()


# ── Finding 2b (HIGH): a ResumeParsed ValidationError must not escape ──────
#
# ResumeParsed.model_validate(...) in parse_resume today sits in NO
# try/except. An ordinary long CV that chunks into >200 pieces (finding 2a)
# — or any other cause of a malformed payload — trips ValidationError,
# which propagates straight out of parse_resume: record_parse_failure never
# runs, the row is stranded uploaded/parsing with a NULL failure_reason, and
# arq re-runs the whole expensive LLM pipeline on every retry.


@pytest.mark.asyncio
async def test_resume_parsed_validation_error_is_caught_not_raised() -> None:
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock())
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "src.worker.resume_tasks.ResumeParsed.model_validate",
            MagicMock(side_effect=_make_validation_error()),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        # Today this raises ValidationError straight out of parse_resume —
        # that IS the bug. The assertion below is what the fix must satisfy.
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


# ── Finding 3 (MAJOR, round 2): a PERMANENT embedding failure must not ──────
# escape parse_resume uncaught ───────────────────────────────────────────
#
# LLMClient.embed raises LLMOutputInvalidError on a count mismatch AND on
# the expected_dim check — a PERMANENT per-document error, exactly like a
# core-LLM chat_json failure. The embedding calls (summary embed, chunk
# embeds via _embed_batched) are today UNguarded: the exception escapes
# parse_resume uncaught, stranding the row uploaded/parsing with a NULL
# failure_reason. (A transient LLMUnavailableError is NOT asserted here —
# it may still escape so arq retries a genuine outage.)


@pytest.mark.asyncio
async def test_summary_embed_permanent_failure_records_failure_and_returns_failed() -> (
    None
):
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(
        embed=AsyncMock(side_effect=LLMOutputInvalidError("768-d contract violated"))
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        # Today LLMOutputInvalidError escapes uncaught right here — that IS
        # the bug. The assertions below are what the fix must satisfy.
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_chunk_embed_permanent_failure_records_failure_and_returns_failed() -> (
    None
):
    """Same guarantee for the CHUNK embed call (`_embed_batched`), separable
    from the summary embed: the summary embed succeeds, the chunk-batch
    embed raises."""
    resume_id = uuid4()
    conn = _make_conn(_meta_row(job_id=uuid4()))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"), summary="Backend engineer."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(
        embed=AsyncMock(
            side_effect=[[[0.1] * 8], LLMOutputInvalidError("count mismatch")]
        )
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parse_failure",
            new_callable=AsyncMock,
        ) as record_failure,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "failed"
    record_failure.assert_awaited_once()
    record_parsed.assert_not_awaited()
    enqueue.assert_not_awaited()


# ── parse_resume: happy path ────────────────────────────────────────────


@pytest.mark.parametrize("status", ["uploaded", "parsing"])
@pytest.mark.asyncio
async def test_happy_path_no_cover_letter_returns_parsed(status: str) -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status=status, blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace"),
        summary="Backend engineer.",
        experience=[Experience(company="Acme", title="Engineer")],
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer."),
        ResumeChunk(id="c_002", section="skills", page=0, text="Python, SQL"),
    ]
    summary_vec = [0.1] * 8
    chunk_vecs = [[0.2] * 8, [0.3] * 8]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[summary_vec], chunk_vecs]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python", years=5, evidence_chunk_ids=["c_002"])]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=merged_skills,
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ) as encrypt_pii,
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    blob_store.get.assert_awaited_once_with("resumes/abc.pdf")
    assert blob_store.get_object.called is False  # no MinIO-style call site

    encrypt_pii.assert_awaited_once()
    record_parsed.assert_awaited_once()
    flat = _flat_call_args(record_parsed.await_args)
    assert resume_id in flat
    assert any(isinstance(a, ResumeParsed) for a in flat)
    assert (b"enc-name", b"enc-email", b"enc-phone", "hash123") in flat
    assert None in flat  # cover_letter_parsed — no cover letter attached
    assert any(isinstance(a, dt.datetime) for a in flat)

    assert embedder.embed.call_count == 2
    enqueue.assert_awaited_once()
    kwargs = enqueue.await_args.kwargs
    assert kwargs["event_type"] == "resume.parsed"
    assert kwargs["aggregate_id"] == resume_id
    payload = kwargs["payload"]
    assert payload["summary_emb"] == summary_vec
    assert payload["chunk_embs"] == {"c_001": chunk_vecs[0], "c_002": chunk_vecs[1]}
    assert payload["prompt_version"] == "resume_core_v1+resume_skills_v2"
    assert str(payload["job_id"]) == str(job_id)
    assert isinstance(payload["parsed"], dict)


@pytest.mark.asyncio
async def test_race_guard_stale_write_returns_stale_and_enqueues_no_outbox_row() -> (
    None
):
    """``record_parsed`` returning False means the row was no longer
    'uploaded'/'parsing' by the time the UPDATE ran. Must return "stale" and
    MUST NOT enqueue an outbox row for a write that never happened."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(_meta_row(job_id=job_id))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(summary="Backend engineer.")
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(None, None, None, None),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "stale"
    enqueue.assert_not_awaited()


# ── Finding 5 (decision): outbox payload must NOT carry the candidate ─────
#
# Phase 4 needs skills/experience/embeddings, NOT identity — `resumes` is
# the system of record for PII. The outbox payload today ships the full
# ``parsed.model_dump()``, including the cleartext ``candidate`` block
# (name/email/phone), which Phase 4 would otherwise project into Neo4j.


@pytest.mark.asyncio
async def test_outbox_payload_parsed_dict_excludes_candidate_block() -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace", email="ada@example.com"),
        summary="Backend engineer.",
        experience=[Experience(company="Acme", title="Engineer")],
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=merged_skills,
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    enqueue.assert_awaited_once()
    payload = enqueue.await_args.kwargs["payload"]
    parsed_dict = payload["parsed"]

    assert "candidate" not in parsed_dict, (
        "outbox payload leaks the cleartext candidate (name/email/phone) "
        "block — Phase 4 must receive skills/experience/embeddings only"
    )
    # Round 3 F2: `summary` is dropped from the outbox payload (a small model may
    # write the candidate's name into it); Phase 4 reads it from resumes.parsed.
    assert "summary" not in parsed_dict
    assert "skills" in parsed_dict
    assert "chunks" in parsed_dict
    assert payload["summary_emb"] == [0.1] * 8
    assert "chunk_embs" in payload
    assert payload["prompt_version"] == "resume_core_v1+resume_skills_v2"
    assert str(payload["job_id"]) == str(job_id)


# ── Finding 6 (MEDIUM, round 2): outbox must not carry raw chunk TEXT ─────
#
# Round 1 dropped the structured `candidate` block, but the same payload
# still ships `parsed.chunks[].text` / `parsed.cover_letter_chunks[].text` —
# raw résumé text whose header chunks contain the candidate's name/email/
# phone verbatim. So candidate identity is still in the unencrypted
# `outbox` jsonb. The fix drops chunk TEXT from the outbox `parsed` dict
# too — ids-only (or absent) is fine; the payload still needs `chunk_embs`
# keyed by chunk id plus the structured skills/experience/education/summary.
# `resumes.parsed` (the system of record) keeps full chunk text — that's an
# integration concern, not asserted here.


@pytest.mark.asyncio
async def test_outbox_payload_never_carries_raw_chunk_text_pii() -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Ada Lovelace", email="ada@example.com"),
        summary="Backend engineer.",
        experience=[Experience(company="Acme", title="Engineer")],
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(
            id="c_001",
            section="header",
            page=0,
            text="SENTINEL_HEADER_PII Ada Lovelace ada@example.com 555-0100",
        ),
        ResumeChunk(id="c_002", section="skills", page=0, text="Python, SQL"),
    ]
    embedder = MagicMock(
        embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8, [0.3] * 8]])
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=merged_skills,
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    enqueue.assert_awaited_once()
    payload = enqueue.await_args.kwargs["payload"]

    dumped = json.dumps(payload, default=str)
    assert "SENTINEL_HEADER_PII" not in dumped, (
        "raw chunk text (candidate PII carried by header chunks) leaked "
        "into the unencrypted outbox payload"
    )

    parsed_dict = payload["parsed"]
    for key in ("chunks", "cover_letter_chunks"):
        if key in parsed_dict:
            for row in parsed_dict[key]:
                assert "text" not in row, (
                    f"outbox parsed.{key}[] still carries chunk TEXT — "
                    "ids-only is fine, raw text is not"
                )

    assert "chunk_embs" in payload
    assert set(payload["chunk_embs"]) == {"c_001", "c_002"}
    assert payload["summary_emb"] == [0.1] * 8
    assert payload["prompt_version"] == "resume_core_v1+resume_skills_v2"
    assert str(payload["job_id"]) == str(job_id)
    assert "skills" in parsed_dict
    assert "experience" in parsed_dict
    assert "education" in parsed_dict
    # Round 3 F2: `summary` is excluded from the outbox payload.
    assert "summary" not in parsed_dict


# ── _drop_smeared_years ──────────────────────────────────────────────────


def _skills_with_years(
    count: int, years: int | None, *, prefix: str = "skill"
) -> list[ResumeSkill]:
    return [ResumeSkill(name=f"{prefix}_{i}", years=years) for i in range(count)]


def test_drop_smeared_years_resets_at_exactly_6_sharing_same_value() -> None:
    """The boundary that TRIGGERS — the smeared-years failure mode."""
    skills = _skills_with_years(6, 12)

    result = _drop_smeared_years(skills, "res-1")

    assert all(s.years is None for s in result)


def test_drop_smeared_years_leaves_5_sharing_same_value_untouched() -> None:
    """The boundary that does NOT trigger."""
    skills = _skills_with_years(5, 12)

    result = _drop_smeared_years(skills, "res-1")

    assert all(s.years == 12 for s in result)


def test_drop_smeared_years_leaves_differing_values_untouched() -> None:
    skills = [ResumeSkill(name=f"skill_{i}", years=i + 1) for i in range(6)]

    result = _drop_smeared_years(skills, "res-1")

    assert [s.years for s in result] == [1, 2, 3, 4, 5, 6]


def test_drop_smeared_years_only_resets_the_smeared_group() -> None:
    smeared = _skills_with_years(6, 12, prefix="smeared")
    unrelated = ResumeSkill(name="unique_skill", years=3)

    result = _drop_smeared_years([*smeared, unrelated], "res-1")

    smeared_result = [s for s in result if s.name.startswith("smeared")]
    unrelated_result = next(s for s in result if s.name == "unique_skill")
    assert all(s.years is None for s in smeared_result)
    assert unrelated_result.years == 3


# ── _extract_skills_merged ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_extract_skills_merged_combines_scan_and_llm_results() -> None:
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="SQL", years=5),
                    ResumeSkillDetail(name="Kubernetes", years=2),
                ]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="Python, SQL")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=["Python", "SQL"]),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged = await _extract_skills_merged(llm, chunks, "res-1")

    names = {s.name for s in merged}
    assert names == {"Python", "SQL", "Kubernetes"}
    sql_skill = next(s for s in merged if s.name == "SQL")
    assert sql_skill.years == 5  # LLM value fills in the scan-only match
    python_skill = next(s for s in merged if s.name == "Python")
    assert python_skill.years is None  # scan-only, no LLM mention


@pytest.mark.asyncio
async def test_extract_skills_merged_llm_failure_is_non_fatal() -> None:
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMOutputInvalidError("bad json")))
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="Python, SQL")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=["Python", "SQL"]),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged = await _extract_skills_merged(llm, chunks, "res-1")

    assert {s.name for s in merged} == {"Python", "SQL"}


@pytest.mark.asyncio
async def test_extract_skills_merged_first_non_null_years_wins_on_collision() -> None:
    llm = MagicMock(
        chat_json=AsyncMock(
            return_value=ResumeSkillDetails(
                skills=[
                    ResumeSkillDetail(name="SQL", years=5),
                    ResumeSkillDetail(name="SQL", years=10),
                ]
            )
        )
    )
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="SQL expert")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text", MagicMock(return_value=[])
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged = await _extract_skills_merged(llm, chunks, "res-1")

    assert len(merged) == 1
    assert merged[0].years == 5


@pytest.mark.asyncio
async def test_extract_skills_merged_is_capped_at_80() -> None:
    scan_names = [f"skill_{i:03d}" for i in range(100)]
    llm = MagicMock(chat_json=AsyncMock(return_value=ResumeSkillDetails(skills=[])))
    chunks = [ResumeChunk(id="c_001", section="skills", page=0, text="lots of skills")]

    with (
        patch(
            "src.worker.resume_tasks.match_skills_in_text",
            MagicMock(return_value=scan_names),
        ),
        patch(
            "src.worker.resume_tasks.canonicalize_skill_names",
            MagicMock(side_effect=lambda names: list(names)),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("resume_skills_v2"),
        ),
    ):
        merged = await _extract_skills_merged(llm, chunks, "res-1")

    assert len(merged) == 80


# ── _parse_cover_letter ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_cover_letter_absent_returns_no_chunks_no_extraction() -> None:
    conn = MagicMock(name="conn")
    llm = MagicMock(chat_json=AsyncMock())
    blob_store = MagicMock(get=AsyncMock())
    meta = _meta_row(job_id=uuid4())  # no cover letter fields set

    chunks, parsed = await _parse_cover_letter(conn, llm, blob_store, meta, uuid4())

    assert chunks == []
    assert parsed is None
    blob_store.get.assert_not_called()
    llm.chat_json.assert_not_called()


@pytest.mark.asyncio
async def test_parse_cover_letter_blob_branch_fetches_via_blob_store_get() -> None:
    conn = MagicMock(name="conn")
    cl_parsed = CoverLetterParsed(raw_text="Dear hiring manager...")
    llm = MagicMock(chat_json=AsyncMock(return_value=cl_parsed))
    blob_store = MagicMock(get=AsyncMock(return_value=b"docx-bytes"))
    meta = _meta_row(job_id=uuid4(), cover_letter_blob_key="resumes/abc/cover.docx")
    chunks_sentinel = [ResumeChunk(id="cl_001", section="other", page=0, text="Dear x")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ) as extract_text,
        patch(
            "src.worker.resume_tasks.chunk_resume",
            MagicMock(return_value=chunks_sentinel),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("cover_letter_v1"),
        ),
    ):
        chunks, parsed = await _parse_cover_letter(conn, llm, blob_store, meta, uuid4())

    blob_store.get.assert_awaited_once_with("resumes/abc/cover.docx")
    assert blob_store.get_object.called is False  # no MinIO-style call site
    extract_text.assert_called_once()
    assert MIME_DOCX in _flat_call_args(extract_text.call_args)
    assert chunks == chunks_sentinel
    assert parsed == cl_parsed


@pytest.mark.asyncio
async def test_parse_cover_letter_text_branch_decrypts_after_set_pii_key() -> None:
    """SET LOCAL app.pii_key is transaction-scoped — pgp_sym_decrypt fails
    with an unrecognized-configuration-parameter error unless set_pii_key
    ran first, in the same transaction."""
    conn = MagicMock(name="conn")
    llm = MagicMock(chat_json=AsyncMock(return_value=CoverLetterParsed()))
    blob_store = MagicMock(get=AsyncMock())
    meta = _meta_row(job_id=uuid4(), cover_letter_text=b"pgp-ciphertext-bytes")

    manager = MagicMock()
    set_pii_key = AsyncMock()
    decrypt = AsyncMock(return_value="Dear hiring manager, plaintext cover letter.")
    manager.attach_mock(set_pii_key, "set_pii_key")
    manager.attach_mock(decrypt, "decrypt")

    with (
        patch("src.worker.resume_tasks.pii.set_pii_key", set_pii_key),
        patch("src.worker.resume_tasks.pii.decrypt", decrypt),
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch(
            "src.worker.resume_tasks.chunk_resume",
            MagicMock(
                return_value=[
                    ResumeChunk(id="cl_001", section="other", page=0, text="x")
                ]
            ),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("cover_letter_v1"),
        ),
    ):
        await _parse_cover_letter(conn, llm, blob_store, meta, uuid4())

    blob_store.get.assert_not_called()
    set_pii_key.assert_awaited_once()
    decrypt.assert_awaited_once()
    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("set_pii_key") < call_names.index(
        "decrypt"
    ), "set_pii_key must be called BEFORE decrypt (transaction-scoped GUC)"


@pytest.mark.asyncio
async def test_parse_cover_letter_llm_failure_is_non_fatal() -> None:
    """A résumé parse must not fail because its cover letter's LLM call
    failed — the chunks are preserved (for evidence citation) but the
    extraction comes back as an EMPTY ``CoverLetterParsed()``, never
    ``None`` and never a propagated exception."""
    conn = MagicMock(name="conn")
    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMOutputInvalidError("bad json")))
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    meta = _meta_row(job_id=uuid4(), cover_letter_blob_key="resumes/abc/cover.pdf")
    chunks_sentinel = [ResumeChunk(id="cl_001", section="other", page=0, text="Dear x")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch(
            "src.worker.resume_tasks.chunk_resume",
            MagicMock(return_value=chunks_sentinel),
        ),
        patch(
            "src.worker.resume_tasks.load_prompt",
            return_value=_fake_prompt("cover_letter_v1"),
        ),
    ):
        chunks, parsed = await _parse_cover_letter(conn, llm, blob_store, meta, uuid4())

    assert chunks == chunks_sentinel
    assert parsed is not None
    assert parsed == CoverLetterParsed()


# ── F1 (HIGH, round 3) — PII-equivalent EMBEDDINGS ride the outbox ─────────
#
# `chunk_embs`/`summary_emb` are nomic-embed-text vectors of the TEXT handed
# to the embedder. A header chunk's text (and sometimes the LLM-authored
# summary) carries the candidate's name/email/phone/location VERBATIM, so
# those vectors are PII-equivalent even though round 2 already dropped raw
# chunk TEXT from the outbox `parsed` dict (finding 6) — the embedding is a
# separate top-level payload key that still encodes identity. The fix is a
# pure `_redact_candidate_pii(text, candidate) -> str` helper applied to every
# chunk's text and the composed summary text BEFORE either is embedded.
# `resumes.parsed` (system of record, ADR-007 §6) keeps the FULL, unredacted
# chunk text and summary at rest — only the embedder's INPUT is scrubbed.
#
# `_redact_candidate_pii` does not exist yet. Each pure-function test below
# imports it LOCALLY (inside the helper, called from each test body) so a
# missing implementation errors only these new tests — not the ~30
# already-green tests earlier in this file that import real,
# already-implemented symbols from `src.worker.resume_tasks` at module scope.


def _redact(text: str, candidate: CandidateInfo) -> str:
    from src.worker.resume_tasks import _redact_candidate_pii

    result: str = _redact_candidate_pii(text, candidate)
    return result


def test_redact_candidate_pii_removes_name_email_phone_location_verbatim() -> None:
    candidate = CandidateInfo(
        name="Ada Lovelace",
        email="ada@example.com",
        phone="555-0100",
        location="Toronto, ON",
    )
    text = "Ada Lovelace | ada@example.com | 555-0100 | Toronto, ON | Senior Engineer"

    result = _redact(text, candidate)

    assert "ada lovelace" not in result.lower()
    assert "ada@example.com" not in result.lower()
    assert "555-0100" not in result.lower()
    assert "toronto, on" not in result.lower()
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_is_case_insensitive() -> None:
    """A small model may title-case the summary differently than the résumé
    body (e.g. ALL CAPS in a header vs. Title Case from the LLM)."""
    candidate = CandidateInfo(name="Ada Lovelace", email="ADA@EXAMPLE.COM")
    text = "ADA LOVELACE is a senior backend engineer. Contact: ada@example.com"

    result = _redact(text, candidate)

    assert "ada lovelace" not in result.lower()
    assert "ada@example.com" not in result.lower()
    assert "senior backend engineer" in result.lower()


def test_redact_candidate_pii_none_fields_are_noop_for_that_field() -> None:
    """Only email is set — phone/location/name are None and must not crash or
    get matched against anything (there's nothing to match)."""
    candidate = CandidateInfo(email="ada@example.com")
    text = "Ada Lovelace, Python developer, ada@example.com, 555-0100"

    result = _redact(text, candidate)

    assert "ada@example.com" not in result.lower()
    assert "ada lovelace" in result.lower()  # name is None: untouched
    assert "555-0100" in result  # phone is None: untouched


def test_redact_candidate_pii_all_fields_none_returns_text_verbatim() -> None:
    candidate = CandidateInfo()
    text = "Python developer with 5 years of backend experience."

    result = _redact(text, candidate)

    assert result == text


def test_redact_candidate_pii_matches_full_identifier_not_per_token() -> None:
    """`candidate.name="Jane Doe"` must redact the literal "Jane Doe" SUBSTRING
    only — not decompose into per-token global removal, which would also eat
    an unrelated "Doe" elsewhere in the résumé (e.g. a reference's surname)."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane Doe. References: John Doe, former manager at Acme Corp."

    result = _redact(text, candidate)

    assert "jane doe" not in result.lower()
    assert "john doe" in result.lower(), (
        "per-token redaction of 'Doe' corrupted unrelated content that is "
        "not the candidate's own name"
    )


def test_redact_candidate_pii_collapses_resulting_whitespace() -> None:
    candidate = CandidateInfo(name="Ada Lovelace")
    text = "Ada Lovelace   Senior Engineer"

    result = _redact(text, candidate)

    assert "  " not in result  # no doubled/tripled space left by the removal
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_does_not_mutate_the_candidate_argument() -> None:
    candidate = CandidateInfo(name="Ada Lovelace")
    before = candidate.model_dump()
    _redact("Ada Lovelace, backend engineer.", candidate)

    assert candidate.model_dump() == before


# ── F1-R (MEDIUM, round 4) — whitespace-flexible identifier scrub closes ──
# format-divergent leaks ─────────────────────────────────────────────────
#
# `_redact_candidate_pii` today matches each `candidate.*` value as a single,
# LITERAL `re.escape`'d substring against the un-normalized résumé body. The
# candidate values are the LLM's NORMALIZED output — a name split across a
# PDF line break, a phone number reflowed with extra whitespace, or an email
# whose local-part appears alone in the body all diverge from the literal
# structured value and leak into the text handed to the embedder (security
# round-4 finding F1-R). The fix: for each identifier, split on `\s+` into
# tokens, `re.escape` each token, and join with `r"\s+"` so any run of
# whitespace (space/tab/newline, any count) between tokens still matches.
# Single-token identifiers (and the email as a whole) degrade to the current
# literal match — no regression. The email LOCAL-PART is additionally
# scrubbed as its own whitespace-flexible pattern (the domain is not PII).


def test_redact_candidate_pii_scrubs_newline_split_name() -> None:
    """A two-column PDF, or a name stacked above a title on the next line,
    can insert a NEWLINE where the LLM's normalized `candidate.name` has a
    plain space. The literal `re.escape` match in the current code requires
    an EXACT single space and misses this — "Jane" and "Doe" both survive,
    adjacent, in the text handed to the embedder."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane\nDoe Senior Engineer"

    result = _redact(text, candidate)

    assert not re.search(
        r"jane\s*doe", result, re.IGNORECASE
    ), f"candidate name survived a newline-split format divergence: {result!r}"
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_scrubs_double_space_split_name() -> None:
    """Same format-divergence leak as the newline case, but a doubled space
    (a common PDF-extraction artifact from column gaps)."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane  Doe Senior Engineer"

    result = _redact(text, candidate)

    assert not re.search(
        r"jane\s*doe", result, re.IGNORECASE
    ), f"candidate name survived a double-space format divergence: {result!r}"
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_scrubs_tab_split_name() -> None:
    """Same format-divergence leak, this time a tab (some PDF extractors
    render column gaps as literal tab characters)."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane\tDoe Senior Engineer"

    result = _redact(text, candidate)

    assert not re.search(
        r"jane\s*doe", result, re.IGNORECASE
    ), f"candidate name survived a tab-split format divergence: {result!r}"
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_scrubs_space_divergent_phone_digits() -> None:
    """PDF text extraction can reflow a phone number's whitespace (an extra
    space, or a line break inserted mid-number by a two-column layout) while
    the LLM normalizes the SAME digit groups with single spaces. The
    whitespace-flexible fix closes this; the current literal `re.escape`
    match requires an EXACT single space between "604", "555" and "1212"
    and leaves the digits in the embedded text.

    NOTE (round4-fix-spec.md): the fix is deliberately bounded to WHITESPACE
    divergence only — it does not strip or normalize punctuation (parens/
    dashes) that differs between the body and the LLM-normalized value. This
    body diverges from `candidate.phone` ONLY in whitespace (an extra space
    plus a line break), never in punctuation, which is exactly the
    "space-divergent phone (two-column-PDF)" case the spec fix commits to
    closing — see round4-fix-spec.md for the accepted punctuation-divergence
    residual.
    """
    candidate = CandidateInfo(phone="+1 604 555 1212")
    text = "Contact:\n+1  604 555\n1212\nSenior Engineer"

    result = _redact(text, candidate)

    assert "604" not in result
    assert "555" not in result
    assert "1212" not in result
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_scrubs_email_local_part_alone() -> None:
    """A résumé header sometimes shows just the local-part of an email (a
    truncated or column-wrapped rendering) while `candidate.email` carries
    the LLM-normalized full address. The current code only matches the FULL
    `name@domain` literal, so a bare local-part in the body is never
    touched. The domain is not PII and is not required to be scrubbed."""
    candidate = CandidateInfo(email="jane.doe@example.com")
    text = "jane.doe Senior Engineer"

    result = _redact(text, candidate)

    assert "jane.doe" not in result.lower()
    assert "senior engineer" in result.lower()


def test_redact_candidate_pii_whitespace_flexible_name_keeps_precision() -> None:
    """Over-redaction guard: the whitespace-flexible pattern must still
    require the candidate's OWN tokens, in the candidate's OWN order,
    separated by nothing but whitespace. An unrelated person who shares only
    ONE token with the candidate's name — a different first name paired
    with the candidate's surname, or vice versa — must be preserved
    verbatim; the fix must not start connecting unrelated tokens across
    word boundaries just because it tolerates whitespace runs."""
    candidate = CandidateInfo(name="Jane Doe")
    text = "Jane Smith is the referring recruiter. Contact John Doe for references."

    result = _redact(text, candidate)

    assert "jane smith" in result.lower(), (
        "an unrelated person's name sharing the candidate's first name was "
        f"corrupted by the whitespace-flexible scrub: {result!r}"
    )
    assert "john doe" in result.lower(), (
        "an unrelated person's name sharing the candidate's surname was "
        f"corrupted by the whitespace-flexible scrub: {result!r}"
    )


# ── F1 (HIGH, round 3) — end-to-end: embed-call TEXT is scrubbed, the ─────
# STORED ResumeParsed stays full/unredacted ─────────────────────────────────


@pytest.mark.asyncio
async def test_header_chunk_pii_redacted_in_embeds_full_text_stored() -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    candidate = CandidateInfo(
        name="Ada Lovelace",
        email="ada.lovelace@example.com",
        phone="555-0100",
        location="Toronto, ON",
    )
    core = ResumeCore(
        candidate=candidate, summary="Backend engineer with 10 years experience."
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    header_text = "Ada Lovelace | ada.lovelace@example.com | 555-0100 | Toronto, ON"
    chunks = [
        ResumeChunk(id="c_001", section="header", page=0, text=header_text),
        ResumeChunk(
            id="c_002", section="skills", page=0, text="Python, SQL, Kubernetes"
        ),
    ]
    embedder = MagicMock(
        embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8, [0.3] * 8]])
    )
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=merged_skills,
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ),
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    assert embedder.embed.await_count == 2
    summary_call_texts = _flat_call_args(embedder.embed.await_args_list[0])[0]
    chunk_call_texts = _flat_call_args(embedder.embed.await_args_list[1])[0]

    pii_substrings = [
        "Ada Lovelace",
        "ada.lovelace@example.com",
        "555-0100",
        "Toronto, ON",
    ]
    for embedded_text in [*summary_call_texts, *chunk_call_texts]:
        for pii in pii_substrings:
            assert pii.lower() not in embedded_text.lower(), (
                f"embedded text still carries candidate PII substring "
                f"{pii!r}: {embedded_text!r}"
            )

    # Non-header chunks still get embedded — redaction is not "embed nothing".
    assert len(chunk_call_texts) == 2
    assert "python" in chunk_call_texts[1].lower()
    assert "kubernetes" in chunk_call_texts[1].lower()

    # The STORED ResumeParsed (system of record, ADR-007 §6) keeps the FULL,
    # unredacted chunk text — only the embedder's input was scrubbed.
    record_parsed.assert_awaited_once()
    flat = _flat_call_args(record_parsed.await_args)
    stored_parsed = next(a for a in flat if isinstance(a, ResumeParsed))
    assert stored_parsed.chunks[0].text == header_text


@pytest.mark.asyncio
async def test_summary_with_candidate_name_redacted_in_embed_only() -> None:
    """Small models sometimes open the `summary` field with the candidate's
    own name ("Jane Doe is a senior engineer..."). The embedded summary text
    must not carry it; the STORED `ResumeParsed.summary` must stay full."""
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    candidate = CandidateInfo(name="Jane Doe", email="jane.doe@example.com")
    summary_text = "Jane Doe is a senior backend engineer with deep Python experience."
    core = ResumeCore(candidate=candidate, summary=summary_text)
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ) as record_parsed,
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ),
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    summary_call_texts = _flat_call_args(embedder.embed.await_args_list[0])[0]
    assert len(summary_call_texts) == 1
    embedded_summary = summary_call_texts[0]
    assert "jane doe" not in embedded_summary.lower()
    assert "jane.doe@example.com" not in embedded_summary.lower()
    assert "senior backend engineer" in embedded_summary.lower()

    record_parsed.assert_awaited_once()
    flat = _flat_call_args(record_parsed.await_args)
    stored_parsed = next(a for a in flat if isinstance(a, ResumeParsed))
    assert stored_parsed.summary == summary_text


# ── F2 (MEDIUM, round 3) — outbox payload must not carry cleartext `summary`
#
# Even with the embedding scrubbed (F1), `outbox.parsed.summary` is still
# cleartext and can carry the candidate's name. Phase 4 reads `summary` from
# `resumes.parsed` (the system of record) — the outbox does not need it.


@pytest.mark.asyncio
async def test_outbox_payload_parsed_dict_excludes_summary_key() -> None:
    resume_id = uuid4()
    job_id = uuid4()
    conn = _make_conn(
        _meta_row(job_id=job_id, status="uploaded", blob_key="resumes/abc.pdf")
    )
    blob_store = MagicMock(get=AsyncMock(return_value=b"pdf-bytes"))
    core = ResumeCore(
        candidate=CandidateInfo(name="Jane Doe"),
        summary="Jane Doe is a senior backend engineer.",
        experience=[Experience(company="Acme", title="Engineer")],
    )
    llm = MagicMock(chat_json=AsyncMock(return_value=core))
    chunks = [
        ResumeChunk(id="c_001", section="summary", page=0, text="Backend engineer.")
    ]
    embedder = MagicMock(embed=AsyncMock(side_effect=[[[0.1] * 8], [[0.2] * 8]]))
    ctx = _make_ctx(conn, llm=llm, blob_store=blob_store, embedder=embedder)
    merged_skills = [ResumeSkill(name="Python")]

    with (
        patch(
            "src.worker.resume_tasks.extract_text", MagicMock(return_value=MagicMock())
        ),
        patch("src.worker.resume_tasks.chunk_resume", MagicMock(return_value=chunks)),
        patch(
            "src.worker.resume_tasks._extract_skills_merged",
            new_callable=AsyncMock,
            return_value=merged_skills,
        ),
        patch(
            "src.worker.resume_tasks.resume_service.encrypt_pii_via_session",
            new_callable=AsyncMock,
            return_value=(b"enc-name", b"enc-email", b"enc-phone", "hash123"),
        ),
        patch(
            "src.worker.resume_tasks.resume_service.record_parsed",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "src.worker.resume_tasks.outbox_service.enqueue_outbox",
            new_callable=AsyncMock,
        ) as enqueue,
    ):
        result = await parse_resume(ctx, str(resume_id))

    assert result == "parsed"
    enqueue.assert_awaited_once()
    parsed_dict = enqueue.await_args.kwargs["payload"]["parsed"]

    assert "summary" not in parsed_dict, (
        "outbox payload leaks the cleartext `summary` — Phase 4 reads it "
        "from resumes.parsed (system of record), not the outbox"
    )
    assert "skills" in parsed_dict
    assert "experience" in parsed_dict
