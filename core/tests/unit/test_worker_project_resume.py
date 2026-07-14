"""Unit tests for ``src.worker.resume_tasks.project_resume`` /
``_resume_projection_tx`` — the résumé half of Phase 4b's graph projection.

Uses a FAKE Neo4j tx/session that RECORDS every Cypher statement and its
params, so every assertion below is against what would actually be written,
not against a mocked return value. Ported behaviourally from hris
``apps/worker/src/worker/resume_tasks.py::project_resume`` /
``_resume_projection_tx``, with the human-locked deviations from
``docs/EXTRACTION_PLAN.md`` (4b row) pinned explicitly:

* **Decision 1 (CRIT) / R1 — Neo4j gets NO chunk text, ever.** Not even a
  200-char preview. hris writes ``preview=chunk["text"][:200]`` — the résumé
  header chunk's first 200 chars ARE the candidate's name/email/phone. The
  reveal path is Postgres-backed (``resumes.parsed``, ADR-007 §6), so Neo4j
  never needs it. A test asserting only "text_preview is non-empty" would
  stay green under a real leak — see
  ``test_no_text_preview_key_is_ever_written_to_any_node`` below, which walks
  every captured Cypher CALL's params instead.
* **R2 (HIGH) — the outbox payload has NO ``chunks[].text`` key at all**
  (ADR-007 §7). A verbatim ``_resume_projection_tx`` does ``chunk["text"]`` —
  a hard subscript on a key the payload doesn't have — which raises
  ``KeyError``, gets swallowed by the drainer's blanket ``except Exception``,
  and the row retries forever. Pinned by feeding chunks shaped EXACTLY like
  the real outbox payload (``id``/``section``/``page`` only).
* **Decision 3 — skill resolution happens OUTSIDE the write transaction.**
  ``project_resume`` must resolve every skill name to a canonical Neo4j name
  via a plain session BEFORE calling ``session.execute_write``, and the
  callback handed to ``execute_write`` must never receive ``llm``/``embedder``
  at all — so it CANNOT call them, architecturally, not just "doesn't
  happen to" in this test run.
* **R8 (MED) — the pinned label set.** The projection writes only
  ``{Resume, ResumeChunk, Skill}`` from this module (``Job`` is the JD side)
  — never ``Company``/``Institution``, even though core's Neo4j bootstrap
  already declares constraints for those labels (an inviting trap).

``src.worker.resume_tasks.project_resume`` does not exist yet — this whole
file fails at collection (``ImportError``). RED half of the TDD cycle.
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.worker.resume_tasks import _resume_projection_tx, project_resume

# ── fakes ─────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []

    async def single(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def __aiter__(self) -> _FakeResult:
        self._iter = iter(self._rows)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class _RecordingTx:
    """Captures every ``tx.run(cypher, **params)`` call, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, cypher: str, **params: Any) -> _FakeResult:
        self.calls.append((cypher, params))
        return _FakeResult([])


def _acm(return_value: Any) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_driver_and_tx(
    resolve_result: dict[str, str],
) -> tuple[MagicMock, _RecordingTx, AsyncMock, list[str]]:
    """A neo4j driver double whose ``session()`` yields an object exposing
    ONLY ``execute_write`` (no bare ``.run`` — resolution must go through the
    patched ``skills_graph.resolve_canonical_names``, not the projection
    session). Returns (driver, recording_tx, resolve_mock, events)."""
    tx = _RecordingTx()
    events: list[str] = []
    captured_write_args: list[tuple[Any, ...]] = []
    captured_write_kwargs: list[dict[str, Any]] = []

    async def _execute_write(fn: Any, *args: Any, **kwargs: Any) -> Any:
        events.append("write_start")
        captured_write_args.append(args)
        captured_write_kwargs.append(kwargs)
        result = await fn(tx, *args, **kwargs)
        events.append("write_end")
        return result

    session = MagicMock(name="session")
    session.execute_write = AsyncMock(side_effect=_execute_write)
    session._captured_write_args = captured_write_args
    session._captured_write_kwargs = captured_write_kwargs

    driver = MagicMock(name="driver")
    driver.session = MagicMock(return_value=_acm(session))

    async def _resolve(*args: Any, **kwargs: Any) -> dict[str, str]:
        events.append("resolve")
        return resolve_result

    resolve_mock = AsyncMock(side_effect=_resolve)
    return driver, tx, resolve_mock, events


