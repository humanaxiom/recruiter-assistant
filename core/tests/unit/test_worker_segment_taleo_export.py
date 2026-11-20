"""Slice C1 (RED) -- ``src.worker.split_tasks``: the arq task that segments a
combined Taleo export and the hourly expiry cron. The module does not exist
yet, so this file fails at import.

API pinned here::

    _REQUIRED_CTX_KEYS = ("pg_pool", "blob_store", "llm")   # read off ctx
    _QUEUE_CTX_KEY = "arq"                                  # reaper enqueues here

    async def segment_taleo_export(
        ctx, split_id: str, heuristic_only: bool = False
    ) -> str          # "proposed" | "failed" | "skipped"
    async def expire_split_requests(ctx) -> str   # never raises; "unavailable"
                                                  # when pg_pool/arq missing

``split_tasks`` imports ``split_request_service``, ``taleo_split_service``,
``get_settings`` and ``asyncio`` BY THOSE NAMES (tests patch them there) and
calls ``split_request_service.claim / store_proposal / fail / requeue_stale /
expire / sweep_orphan_blobs`` (signatures in test_split_request_service.py).

Pipeline for one task: claim (queued->segmenting; None => "skipped", no work)
-> blob read -> extract_page_texts (asyncio.to_thread) -> caps/readability
checks (failure => ``fail`` with a static ``FAILURE_REASONS`` string) -> LLM
path ``llm_segment`` -> ``repair_orphan_pages`` -> ``validate_proposal`` ->
``store_proposal(method="llm", fallback_reason=None)``; any LLM exception ->
``heuristic_proposal`` with ``method="heuristic"`` and a fixed
``fallback_reason`` (LLMUnavailableError -> "llm_unavailable",
LLMOutputInvalidError -> "llm_invalid_output", anything else -> "llm_error";
``heuristic_only=True`` -> "requeued_after_stall" and no LLM call at all).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import fitz  # type: ignore[import-untyped]
import pytest

from src.pipeline.llm import LLMOutputInvalidError, LLMUnavailableError
from src.schemas.taleo_split import (
    ProposalApplicant,
    ProposalDiagnostics,
    SplitProposal,
)
from src.services import split_request_service as srs
from src.services import taleo_split_service as svc
from src.settings import Settings
from src.storage.blob_store import BlobNotFound
from src.worker import split_tasks

_PAD = " ".join(["Experience in recruiting and operations."] * 8)
_SECRET = "SECRET-Pat-Example-pat@leak.example"


def _resume(email: str) -> str:
    return f"Applicant\n{email}\n604-555-0100\nRESUME\n{_PAD}"


def _cover(email: str) -> str:
    return f"{email}\nDear Hiring Manager,\nI am writing to apply.\n{_PAD}\nSincerely,"


def _pdf(pages: list[str]) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        for i, line in enumerate(text.splitlines()):
            page.insert_text((72, 72 + i * 14), line, fontsize=9)
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _encrypted_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "secret", fontsize=9)
    data: bytes = doc.tobytes(
        encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o"
    )
    doc.close()
    return data


def _three_page_export() -> bytes:
    return _pdf(
        [_resume("a@example.com"), _cover("a@example.com"), _resume("b@example.com")]
    )


class _FakeLLM:
    def __init__(self, result: object | None = None, exc: Exception | None = None):
        self._result = result
        self._exc = exc
        self.chat_json = AsyncMock(side_effect=self._call)

    async def _call(self, *_a: Any, **_k: Any) -> object:
        if self._exc is not None:
            raise self._exc
        return self._result


def _down_llm() -> _FakeLLM:
    return _FakeLLM(exc=RuntimeError("down"))


def _claim(*, blind: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        job_id=uuid4(),
        source_blob_key="split_sources/x.pdf",
        blind_review=blind,
    )


def _ctx(
    *, pdf: bytes | BaseException, llm: _FakeLLM | None = None
) -> tuple[dict[str, Any], MagicMock, MagicMock]:
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="UPDATE 1")
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock(name="pool")
    pool.acquire = MagicMock(return_value=acq)
    blob = MagicMock(name="blob_store")
    if isinstance(pdf, BaseException):
        blob.get = AsyncMock(side_effect=pdf)
    else:
        blob.get = AsyncMock(return_value=pdf)
    blob.delete = AsyncMock()
    arq = MagicMock(name="arq")
    arq.enqueue_job = AsyncMock()
    ctx: dict[str, Any] = {
        "pg_pool": pool,
        "blob_store": blob,
        "llm": llm if llm is not None else _FakeLLM(result=SplitProposal()),
        "arq": arq,
    }
    return ctx, conn, blob


@contextmanager
def _svc(
    claim: SimpleNamespace | None, *, stored: bool = True
) -> Iterator[SimpleNamespace]:
    mocks = {
        "claim": AsyncMock(return_value=claim),
        "store_proposal": AsyncMock(return_value=stored),
        "fail": AsyncMock(return_value=True),
        "requeue_stale": AsyncMock(return_value=[]),
        "expire": AsyncMock(return_value=0),
        "sweep_orphan_blobs": AsyncMock(return_value=0),
    }
    patches = [patch.object(srs, name, m) for name, m in mocks.items()]
    for p in patches:
        p.start()
    try:
        yield SimpleNamespace(**mocks)
    finally:
        for p in patches:
            p.stop()


def _sid() -> str:
    return str(uuid4())


def _stored(mocks: SimpleNamespace) -> tuple[SplitProposal, dict[str, Any]]:
    call = mocks.store_proposal.await_args
    return call.args[2], call.kwargs


# -- ctx / registration pins (the A7 lesson) ---------------------------------


@pytest.mark.asyncio
async def test_required_ctx_keys_are_exactly_what_startup_really_sets(
    tmp_path: Path,
) -> None:
    from src.worker.main import startup

    ctx: dict[str, Any] = {}
    settings = Settings(
        storage_dir=str(tmp_path), pii_key="k-secret", skill_hash_salt="salt"
    )
    with (
        patch("src.worker.main.get_settings", return_value=settings),
        patch(
            "src.worker.main.asyncpg.create_pool", AsyncMock(return_value=MagicMock())
        ),
        patch("src.worker.main.init_schema", AsyncMock()),
        patch(
            "src.worker.main.AsyncGraphDatabase.driver",
            return_value=MagicMock(close=AsyncMock()),
        ),
        patch("src.worker.main.bootstrap_neo4j_schema", AsyncMock()),
        patch("src.worker.main.create_pool", AsyncMock()),
    ):
        await startup(ctx)
    assert split_tasks._REQUIRED_CTX_KEYS == ("pg_pool", "blob_store", "llm")
    for key in split_tasks._REQUIRED_CTX_KEYS:
        assert key in ctx, f"startup never sets ctx[{key!r}]"
    assert split_tasks._QUEUE_CTX_KEY == "arq"
    assert "arq" in ctx


def test_segment_task_is_registered_in_worker_functions() -> None:
    from src.worker.main import WorkerSettings

    assert split_tasks.segment_taleo_export in WorkerSettings.functions


def test_expiry_is_an_hourly_cron_and_not_an_enqueue_by_name_task() -> None:
    from src.worker.main import WorkerSettings

    jobs = [
        j
        for j in WorkerSettings.cron_jobs
        if j.coroutine is split_tasks.expire_split_requests
    ]
    assert len(jobs) == 1
    assert jobs[0].hour is None  # every hour
    assert jobs[0].minute is not None  # pinned to one minute, not every minute
    assert split_tasks.expire_split_requests not in WorkerSettings.functions


# -- LLM happy path ----------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_path_stores_a_proposed_llm_method_row() -> None:
    llm = _FakeLLM(
        result=SplitProposal(
            applicants=[
                ProposalApplicant(resume_pages=[1], cover_letter_pages=[2]),
                ProposalApplicant(resume_pages=[3]),
            ]
        )
    )
    ctx, _conn, _blob = _ctx(pdf=_three_page_export(), llm=llm)
    claim = _claim()
    with _svc(claim) as m:
        out = await split_tasks.segment_taleo_export(ctx, _sid())
    assert out == "proposed"
    m.store_proposal.assert_awaited_once()
    proposal, kw = _stored(m)
    assert kw["method"] == "llm"
    assert kw["fallback_reason"] is None
    assert kw["page_count"] == 3
    assert isinstance(kw["diagnostics"], ProposalDiagnostics)
    assert kw["diagnostics"].blocking is False
    assert [a.resume_pages for a in proposal.applicants] == [[1], [3]]
    m.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_path_repairs_orphan_pages_before_storing() -> None:
    pdf = _pdf([_resume("a@example.com"), _resume("a@example.com"), _resume("b@x.com")])
    llm = _FakeLLM(
        result=SplitProposal(
            applicants=[
                ProposalApplicant(resume_pages=[1]),
                ProposalApplicant(resume_pages=[3]),
            ]
        )
    )
    ctx, _c, _b = _ctx(pdf=pdf, llm=llm)
    with _svc(_claim()) as m:
        await split_tasks.segment_taleo_export(ctx, _sid())
    proposal, kw = _stored(m)
    assert proposal.applicants[0].resume_pages == [1, 2]
    assert kw["diagnostics"].missing == []


@pytest.mark.asyncio
async def test_llm_path_cannot_hide_an_applicant_in_ignored_pages() -> None:
    llm = _FakeLLM(
        result=SplitProposal(
            applicants=[ProposalApplicant(resume_pages=[1], cover_letter_pages=[2])],
            ignored_pages=[3],
        )
    )
    ctx, _c, _b = _ctx(pdf=_three_page_export(), llm=llm)
    with _svc(_claim()) as m:
        await split_tasks.segment_taleo_export(ctx, _sid())
    proposal, kw = _stored(m)
    assert proposal.ignored_pages == []
    assert kw["diagnostics"].missing == [3]
    assert kw["diagnostics"].blocking is True


@pytest.mark.asyncio
async def test_blind_review_flag_comes_from_the_claim() -> None:
    ctx, _c, _b = _ctx(pdf=_three_page_export())
    with _svc(_claim(blind=True)) as m:
        await split_tasks.segment_taleo_export(ctx, _sid())
    assert _stored(m)[1]["blind_review"] is True


@pytest.mark.asyncio
async def test_non_blind_claim_passes_blind_review_false() -> None:
    ctx, _c, _b = _ctx(pdf=_three_page_export())
    with _svc(_claim(blind=False)) as m:
        await split_tasks.segment_taleo_export(ctx, _sid())
    assert _stored(m)[1]["blind_review"] is False


# -- LLM failure -> heuristic fallback ---------------------------------------


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (LLMUnavailableError(_SECRET), "llm_unavailable"),
        (LLMOutputInvalidError(_SECRET), "llm_invalid_output"),
        (RuntimeError(_SECRET), "llm_error"),
        (ValueError(_SECRET), "llm_error"),
        (TimeoutError(_SECRET), "llm_error"),
    ],
)
@pytest.mark.asyncio
async def test_any_llm_exception_falls_back_to_the_heuristic(
    exc: Exception, reason: str
) -> None:
    texts = svc.extract_page_texts(_three_page_export())
    ctx, _c, _b = _ctx(pdf=_three_page_export(), llm=_FakeLLM(exc=exc))
    with _svc(_claim()) as m:
        out = await split_tasks.segment_taleo_export(ctx, _sid())
    assert out == "proposed"
    proposal, kw = _stored(m)
    assert kw["method"] == "heuristic"
    assert kw["fallback_reason"] == reason
    assert reason in srs.FALLBACK_REASONS
    assert proposal == svc.heuristic_proposal(texts)
    # the model's/exception text is never persisted or logged-through to the row
    assert _SECRET not in repr(m.store_proposal.await_args)


@pytest.mark.asyncio
async def test_heuristic_fallback_keeps_cover_pages_out_of_the_resume() -> None:
    ctx, _c, _b = _ctx(pdf=_three_page_export(), llm=_down_llm())
    with _svc(_claim()) as m:
        await split_tasks.segment_taleo_export(ctx, _sid())
    proposal, _kw = _stored(m)
    assert proposal.applicants[0].resume_pages == [1]
    assert proposal.applicants[0].cover_letter_pages == [2]


@pytest.mark.asyncio
async def test_heuristic_only_never_calls_the_llm() -> None:
    llm = _FakeLLM(result=SplitProposal())
    ctx, _c, _b = _ctx(pdf=_three_page_export(), llm=llm)
    with _svc(_claim()) as m:
        out = await split_tasks.segment_taleo_export(ctx, _sid(), heuristic_only=True)
    assert out == "proposed"
    llm.chat_json.assert_not_awaited()
    _proposal, kw = _stored(m)
    assert kw["method"] == "heuristic"
    assert kw["fallback_reason"] == "requeued_after_stall"


# -- terminal failures: static reasons, never free text ----------------------


async def _run_failing(pdf: bytes | BaseException, **settings: Any) -> Any:
    llm = _FakeLLM(result=SplitProposal())
    ctx, _c, _b = _ctx(pdf=pdf, llm=llm)
    sid = _sid()
    with _svc(_claim()) as m:
        if settings:
            with patch.object(
                split_tasks, "get_settings", return_value=Settings(**settings)
            ):
                out = await split_tasks.segment_taleo_export(ctx, sid)
        else:
            out = await split_tasks.segment_taleo_export(ctx, sid)
    llm.chat_json.assert_not_awaited()
    m.store_proposal.assert_not_awaited()
    assert out == "failed"
    m.fail.assert_awaited_once()
    assert str(m.fail.await_args.args[2]) == sid
    return m.fail.await_args.args[3]


@pytest.mark.asyncio
async def test_unreadable_pdf_fails_with_a_static_reason() -> None:
    assert await _run_failing(b"this is not a pdf at all") == "unreadable_pdf"


@pytest.mark.asyncio
async def test_encrypted_pdf_fails_with_a_static_reason() -> None:
    assert await _run_failing(_encrypted_pdf()) == "encrypted_pdf"


@pytest.mark.asyncio
async def test_pdf_with_no_text_layer_fails_with_a_static_reason() -> None:
    blank = _pdf(["", "", ""])
    assert await _run_failing(blank) == "no_text_layer"


@pytest.mark.asyncio
async def test_over_page_cap_pdf_fails_before_any_llm_work() -> None:
    pdf = _pdf([_resume(f"p{i}@example.com") for i in range(5)])
    assert await _run_failing(pdf, split_max_pages=3) == "too_many_pages"


@pytest.mark.asyncio
async def test_missing_source_blob_fails_with_a_static_reason() -> None:
    assert await _run_failing(BlobNotFound("split_sources/x.pdf")) == "source_missing"


@pytest.mark.asyncio
async def test_pdf_at_exactly_the_page_cap_is_accepted() -> None:
    pdf = _pdf([_resume(f"p{i}@example.com") for i in range(3)])
    ctx, _c, _b = _ctx(pdf=pdf)
    with (
        _svc(_claim()) as m,
        patch.object(
            split_tasks, "get_settings", return_value=Settings(split_max_pages=3)
        ),
    ):
        out = await split_tasks.segment_taleo_export(ctx, _sid())
    assert out == "proposed"
    m.fail.assert_not_awaited()


# -- idempotence / redelivery ------------------------------------------------


@pytest.mark.asyncio
async def test_redelivery_of_an_already_claimed_row_does_nothing() -> None:
    llm = _FakeLLM(result=SplitProposal())
    ctx, _c, blob = _ctx(pdf=_three_page_export(), llm=llm)
    with _svc(None) as m:
        out = await split_tasks.segment_taleo_export(ctx, _sid())
    assert out == "skipped"
    blob.get.assert_not_awaited()
    llm.chat_json.assert_not_awaited()
    m.store_proposal.assert_not_awaited()
    m.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_store_race_is_skipped_not_raised() -> None:
    ctx, _c, _b = _ctx(pdf=_three_page_export())
    with _svc(_claim(), stored=False) as m:
        out = await split_tasks.segment_taleo_export(ctx, _sid())
    assert out == "skipped"
    m.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_never_deletes_the_blob_itself_on_success() -> None:
    # the blob stays until accept/discard/expiry: the service owns deletion
    ctx, _c, blob = _ctx(pdf=_three_page_export())
    with _svc(_claim()):
        await split_tasks.segment_taleo_export(ctx, _sid())
    blob.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_claims_the_row_it_was_asked_for() -> None:
    ctx, _c, _b = _ctx(pdf=_three_page_export())
    sid = uuid4()
    with _svc(_claim()) as m:
        await split_tasks.segment_taleo_export(ctx, str(sid))
    assert m.claim.await_args.args[1] == sid
    assert isinstance(m.claim.await_args.args[1], UUID)


# -- sync CPU work is off the event loop -------------------------------------


@pytest.mark.asyncio
async def test_pdf_text_extraction_runs_in_asyncio_to_thread() -> None:
    real = asyncio.to_thread
    spy = AsyncMock(side_effect=real)
    ctx, _c, _b = _ctx(pdf=_three_page_export())
    with _svc(_claim()), patch.object(split_tasks.asyncio, "to_thread", spy):
        await split_tasks.segment_taleo_export(ctx, _sid())
    funcs = [c.args[0] for c in spy.await_args_list]
    assert svc.extract_page_texts in funcs


@pytest.mark.asyncio
async def test_heuristic_and_validation_also_run_in_a_thread() -> None:
    real = asyncio.to_thread
    spy = AsyncMock(side_effect=real)
    ctx, _c, _b = _ctx(pdf=_three_page_export(), llm=_down_llm())
    with _svc(_claim()), patch.object(split_tasks.asyncio, "to_thread", spy):
        await split_tasks.segment_taleo_export(ctx, _sid())
    funcs = [c.args[0] for c in spy.await_args_list]
    assert svc.extract_page_texts in funcs
    assert svc.validate_proposal in funcs


# -- the hourly cron ---------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_expires_sweeps_and_requeues_stale_rows() -> None:
    stale = [uuid4(), uuid4()]
    ctx, _c, _b = _ctx(pdf=b"")
    with _svc(None) as m:
        m.requeue_stale.return_value = stale
        m.expire.return_value = 3
        m.sweep_orphan_blobs.return_value = 1
        out = await split_tasks.expire_split_requests(ctx)
    m.expire.assert_awaited_once()
    m.sweep_orphan_blobs.assert_awaited_once()
    # Derived from the LLM retry budget, no longer a fixed 30: with default
    # settings (2 retries, 120 s timeout) that is (2+1)*120/60 + 5 = 11 min.
    assert m.requeue_stale.await_args.kwargs["minutes"] == pytest.approx(11.0)
    assert isinstance(out, str)
    arq = ctx["arq"]
    assert arq.enqueue_job.await_count == 2
    for call, sid in zip(arq.enqueue_job.await_args_list, stale, strict=True):
        assert call.args[0] == "segment_taleo_export"
        assert str(sid) in [str(a) for a in call.args[1:]]
        assert call.kwargs.get("heuristic_only") is True or True in call.args[2:]
        assert str(sid) in call.kwargs["_job_id"]


@pytest.mark.asyncio
async def test_cron_with_nothing_to_do_enqueues_nothing() -> None:
    ctx, _c, _b = _ctx(pdf=b"")
    with _svc(None):
        await split_tasks.expire_split_requests(ctx)
    ctx["arq"].enqueue_job.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_never_raises_when_a_step_blows_up() -> None:
    ctx, _c, _b = _ctx(pdf=b"")
    with _svc(None) as m:
        m.expire.side_effect = RuntimeError("db gone")
        out = await split_tasks.expire_split_requests(ctx)
    assert out == "error"


@pytest.mark.asyncio
async def test_cron_reports_unavailable_when_ctx_is_incomplete() -> None:
    out = await split_tasks.expire_split_requests({})
    assert out == "unavailable"


@pytest.mark.asyncio
async def test_cron_expiry_ttl_comes_from_settings_not_a_literal() -> None:
    ctx, _c, _b = _ctx(pdf=b"")
    with _svc(None) as m:
        await split_tasks.expire_split_requests(ctx)
    # no explicit ttl => the service applies settings.split_proposal_ttl_h
    assert m.expire.await_args.kwargs.get("ttl_hours") is None


# -- item 7: the stale threshold derives from the LLM settings ----------------
#
# A segmenting row is only stalled once it has outlived the model's own worst
# case: ``llm_max_retries + 1`` attempts (LLMClient's HTTP-layer retry budget,
# ``Settings.llm_max_retries`` -- the retry count the worker's LLMClient really
# uses) of ``llm_timeout_s`` each, plus 300 s of slack. The cron hands that to
# ``requeue_stale(minutes=...)`` (a float number of minutes).


@pytest.mark.parametrize(
    ("retries", "timeout_s", "expected_s"),
    [(2, 120.0, 660.0), (1, 100.0, 500.0), (4, 60.0, 600.0), (0, 30.0, 330.0)],
)
@pytest.mark.asyncio
async def test_cron_stale_threshold_is_derived_from_llm_settings(
    retries: int, timeout_s: float, expected_s: float
) -> None:
    ctx, _c, _b = _ctx(pdf=b"")
    settings = Settings(llm_max_retries=retries, llm_timeout_s=timeout_s)
    with (
        _svc(None) as m,
        patch.object(split_tasks, "get_settings", return_value=settings),
    ):
        await split_tasks.expire_split_requests(ctx)
    minutes = m.requeue_stale.await_args.kwargs["minutes"]
    assert minutes == pytest.approx(expected_s / 60)
