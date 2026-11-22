"""RED -- interview-question generator, slice S2: the arq task
``src.worker.interview_tasks.generate_interview_questions(ctx, job_id, resume_id)``.

Everything external is faked: the pool/connection, the LLM client (a fake with
``chat_json`` -- the REAL ``select_gaps`` / ``generate`` / ``filter_questions``
from S1 run), ``shortlist_service.get_one``, ``pii.decrypt`` and the three
``record_*`` writers. That the writers really persist is the integration file's
job.

Contract (decisions of this RED commit):

* ids arrive as ``str`` (arq payload); the task returns the status it recorded:
  ``"ready"`` / ``"refused"`` / ``"failed"``.
* The entry is rebuilt ONLY via ``shortlist_service.get_one`` (unscoped,
  ``user_id`` unset), looked up from ``(job_id, resume_id)`` on the connection.
  A lookup miss, or a ``NotFoundError`` from ``get_one`` (e.g. a WITHDRAWN
  résumé), records ``failed`` / ``entry_gone`` and never calls the model.
* Refusals (S1's ``InterviewRefusal``) record ``refused`` with that reason and
  never call the model.
* Schema-invalid output (``LLMOutputInvalidError``) -> ``failed`` /
  ``invalid_output``. Nothing survives the filter -> ``failed`` /
  ``no_questions``. ``LLMUnavailableError`` propagates (arq retries) and
  records nothing.
* name / email / phone come from decrypting the résumé's candidate columns via
  ``src.services.pii`` (``set_pii_key`` then ``decrypt``, module attributes),
  are used ONLY for the post-generation filter, and appear in no message sent
  to the model.
* ``record_ready(conn, job_id, resume_id, *, questions, gaps, model)`` with
  ``model == get_settings().llm_model_generation``; ``record_failed`` /
  ``record_refused`` take the reason as 4th positional or ``reason=``.
"""

from __future__ import annotations

import datetime as dt
import importlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.errors import NotFoundError
from src.pipeline.llm import LLMOutputInvalidError, LLMUnavailableError
from src.schemas.matching import (
    EvidenceObject,
    EvidenceStatus,
    RequirementEvidence,
    ScoreBreakdown,
    ShortlistEntry,
)
from src.services import pii as pii_service
from src.services import shortlist_service
from src.services.interview_questions import (
    InterviewQuestionsIngest,
    RefusalReason,
    select_gaps,
)
from src.settings import get_settings

NAME = "Jane Smith"
EMAIL = "jane.smith@example.test"
PHONE = "604-555-0192"
_CT = {b"name-ct": NAME, b"email-ct": EMAIL, b"phone-ct": PHONE}

QUOTE = "Deployed a pod once in a lab"


def _iq() -> Any:
    return importlib.import_module("src.services.interview_question_service")


def _aw(mock: Any) -> Any:
    assert mock.await_args is not None
    return mock.await_args


def _task() -> Any:
    return importlib.import_module(
        "src.worker.interview_tasks"
    ).generate_interview_questions


class _Row(dict[str, Any]):
    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _acm(value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class _FakeConn:
    """Answers the worker's entry/résumé lookups regardless of exact SQL: any
    fetchrow yields one merged row (entry id + the three PII ciphertexts), any
    fetchval yields the entry id; ``exists=False`` makes every lookup miss."""

    def __init__(
        self, entry_id: UUID, *, exists: bool = True, phone: bytes | None = b"phone-ct"
    ) -> None:
        self._row = (
            _Row(
                id=entry_id,
                candidate_name=b"name-ct",
                candidate_email=b"email-ct",
                candidate_phone=phone,
            )
            if exists
            else None
        )
        self._entry_id = entry_id if exists else None
        self.execute = AsyncMock(return_value="UPDATE 1")
        self.fetch = AsyncMock(return_value=[])

    async def fetchrow(self, *_a: Any, **_k: Any) -> Any:
        return self._row

    async def fetchval(self, *_a: Any, **_k: Any) -> Any:
        return self._entry_id

    def transaction(self) -> MagicMock:
        return _acm()


class _FakeLLM:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[list[dict[str, str]], Any]] = []

    async def chat_json(self, messages: Any, schema: Any, **_kw: Any) -> Any:
        self.calls.append((list(messages), schema))
        if self.error is not None:
            raise self.error
        return self.result


def _req(text: str, status: EvidenceStatus, quote: str = "") -> RequirementEvidence:
    return RequirementEvidence.model_validate(
        {"requirement": text, "status": status, "evidence": quote, "confidence": 0.6}
    )