def _outbox_chunk(
    chunk_id: str, section: str = "experience", page: int = 1
) -> dict[str, Any]:
    """A chunk exactly as the real outbox payload shapes it — NO 'text' key
    (ADR-007 §7 / R2)."""
    return {"id": chunk_id, "section": section, "page": page}


def _payload(
    *,
    skills: list[dict[str, Any]] | None = None,
    chunks: list[dict[str, Any]] | None = None,
    chunk_embs: dict[str, list[float]] | None = None,
    job_id: str | None = "11111111-1111-1111-1111-111111111111",
    total_years_experience: int = 6,
) -> dict[str, Any]:
    chunks = (
        chunks
        if chunks is not None
        else [_outbox_chunk("c_001"), _outbox_chunk("c_002")]
    )
    chunk_embs = (
        chunk_embs if chunk_embs is not None else {c["id"]: [0.1] * 8 for c in chunks}
    )
    skills = skills if skills is not None else [{"name": "python", "years": 5}]
    return {
        "parsed": {
            "total_years_experience": total_years_experience,
            "skills": skills,
            "experience": [],
            "education": [],
            "chunks": chunks,
            "cover_letter_chunks": [],
        },
        "summary_emb": [0.2] * 8,
        "chunk_embs": chunk_embs,
        "prompt_version": "resume_core_v1+resume_skills_v2",
        "job_id": job_id,
    }


def _all_run_calls(tx: _RecordingTx) -> list[tuple[str, dict[str, Any]]]:
    return tx.calls


def _all_cypher(tx: _RecordingTx) -> str:
    return "\n".join(c for c, _ in tx.calls)


def _all_params(tx: _RecordingTx) -> list[Any]:
    out: list[Any] = []
    for _cypher, params in tx.calls:
        out.extend(params.values())
    return out


async def _project(
    payload: dict[str, Any], resolve_result: dict[str, str]
) -> tuple[_RecordingTx, MagicMock, list[str]]:
    resume_id = uuid4()
    driver, tx, resolve_mock, events = _fake_driver_and_tx(resolve_result)
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock())
    with patch(
        "src.worker.resume_tasks.skills_graph.resolve_canonical_names", resolve_mock
    ):
        await project_resume(
            driver, resume_id=resume_id, payload=payload, llm=llm, embedder=embedder
        )
    return tx, driver, events


# ── Decision 1 / R1: no chunk text, no preview, anywhere ─────────────────


@pytest.mark.asyncio
async def test_no_text_preview_key_is_ever_written_to_any_node() -> None:
    """R1 (CRIT). Walks EVERY captured Cypher call's params — not just the
    ResumeChunk creation call — so a preview smuggled onto a different node
    (e.g. the Resume node itself) would also be caught."""
    tx, _driver, _events = await _project(_payload(), {"python": "python"})
    for cypher, params in _all_run_calls(tx):
        assert "text_preview" not in params
        assert "preview" not in params
        assert "text_preview" not in cypher


@pytest.mark.asyncio
async def test_resume_chunk_creation_params_never_include_a_text_key() -> None:
    tx, _driver, _events = await _project(_payload(), {"python": "python"})
    chunk_calls = [
        (cypher, params)
        for cypher, params in _all_run_calls(tx)
        if "ResumeChunk" in cypher
        and ("CREATE" in cypher.upper() or "MERGE" in cypher.upper())
    ]
    assert chunk_calls, "expected at least one ResumeChunk write"
    for _cypher, params in chunk_calls:
        assert "text" not in params


@pytest.mark.asyncio
async def test_resume_chunk_node_only_carries_id_section_page_and_embedding() -> None:
    tx, _driver, _events = await _project(_payload(), {"python": "python"})
    chunk_calls = [
        (cypher, params)
        for cypher, params in _all_run_calls(tx)
        if re.search(r"CREATE\s*\(\s*c\s*:\s*ResumeChunk", cypher, re.IGNORECASE)
    ]
    assert chunk_calls
    allowed = {"rid", "cid", "section", "page", "emb"}
    for _cypher, params in chunk_calls:
        assert set(params.keys()) <= allowed, params.keys()


# ── R2: the outbox payload has no chunks[].text — no KeyError ────────────


