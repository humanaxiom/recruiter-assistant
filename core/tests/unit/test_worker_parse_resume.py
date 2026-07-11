"""Unit tests for ``src.worker.resume_tasks`` — all I/O mocked.

Same real-ctx-keys discipline as ``test_worker_parse_job.py``: ``ctx`` is
built from ``pg_pool`` / ``llm`` / ``embedder`` / ``blob_store`` (the keys
``src/worker/main.py::startup`` actually sets), never hris's
``ctx["minio"]``/``ctx["minio_bucket_resumes_raw"]``.

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
"""

from __future__ import annotations

import datetime as dt
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
    assert parsed_dict["summary"] == "Backend engineer."
    assert "skills" in parsed_dict
    assert "chunks" in parsed_dict
    assert payload["summary_emb"] == [0.1] * 8
    assert "chunk_embs" in payload
    assert payload["prompt_version"] == "resume_core_v1+resume_skills_v2"
    assert str(payload["job_id"]) == str(job_id)


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
