"""RED -- interview-question generator, review round 3.

H1 -- weak name tokens (shorter than 3 chars, or on ``_COMMON_WORD_NAME_TOKENS``)
STANDALONE rule, encoded exactly:

* The LAST token of the name (the surname; the only token of a one-word name)
  is matched standalone when its source span is Title-case, i.e. the first
  letter of the span is uppercase in the ORIGINAL text (checked after mapping
  the folded match back through ``_fold_with_map``), ANYWHERE in the sentence
  (start of text included). "Li", "LI" and "Grant" match; "li" and "grant" do
  not. A lowercase weak surname is never scrubbed ("the li element").
* A weak token that is NOT the last token (a first/middle name: "Will" in
  "Will Grant", "An" in "An Tran", "June" in "June Rose") is NEVER matched
  alone: the round-2 tests keep "Will you walk me through grants" and "What
  happened in June" for "Will Grant" / "June Rose", and a month or an auxiliary
  verb cannot be told from a first name. It is scrubbed only inside the
  full-name sequence.
* Strong tokens (3+ chars, not on the list) match alone in any case, as before.
* The REVERSED full-name sequence ("Grant, Will", "Li, Jane", "Tran An") is
  scrubbed as a whole: tokens in reverse order separated by whitespace and/or
  commas.

H2 -- U+2010, U+2011, U+2012, U+2015, U+2212, U+FE63, U+FF0D are phone
separators (candidate's phone, anyone's phone, and the 7-9 digit stored-phone
loose fallback). Short runs are untouched. Non-ASCII is written as escapes.

H3 -- a heartbeat that returns False means the run is superseded: if the row
still exists the worker logs a WARNING containing "superseded" and returns
before ``load_identifiers`` / ``get_one`` / the model; if the row is gone it
returns "failed" with no model call.

H4 -- ``claim_id`` defaults to None (an old queued job from before claims):
the worker records nothing, makes no model call, warns, returns "failed".
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import pytest

from src.services import interview_question_service as iq_service
from src.services.interview_questions import (
    Gap,
    InterviewQuestionsIngest,
    filter_questions,
    scrub_identifiers,
)
from tests.unit.test_worker_interview_tasks import (
    _FakeLLM,
    _Harness,
    _ingest,
    _q,
    _task,
)


def _scrub(text: str, **kw: Any) -> str:
    args: dict[str, Any] = {"name": None, "email": None, "phone": None}
    args.update(kw)
    return scrub_identifiers(text, **args)


def _filter(question: str, **kw: Any) -> tuple[list[str], int]:
    args: dict[str, Any] = {"name": None, "email": None, "phone": None}
    args.update(kw)
    ingest = InterviewQuestionsIngest.model_validate(
        {"questions": [{"requirement_index": 2, "question": question}]}
    )
    gap = Gap(index=2, requirement="Budget ownership", status="missing")
    kept, dropped = filter_questions(ingest, [gap], **args)
    return [k.question for k in kept], dropped


# -- H1a: Title-case weak surname -------------------------------------------


@pytest.mark.parametrize(
    ("name", "text", "forbidden"),
    [
        ("Jane Li", "Dr. Li led the team.", "Li"),
        ("Jane Li", "Li led the team.", "Li"),
        ("Jane Li", "Ask LI about it.", "LI"),
        ("Will Grant", "Grant led the migration.", "Grant"),
        ("Will Grant", "Please thank Mr. Grant for it.", "Grant"),
        ("Will Grant", "Ask GRANT about it.", "GRANT"),
        ("An Tran", "Dr. Tran led it.", "Tran"),
    ],
)
def test_a_title_case_weak_surname_is_scrubbed_alone(
    name: str, text: str, forbidden: str
) -> None:
    out = _scrub(text, name=name)
    assert forbidden not in out
    assert "[redacted]" in out


@pytest.mark.parametrize(
    ("name", "question"),
    [
        ("Jane Li", "How did Li approach the rollout?"),
        ("Will Grant", "How did Grant approach the rollout?"),
    ],
)
def test_filter_drops_a_question_naming_the_title_case_weak_surname(
    name: str, question: str
) -> None:
    kept, dropped = _filter(question, name=name)
    assert kept == []
    assert dropped == 1


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Jane Li", "Explain how the li element nests in a list."),
        ("Will Grant", "Tell me about grant funding you secured."),
        ("Will Grant", "How will lead time change under this plan?"),
        ("An Tran", "Tell me about an occasion you led."),
        ("Bill", "Explain how the bill passed through review."),
    ],
)
def test_lowercase_weak_tokens_are_untouched_and_the_question_kept(
    name: str, text: str
) -> None:
    assert _scrub(text, name=name) == text
    kept, dropped = _filter(text, name=name)
    assert kept == [text]
    assert dropped == 0


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Will Grant", "Will you walk me through grants you wrote?"),
        ("An Tran", "An engineer on the team asked about it."),
        ("June Rose", "What happened in June when the rose bush died?"),
    ],
)
def test_a_title_case_weak_first_name_is_never_matched_alone(
    name: str, text: str
) -> None:
    assert _scrub(text, name=name) == text


# -- H1d: single weak token -------------------------------------------------


@pytest.mark.parametrize(
    "text", ["Ask Bill about it.", "Bill led the migration.", "Thanks, Bill."]
)
def test_a_single_weak_token_name_scrubs_title_case_standalone(text: str) -> None:
    out = _scrub(text, name="Bill")
    assert "Bill" not in out
    assert "[redacted]" in out
    kept, dropped = _filter(text, name="Bill")
    assert kept == []
    assert dropped == 1


# -- H1c: reversed order ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "text", "forbidden"),
    [
        ("Will Grant", "Grant, Will led the migration.", ("Will", "Grant")),
        ("Jane Li", "Li, Jane led the migration.", ("Jane", "Li")),
        ("An Tran", "Tran An led the migration.", ("Tran", " An ")),
        ("Jane Smith", "Smith, Jane led the migration.", ("Smith", "Jane")),
        ("Jane Smith", "Smith Jane led the migration.", ("Smith", "Jane")),
    ],
)
def test_the_reversed_full_name_is_scrubbed_as_a_whole(
    name: str, text: str, forbidden: tuple[str, ...]
) -> None:
    out = _scrub(text, name=name)
    for word in forbidden:
        assert word not in f" {out} "
    assert "[redacted]" in out
    assert out.endswith(" led the migration.")


def test_filter_drops_a_reversed_name_question() -> None:
    kept, dropped = _filter("How did Tran An lead it?", name="An Tran")
    assert kept == []
    assert dropped == 1


# -- H2: more dashes --------------------------------------------------------

_DASHES = ["‐", "‑", "‒", "―", "−", "﹣", "－"]


@pytest.mark.parametrize("sep", _DASHES)
def test_the_candidates_phone_is_redacted_with_the_extra_dashes(sep: str) -> None:
    out = _scrub(f"Call 604{sep}555{sep}1234 any time.", phone="604-555-1234")
    assert "1234" not in out
    assert "555" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize("sep", _DASHES)
def test_anyones_phone_is_redacted_with_the_extra_dashes(sep: str) -> None:
    out = _scrub(f"Her referee is on 604{sep}555{sep}1234.", phone="778-555-0100")
    assert "1234" not in out
    out_none = _scrub(f"Her referee is on 604{sep}555{sep}1234.")
    assert "1234" not in out_none


@pytest.mark.parametrize("sep", _DASHES)
def test_the_short_stored_phone_loose_fallback_takes_the_extra_dashes(
    sep: str,
) -> None:
    out = _scrub(f"Reach me on 555{sep}1234 please.", phone="555-1234")
    assert "1234" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize("sep", _DASHES)
def test_filter_drops_a_phone_with_the_extra_dashes(sep: str) -> None:
    kept, dropped = _filter(
        f"Can we ring 604{sep}555{sep}1234 about budgets?", phone="604-555-1234"
    )
    assert kept == []
    assert dropped == 1
    kept, dropped = _filter(f"Can we ring 604{sep}555{sep}1234 about budgets?")
    assert kept == []
    assert dropped == 1


@pytest.mark.parametrize("sep", _DASHES)
def test_short_runs_with_the_extra_dashes_are_untouched(sep: str) -> None:
    benign = f"Led the team from 2016{sep}2019 and 2020{sep}2022"
    assert _scrub(benign, phone="604-555-1234") == benign
    assert _scrub(benign) == benign
    kept, dropped = _filter(benign, phone="604-555-1234")
    assert kept == [benign]
    assert dropped == 0


# -- H3: a superseded run skips the model -----------------------------------


@pytest.mark.asyncio
async def test_a_superseded_run_warns_and_never_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    llm = _FakeLLM(_ingest(_q(2, "Describe a budget.")))
    h = _Harness(monkeypatch, llm=llm)
    h.heartbeat.return_value = False
    load = _spy_load_identifiers(monkeypatch)
    with caplog.at_level(logging.WARNING):
        await h.run()
    assert llm.calls == []
    h.get_one.assert_not_awaited()
    assert load == []
    assert h.recorded() == (0, 0, 0)
    assert any(
        "superseded" in r.getMessage().lower() and r.levelno >= logging.WARNING
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_superseded_run_returns_a_status_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _Harness(monkeypatch, llm=_FakeLLM(_ingest(_q(2, "Describe a budget."))))
    h.heartbeat.return_value = False
    assert isinstance(await h.run(), str)


@pytest.mark.asyncio
async def test_a_superseded_beat_with_the_row_gone_fails_without_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(_ingest(_q(2, "Describe a budget.")))
    h = _Harness(monkeypatch, llm=llm)
    h.heartbeat.return_value = False

    entry_id = h.entry_id

    async def _only_the_row_is_gone(sql: str, *_a: Any, **_k: Any) -> Any:
        # the shortlist entry still resolves; only the questions row is gone
        return entry_id if "shortlist_entries" in sql else None

    h.conn.fetchval = _only_the_row_is_gone  # type: ignore[method-assign]
    assert await h.run() == "failed"
    assert llm.calls == []
    h.get_one.assert_not_awaited()
    assert h.ready.await_count == 0


@pytest.mark.asyncio
async def test_a_live_heartbeat_still_runs_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _FakeLLM(_ingest(_q(2, "Describe a budget.")))
    h = _Harness(monkeypatch, llm=llm)
    assert await h.run() == "ready"
    assert len(llm.calls) == 1


def _spy_load_identifiers(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []
    real = iq_service.load_identifiers

    async def _spy(*a: Any, **k: Any) -> Any:
        calls.append(a)
        return await real(*a, **k)

    monkeypatch.setattr(iq_service, "load_identifiers", _spy)
    return calls


# -- H4: an old queued job without a claim ----------------------------------


def test_claim_id_defaults_to_none() -> None:
    param = inspect.signature(_task()).parameters["claim_id"]
    assert param.default is None


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_a_job_without_a_claim_records_nothing_and_skips_the_model(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    explicit: bool,
) -> None:
    llm = _FakeLLM(_ingest(_q(2, "Describe a budget.")))
    h = _Harness(monkeypatch, llm=llm)
    args: tuple[Any, ...] = (h.ctx, str(h.job_id), str(h.resume_id))
    if explicit:
        args += (None,)
    with caplog.at_level(logging.WARNING):
        result = await _task()(*args)
    assert result == "failed"
    assert llm.calls == []
    assert h.recorded() == (0, 0, 0)
    h.heartbeat.assert_not_awaited()
    h.get_one.assert_not_awaited()
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