@pytest.mark.asyncio
async def test_projection_does_not_keyerror_on_outbox_shaped_chunks() -> None:
    """R2 (HIGH). A verbatim ``chunk["text"]`` subscript raises KeyError on
    a real outbox payload's chunks (which never carry 'text' — ADR-007 §7).
    This must not raise at all."""
    payload = _payload(chunks=[_outbox_chunk("c_001")], chunk_embs={"c_001": [0.3] * 8})
    tx, _driver, _events = await _project(payload, {"python": "python"})
    assert any("ResumeChunk" in cypher for cypher, _ in _all_run_calls(tx))


@pytest.mark.asyncio
async def test_chunks_with_no_matching_embedding_are_skipped() -> None:
    payload = _payload(
        chunks=[_outbox_chunk("c_001"), _outbox_chunk("c_002")],
        chunk_embs={"c_001": [0.3] * 8},  # c_002 has no embedding
    )
    tx, _driver, _events = await _project(payload, {"python": "python"})
    chunk_ids_written = {
        params.get("cid")
        for cypher, params in _all_run_calls(tx)
        if re.search(r"CREATE\s*\(\s*c\s*:\s*ResumeChunk", cypher, re.IGNORECASE)
    }
    assert chunk_ids_written == {"c_001"}


# ── Resume node: only total_years_experience, no PII, no candidate ───────


@pytest.mark.asyncio
async def test_resume_node_write_carries_no_candidate_key_anywhere() -> None:
    tx, _driver, _events = await _project(_payload(), {"python": "python"})
    for _cypher, params in _all_run_calls(tx):
        assert "candidate" not in params
        for key in ("name", "email", "phone", "location"):
            assert key not in params, f"unexpected key {key!r} in {params!r}"


@pytest.mark.asyncio
async def test_resume_node_write_sets_only_total_years_experience_scalar() -> None:
    """Only ``total_years_experience`` from the parsed payload lands on the
    Resume node — no experience/education list, ever (R8: those would need
    Company/Institution nodes this module must not create)."""
    payload = _payload(total_years_experience=9)
    tx, _driver, _events = await _project(payload, {"python": "python"})
    resume_calls = [
        (cypher, params)
        for cypher, params in _all_run_calls(tx)
        if re.search(r"MERGE\s*\(\s*r\s*:\s*Resume", cypher, re.IGNORECASE)
    ]
    assert len(resume_calls) == 1
    _cypher, params = resume_calls[0]
    assert 9 in params.values()
    assert "experience" not in params
    assert "education" not in params


@pytest.mark.asyncio
async def test_no_experience_education_company_institution_writes_anywhere() -> None:
    """R8 (MED) unit-level pin, complementing the integration label-set
    sweep. Looks for the Cypher LABEL syntax specifically (``:Company`` /
    ``:Institution``), not a loose substring, to avoid false positives on
    unrelated words."""
    tx, _driver, _events = await _project(_payload(), {"python": "python"})
    cypher = _all_cypher(tx)
    for label in ("Company", "Institution"):
        assert f":{label}" not in cypher, f"unexpected {label} label"


@pytest.mark.asyncio
async def test_job_id_is_coalesced_with_the_existing_value() -> None:
    tx, _driver, _events = await _project(_payload(job_id=None), {"python": "python"})
    resume_cypher = next(
        cypher
        for cypher, _params in _all_run_calls(tx)
        if re.search(r"MERGE\s*\(\s*r\s*:\s*Resume", cypher, re.IGNORECASE)
    )
    assert re.search(
        r"coalesce\(\s*\$\w+\s*,\s*r\.job_id\s*\)", resume_cypher, re.IGNORECASE
    )


@pytest.mark.asyncio
async def test_status_and_updated_at_are_set_on_the_resume_node() -> None:
    tx, _driver, _events = await _project(_payload(), {"python": "python"})
    resume_cypher = next(
        cypher
        for cypher, _params in _all_run_calls(tx)
        if re.search(r"MERGE\s*\(\s*r\s*:\s*Resume", cypher, re.IGNORECASE)
    )
    assert "status" in resume_cypher
    assert "updated_at" in resume_cypher


# ── HAS_SKILL uses the RESOLVED canonical name ────────────────────────────


@pytest.mark.asyncio
async def test_has_skill_edge_uses_resolved_canonical_name_not_raw_name() -> None:
    payload = _payload(skills=[{"name": "Py", "years": 4, "last_used_year": 2026}])
    tx, _driver, _events = await _project(payload, {"Py": "python"})
    skill_calls = [p for c, p in _all_run_calls(tx) if "HAS_SKILL" in c]
    assert skill_calls
    assert any(p.get("cn") == "python" or "python" in p.values() for p in skill_calls)
    assert not any("Py" in p.values() for p in skill_calls)