def _entry(
    entry_id: UUID,
    job_id: UUID,
    resume_id: UUID,
    *,
    requirements: list[RequirementEvidence] | None = None,
    evaluated: bool | None = True,
    blinded: bool = False,
) -> ShortlistEntry:
    reqs = (
        requirements
        if requirements is not None
        else [
            _req("5 years of Python", "met", "Built Python services since 2016"),  # 0
            _req("Kubernetes operations", "partial", QUOTE),  # 1 gap
            _req("Budget ownership", "missing"),  # 2 gap
            _req("Mentoring juniors", "met", "Mentored four interns"),  # 3
            _req("Terraform", "missing"),  # 4 gap
        ]
    )
    return ShortlistEntry.model_validate(
        {
            "id": entry_id,
            "job_id": job_id,
            "resume_id": resume_id,
            "rank": 3,
            "score_final": 0.7,
            "score_breakdown": ScoreBreakdown(
                skill=0.8,
                experience=0.7,
                education=0.6,
                seniority=0.5,
                vector=0.4,
                structured=0.65,
            ),
            "evidence": EvidenceObject(requirements=reqs),
            "pipeline_meta": None,
            "generated_at": dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
            "evidence_evaluated": evaluated,
            "blinded": blinded,
            "display_label": "Candidate C" if blinded else NAME,
        }
    )


def _ingest(*items: dict[str, Any]) -> InterviewQuestionsIngest:
    return InterviewQuestionsIngest.model_validate({"questions": list(items)})


def _q(index: int, text: str, quote: str | None = None) -> dict[str, Any]:
    return {"requirement_index": index, "question": text, "probes_quote": quote}


class _Harness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        llm: _FakeLLM,
        entry: ShortlistEntry | None = None,
        lookup_hit: bool = True,
        get_one_error: Exception | None = None,
        phone: bytes | None = b"phone-ct",
        requirements: list[RequirementEvidence] | None = None,
        evaluated: bool | None = True,
        blinded: bool = False,
    ) -> None:
        self.job_id, self.resume_id, self.entry_id = uuid4(), uuid4(), uuid4()
        self.entry = entry or _entry(
            self.entry_id,
            self.job_id,
            self.resume_id,
            requirements=requirements,
            evaluated=evaluated,
            blinded=blinded,
        )
        self.llm = llm
        self.conn = _FakeConn(self.entry_id, exists=lookup_hit, phone=phone)
        pool = MagicMock(name="pg_pool")
        pool.acquire = MagicMock(return_value=_acm(self.conn))
        self.ctx: dict[str, Any] = {"pg_pool": pool, "llm": llm, "job_try": 1}

        self.get_one = AsyncMock(side_effect=get_one_error, return_value=self.entry)
        self.set_key = AsyncMock(return_value=None)

        async def _decrypt(_conn: Any, ct: bytes | None) -> str | None:
            return None if ct is None else _CT.get(ct)

        self.decrypt = AsyncMock(side_effect=_decrypt)
        self.ready = AsyncMock(return_value=None)
        self.failed = AsyncMock(return_value=None)
        self.refused = AsyncMock(return_value=None)
        monkeypatch.setattr(shortlist_service, "get_one", self.get_one)
        monkeypatch.setattr(pii_service, "set_pii_key", self.set_key)
        monkeypatch.setattr(pii_service, "decrypt", self.decrypt)
        monkeypatch.setattr(_iq(), "record_ready", self.ready)
        monkeypatch.setattr(_iq(), "record_failed", self.failed)
        monkeypatch.setattr(_iq(), "record_refused", self.refused)

    async def run(self) -> Any:
        return await _task()(self.ctx, str(self.job_id), str(self.resume_id))

    def recorded(self) -> tuple[int, int, int]:
        return (
            self.ready.await_count,
            self.failed.await_count,
            self.refused.await_count,
        )


def _reason(call: Any) -> str:
    value = call.kwargs["reason"] if "reason" in call.kwargs else call.args[3]
    return str(getattr(value, "value", value))


def _prompt_blob(llm: _FakeLLM) -> str:
    return "\n".join(m["content"] for msgs, _ in llm.calls for m in msgs)


# ── happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ready_records_the_surviving_questions_and_the_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(
        _ingest(
            _q(1, "Walk me through the lab deployment.", QUOTE),
            _q(2, "Describe a budget you were accountable for."),
        )
    )
    h = _Harness(monkeypatch, llm=llm)

    assert await h.run() == "ready"

    assert h.recorded() == (1, 0, 0)
    call = _aw(h.ready)
    assert tuple(call.args[1:3]) == (h.job_id, h.resume_id)
    kept = call.kwargs["questions"]
    assert [(k.requirement_index, k.question) for k in kept] == [
        (1, "Walk me through the lab deployment."),
        (2, "Describe a budget you were accountable for."),
    ]
    assert call.kwargs["gaps"] == select_gaps(h.entry)
    assert call.kwargs["model"] == get_settings().llm_model_generation


@pytest.mark.asyncio
async def test_the_entry_is_rebuilt_via_get_one_unscoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, llm=_FakeLLM(_ingest(_q(2, "Describe a budget."))))
    await h.run()
    h.get_one.assert_awaited_once()
    call = _aw(h.get_one)
    assert h.entry_id in call.args
    assert call.kwargs.get("user_id") is None


@pytest.mark.asyncio
async def test_the_model_is_called_once_with_the_ingest_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(_ingest(_q(2, "Describe a budget.")))
    await _Harness(monkeypatch, llm=llm).run()
    assert len(llm.calls) == 1
    assert llm.calls[0][1] is InterviewQuestionsIngest


# ── identifiers: filter only, never the prompt ───────────────────────────


