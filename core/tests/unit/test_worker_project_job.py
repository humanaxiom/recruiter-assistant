"""Unit tests for ``src.worker.tasks._project_job`` / ``_job_projection_tx``
— the JD half of Phase 4b's graph projection.

Mirrors ``test_worker_project_resume.py``'s fake-tx-capture approach. Ported
behaviourally from hris ``apps/worker/src/worker/tasks.py::_project_job`` /
``_job_projection_tx``, with the same Decision-3 (resolve outside the write
transaction) and R8 (pinned label set — no ``Company``/``Institution``) pins
applied to the JD side.

``src.worker.tasks._project_job`` does not exist yet — this whole file fails
at collection (``ImportError``). RED half of the TDD cycle.
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.worker.tasks import _job_projection_tx, _project_job

# All three skills used across the fixture payload below, pre-resolved to
# themselves (canonical == raw) unless a test overrides it.
_RESOLVED = {"python": "python", "postgresql": "postgresql", "kubernetes": "kubernetes"}

# ── fakes (same shape as test_worker_project_resume.py) ──────────────────


class _RecordingTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, cypher: str, **params: Any) -> list[Any]:
        self.calls.append((cypher, params))
        return []


def _acm(return_value: Any) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_driver_and_tx(
    resolve_result: dict[str, str],
) -> tuple[MagicMock, _RecordingTx, AsyncMock, list[str]]:
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


def _payload(
    *,
    required: list[dict[str, Any]] | None = None,
    nice_to_have: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    required = (
        required
        if required is not None
        else [
            {"name": "python", "min_years": 4},
            {"name": "postgresql", "min_years": 3},
        ]
    )
    nice_to_have = (
        nice_to_have if nice_to_have is not None else [{"name": "kubernetes"}]
    )
    return {
        "embedding": [0.1] * 8,
        "extracted": {
            "title": "Senior Backend Engineer",
            "required_skills": required,
            "nice_to_have_skills": nice_to_have,
            "min_years_experience": 4,
            "education": {"min_level": "bachelors", "fields": []},
            "location": None,
            "remote_policy": None,
            "responsibilities": [],
        },
        "prompt_version": "jd_extract_v1",
    }


async def _project(
    payload: dict[str, Any], resolve_result: dict[str, str]
) -> tuple[_RecordingTx, MagicMock, list[str]]:
    job_id = uuid4()
    driver, tx, resolve_mock, events = _fake_driver_and_tx(resolve_result)
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock())
    with patch("src.worker.tasks.skills_graph.resolve_canonical_names", resolve_mock):
        await _project_job(driver, job_id, payload, llm=llm, embedder=embedder)
    return tx, driver, events


# ── Job node + REQUIRES/NICE_TO_HAVE shape ────────────────────────────────


@pytest.mark.asyncio
async def test_job_node_merge_sets_title_and_summary_embedding() -> None:
    tx, _driver, _events = await _project(_payload(), _RESOLVED)
    job_calls = [
        (cypher, params)
        for cypher, params in tx.calls
        if re.search(r"MERGE\s*\(\s*j\s*:\s*Job", cypher, re.IGNORECASE)
    ]
    assert len(job_calls) == 1
    _cypher, params = job_calls[0]
    assert "Senior Backend Engineer" in params.values()
    assert [0.1] * 8 in params.values()


@pytest.mark.asyncio
async def test_required_skill_gets_a_requires_edge_with_is_must_have_true() -> None:
    tx, _driver, _events = await _project(
        _payload(required=[{"name": "python", "min_years": 4}], nice_to_have=[]),
        {"python": "python"},
    )
    requires_calls = [(c, p) for c, p in tx.calls if "REQUIRES" in c]
    assert requires_calls
    _cypher, params = requires_calls[0]
    assert True in params.values()
    assert 4 in params.values()
    assert "python" in params.values()


@pytest.mark.asyncio
async def test_nice_to_have_skill_gets_a_nice_to_have_edge_not_requires() -> None:
    tx, _driver, _events = await _project(
        _payload(required=[], nice_to_have=[{"name": "kubernetes"}]),
        {"kubernetes": "kubernetes"},
    )
    nice_calls = [(c, p) for c, p in tx.calls if "NICE_TO_HAVE" in c]
    requires_calls = [(c, p) for c, p in tx.calls if re.search(r"\bREQUIRES\b", c)]
    assert nice_calls
    assert not requires_calls


@pytest.mark.asyncio
async def test_requires_edge_uses_resolved_canonical_name_not_raw_name() -> None:
    tx, _driver, _events = await _project(
        _payload(required=[{"name": "Py", "min_years": 2}], nice_to_have=[]),
        {"Py": "python"},
    )
    requires_calls = [(c, p) for c, p in tx.calls if "REQUIRES" in c]
    assert any("python" in p.values() for _c, p in requires_calls)
    assert not any("Py" in p.values() for _c, p in requires_calls)


@pytest.mark.asyncio
async def test_old_requires_and_nice_to_have_edges_are_dropped_before_recreation() -> (
    None
):
    tx, _driver, _events = await _project(_payload(), _RESOLVED)
    delete_idxs = [i for i, (c, _p) in enumerate(tx.calls) if "DELETE" in c.upper()]
    requires_idxs = [
        i
        for i, (c, _p) in enumerate(tx.calls)
        if "REQUIRES" in c and "MERGE" in c.upper()
    ]
    assert delete_idxs
    assert requires_idxs
    assert max(delete_idxs) < min(requires_idxs)


# ── R8: pinned label set (no Company/Institution) ─────────────────────────


@pytest.mark.asyncio
async def test_no_company_or_institution_writes_on_the_job_side() -> None:
    tx, _driver, _events = await _project(_payload(), _RESOLVED)
    cypher = "\n".join(c for c, _ in tx.calls)
    for label in ("Company", "Institution"):
        assert f":{label}" not in cypher


# ── Decision 3: resolution outside the write transaction ─────────────────


@pytest.mark.asyncio
async def test_job_skills_are_resolved_before_the_write_transaction_opens() -> None:
    _tx, _driver, events = await _project(_payload(), _RESOLVED)
    assert events[0] == "resolve"
    assert events.index("resolve") < events.index("write_start")


@pytest.mark.asyncio
async def test_job_write_transaction_callback_never_receives_llm_or_embedder() -> None:
    driver, tx, resolve_mock, _events = _fake_driver_and_tx(_RESOLVED)
    llm = MagicMock(chat_json=AsyncMock())
    embedder = MagicMock(embed=AsyncMock())
    with patch("src.worker.tasks.skills_graph.resolve_canonical_names", resolve_mock):
        await _project_job(driver, str(uuid4()), _payload(), llm=llm, embedder=embedder)
    session = driver.session.return_value.__aenter__.return_value
    for args, kwargs in zip(
        session._captured_write_args, session._captured_write_kwargs, strict=True
    ):
        assert llm not in args and llm not in kwargs.values()
        assert embedder not in args and embedder not in kwargs.values()


def test_job_projection_tx_signature_has_no_llm_or_embedder_parameter() -> None:
    params = set(inspect.signature(_job_projection_tx).parameters)
    assert not params & {"llm", "embedder"}