@pytest.mark.asyncio
async def test_old_skill_edges_and_chunks_are_detached_before_recreation() -> None:
    """Idempotency at the write-tx level — a re-parse must not accumulate
    stale HAS_CHUNK/HAS_SKILL edges. Order: DELETE calls precede CREATE."""
    tx, _driver, _events = await _project(_payload(), {"python": "python"})
    calls = _all_run_calls(tx)
    delete_idxs = [i for i, (c, _p) in enumerate(calls) if "DELETE" in c.upper()]
    create_idxs = [
        i
        for i, (c, _p) in enumerate(calls)
        if "CREATE" in c.upper() and "ResumeChunk" in c
    ]
    assert delete_idxs, "expected at least one DETACH DELETE / DELETE"
    assert create_idxs
    assert max(delete_idxs) < min(create_idxs)


# ── Decision 3: resolution happens OUTSIDE the write transaction ─────────


@pytest.mark.asyncio
async def test_skills_are_resolved_before_the_write_transaction_opens() -> None:
    _tx, _driver, events = await _project(_payload(), {"python": "python"})
    assert events[0] == "resolve"
    assert "write_start" in events
    assert events.index("resolve") < events.index("write_start")


@pytest.mark.asyncio
async def test_write_transaction_callback_never_receives_llm_or_embedder() -> None:
    """Decision 3, architecturally: the callback handed to ``execute_write``
    must not have ``llm``/``embedder`` in its args/kwargs at all — so it
    CANNOT call ``chat_json``/``embed`` while the write transaction is open,
    not merely 'doesn't happen to' in this particular test run."""
    resume_id = uuid4()
    driver, tx, resolve_mock, _events = _fake_driver_and_tx({"python": "python"})
    llm = MagicMock(chat_json=AsyncMock(), name="THE_LLM")
    embedder = MagicMock(embed=AsyncMock(), name="THE_EMBEDDER")
    with patch(
        "src.worker.resume_tasks.skills_graph.resolve_canonical_names", resolve_mock
    ):
        await project_resume(
            driver,
            resume_id=resume_id,
            payload=_payload(),
            llm=llm,
            embedder=embedder,
        )
    session = driver.session.return_value.__aenter__.return_value
    for args, kwargs in zip(
        session._captured_write_args, session._captured_write_kwargs, strict=True
    ):
        assert llm not in args and llm not in kwargs.values()
        assert embedder not in args and embedder not in kwargs.values()
    llm.chat_json.assert_not_awaited()
    embedder.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_canonical_names_is_called_with_the_llm_and_embedder() -> None:
    """The resolution step (outside the write tx) is where llm/embedder
    actually get used."""
    resume_id = uuid4()
    driver, _tx, resolve_mock, _events = _fake_driver_and_tx({"python": "python"})
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock())
    with patch(
        "src.worker.resume_tasks.skills_graph.resolve_canonical_names", resolve_mock
    ):
        await project_resume(
            driver, resume_id=resume_id, payload=_payload(), llm=llm, embedder=embedder
        )
    resolve_mock.assert_awaited_once()
    assert resolve_mock.await_args.kwargs.get("llm") is llm
    assert resolve_mock.await_args.kwargs.get("embedder") is embedder


# ── Architectural: no Postgres dependency at all (Decision 1) ────────────


def test_project_resume_signature_has_no_postgres_connection_parameter() -> None:
    """Decision 1 supersedes the EXTRACTION_PLAN's earlier suggestion of
    reading a chunk-text preview from ``resumes.parsed`` — Neo4j gets NO
    chunk text at all, so this function must not even depend on a Postgres
    connection/pool to read one."""
    params = set(inspect.signature(project_resume).parameters)
    assert not params & {"conn", "pool", "pg_pool"}


def test_resume_projection_tx_signature_has_no_llm_or_embedder_parameter() -> None:
    """Architectural guarantee for decision 3: the write-tx function itself
    cannot even be called with an llm/embedder — the parameter doesn't
    exist."""
    params = set(inspect.signature(_resume_projection_tx).parameters)
    assert not params & {"llm", "embedder"}