@pytest.mark.parametrize("blinded", [False, True])
@pytest.mark.asyncio
async def test_no_name_email_or_phone_reaches_the_prompt(
    blinded: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _FakeLLM(_ingest(_q(2, "Describe a budget.")))
    h = _Harness(monkeypatch, llm=llm, blinded=blinded)
    await h.run()
    blob = _prompt_blob(llm).casefold()
    assert blob, "the model was never called"
    for forbidden in (
        "jane",
        "smith",
        EMAIL,
        "example.test",
        PHONE,
        "555-0192",
        "5550192",
        str(h.entry_id),
        str(h.resume_id),
        str(h.job_id),
    ):
        assert forbidden.casefold() not in blob, forbidden


@pytest.mark.asyncio
async def test_the_decrypted_identifiers_drive_the_post_generation_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(
        _ingest(
            _q(1, "Walk me through the lab deployment.", QUOTE),
            _q(2, "Jane, how did you own a budget?"),
            _q(2, "Who can I email about budgets, jane.smith@example.test?"),
            _q(4, "Call 604-555-0192 and describe your Terraform use."),
            _q(9, "A question about a requirement that was never supplied."),
            _q(4, "Describe how you would learn Terraform."),
        )
    )
    h = _Harness(monkeypatch, llm=llm)

    assert await h.run() == "ready"

    kept = [k.question for k in _aw(h.ready).kwargs["questions"]]
    assert kept == [
        "Walk me through the lab deployment.",
        "Describe how you would learn Terraform.",
    ]
    h.set_key.assert_awaited()
    assert h.decrypt.await_count >= 1


@pytest.mark.asyncio
async def test_a_missing_phone_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(
        _ingest(_q(1, "Walk me through the lab deployment.", QUOTE), _q(2, "Jane?"))
    )
    h = _Harness(monkeypatch, llm=llm, phone=None)
    assert await h.run() == "ready"
    kept = [k.question for k in _aw(h.ready).kwargs["questions"]]
    assert kept == ["Walk me through the lab deployment."]


# ── refusals: typed, and the model is never called ───────────────────────


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"evaluated": False}, RefusalReason.NOT_EVALUATED),
        ({"evaluated": None}, RefusalReason.NOT_EVALUATED),
        ({"requirements": []}, RefusalReason.NO_REQUIREMENTS),
        (
            {"requirements": [_req("5 years of Python", "met", "Built services")]},
            RefusalReason.NO_GAPS,
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_refusal_is_recorded_with_its_reason_and_skips_the_model(
    kwargs: dict[str, Any], reason: RefusalReason, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _FakeLLM(_ingest(_q(2, "never used")))
    h = _Harness(monkeypatch, llm=llm, **kwargs)

    assert await h.run() == "refused"

    assert h.recorded() == (0, 0, 1)
    call = _aw(h.refused)
    assert tuple(call.args[1:3]) == (h.job_id, h.resume_id)
    assert _reason(call) == reason.value
    assert llm.calls == []


# ── failures ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_schema_invalid_output_records_failed_invalid_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(error=LLMOutputInvalidError("not json"))
    h = _Harness(monkeypatch, llm=llm)

    assert await h.run() == "failed"

    assert h.recorded() == (0, 1, 0)
    assert _reason(_aw(h.failed)) == "invalid_output"


@pytest.mark.asyncio
async def test_an_empty_result_records_failed_no_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, llm=_FakeLLM(_ingest()))

    assert await h.run() == "failed"

    assert h.recorded() == (0, 1, 0)
    assert _reason(_aw(h.failed)) == "no_questions"


@pytest.mark.asyncio
async def test_a_result_the_filter_empties_records_failed_no_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(_ingest(_q(9, "cites nothing we supplied"), _q(2, "Jane, why?")))
    h = _Harness(monkeypatch, llm=llm)

    assert await h.run() == "failed"

    assert h.recorded() == (0, 1, 0)
    assert _reason(_aw(h.failed)) == "no_questions"


@pytest.mark.parametrize("how", ["lookup_miss", "get_one_not_found"])
@pytest.mark.asyncio
async def test_a_vanished_entry_records_failed_entry_gone_and_skips_the_model(
    how: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = _FakeLLM(_ingest(_q(2, "never used")))
    h = _Harness(
        monkeypatch,
        llm=llm,
        lookup_hit=how != "lookup_miss",
        get_one_error=NotFoundError("gone") if how == "get_one_not_found" else None,
    )

    assert await h.run() == "failed"

    assert h.recorded() == (0, 1, 0)
    assert _reason(_aw(h.failed)) == "entry_gone"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_llm_unavailable_propagates_for_arq_retry_and_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(error=LLMUnavailableError("breaker open"))
    h = _Harness(monkeypatch, llm=llm)

    with pytest.raises(LLMUnavailableError):
        await h.run()

    assert h.recorded() == (0, 0, 0)


# ── wiring ───────────────────────────────────────────────────────────────


def test_the_task_is_a_coroutine_taking_ctx_and_two_ids() -> None:
    import inspect

    fn = _task()
    assert inspect.iscoroutinefunction(fn)
    assert list(inspect.signature(fn).parameters) == ["ctx", "job_id", "resume_id"]


# ── identifiers inside a partial QUOTE (non-blind jobs) ──────────────────

PII_QUOTE = (
    "Jane deployed a pod once in a lab; mail jane.smith@example.test; "
    "call 604-555-0192 for details"
)
QUOTE_FRAGMENT = "deployed a pod once in a lab"


def _pii_quote_requirements() -> list[RequirementEvidence]:
    return [
        _req("5 years of Python", "met", "Built Python services since 2016"),
        _req("Kubernetes operations", "partial", PII_QUOTE),
        _req("Budget ownership", "missing"),
    ]


@pytest.mark.asyncio
async def test_identifiers_in_a_partial_quote_never_reach_the_prompt_on_a_non_blind_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(_ingest(_q(2, "Describe a budget.")))
    h = _Harness(monkeypatch, llm=llm, requirements=_pii_quote_requirements())
    assert h.entry.blinded is False

    await h.run()

    blob = _prompt_blob(llm)
    assert blob, "the model was never called"
    folded = blob.casefold()
    for forbidden in ("jane", "smith", EMAIL, "example.test", PHONE, "555-0192"):
        assert forbidden.casefold() not in folded, forbidden
    assert "[redacted]" in blob
    assert QUOTE_FRAGMENT in blob  # only the identifiers go, not the evidence


@pytest.mark.asyncio
async def test_the_filter_runs_against_the_scrubbed_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``probes_quote`` must match the REDACTED quote: the original
    (identifier-bearing) text is no longer a substring and is dropped, while a
    fragment that survived the scrub is kept."""
    llm = _FakeLLM(
        _ingest(
            _q(1, "Walk me through the full original quote.", PII_QUOTE),
            _q(1, "Walk me through the lab deployment.", QUOTE_FRAGMENT),
            _q(2, "Describe a budget you were accountable for."),
        )
    )
    h = _Harness(monkeypatch, llm=llm, requirements=_pii_quote_requirements())

    assert await h.run() == "ready"

    kept = [k.question for k in _aw(h.ready).kwargs["questions"]]
    assert kept == [
        "Walk me through the lab deployment.",
        "Describe a budget you were accountable for.",
    ]
    for gap in _aw(h.ready).kwargs["gaps"]:
        quote = (gap.quote or "").casefold()
        for forbidden in ("jane", "smith", EMAIL, PHONE, "555-0192"):
            assert forbidden.casefold() not in quote, forbidden
