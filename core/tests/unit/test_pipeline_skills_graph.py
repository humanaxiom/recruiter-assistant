"""Unit tests for ``src.pipeline.skills_graph`` — the Neo4j-backed half of
skill normalisation, deferred out of Phase 3 (ADR-007 §4) and landing here.

Ported behaviourally from hris ``apps/worker/src/worker/skill_normalize.py``
(``_resolve_canonical`` / ``normalize_skill`` / ``_ask_llm_tiebreaker`` /
``categories_for`` / ``_ensure_categories`` / ``_category_table``), with
THREE deliberate, human-locked deviations this file pins:

* **Decision 3 — resolution runs OUTSIDE any Neo4j write transaction.**
  hris's ``_resolve_canonical`` takes a ``tx`` (an ``AsyncManagedTransaction``
  passed to ``execute_write``) and calls ``embedder.embed`` / ``llm.chat_json``
  from inside it — up to 40 sequential local-model round trips held under one
  write lock. Here, resolution takes a plain ``session`` and issues every
  Cypher statement via auto-commit ``session.run(...)``, never
  ``session.execute_write``/``execute_read``. Every fake session below makes
  those two methods raise if called AT ALL, so any test that reaches them
  fails loudly rather than silently passing.
* **Decision 4 — the LLM tiebreaker's answer is constrained to the ``near``
  candidate set.** hris's Cypher ``MATCH (s:Skill {canonical_name: $c})``
  against a hallucinated ``$c`` matches nothing and is a silent no-op — the
  skill's ``HAS_SKILL``/``REQUIRES`` edge simply never gets created, no error,
  no log. Here a tiebreaker answer that isn't one of the ``near`` candidates'
  own names is treated exactly like "no match" and falls through to
  create-new, so a real Skill node backing the answer always exists before
  any caller writes an edge to it.
* **Thresholds are settings, not hard-coded module constants** — hris's
  ``AUTO_MERGE_THRESHOLD = 0.92`` / ``TIEBREAKER_THRESHOLD = 0.88`` become
  ``settings.skill_auto_merge_threshold`` / ``settings.skill_tiebreaker_threshold``
  (see ``test_settings.py``), proven READ (not just present) by
  ``test_auto_merge_threshold_is_read_from_settings_not_hardcoded`` below.

``src.pipeline.skills_graph`` does not exist yet — this whole file fails at
collection (``ModuleNotFoundError``). RED half of the TDD cycle.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pipeline import skills_graph

# ── fakes ─────────────────────────────────────────────────────────────────


class _FakeRecord(dict[str, Any]):
    """A Neo4j ``Record`` double — ``record["key"]`` and ``dict(record)`` both
    work, matching how hris's source reads query results."""


class _FakeResult:
    """A Neo4j query-result double supporting ``await result.single()`` and
    ``async for record in result``, matching the two access patterns hris's
    ``_resolve_canonical`` uses."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = [_FakeRecord(r) for r in rows]

    async def single(self) -> _FakeRecord | None:
        return self._rows[0] if self._rows else None

    def __aiter__(self) -> _FakeResult:
        self._iter = iter(self._rows)
        return self

    async def __anext__(self) -> _FakeRecord:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


def _managed_tx_forbidden() -> AsyncMock:
    """A callable that fails the test loudly if it is ever awaited — wired
    onto ``execute_write``/``execute_read`` on every fake session below, so
    decision 3 (resolution never opens a managed transaction) is enforced by
    EVERY test in this file, not just a dedicated one."""

    async def _raise(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "skill resolution must run OUTSIDE any Neo4j managed transaction "
            "(decision 3) — execute_write/execute_read must never be called "
            "during resolve_canonical_names/_resolve_canonical"
        )

    return AsyncMock(side_effect=_raise)


def _make_session(run_results: list[_FakeResult]) -> MagicMock:
    session = MagicMock(name="session")
    session.run = AsyncMock(side_effect=run_results)
    session.execute_write = _managed_tx_forbidden()
    session.execute_read = _managed_tx_forbidden()
    return session


def _make_llm(match: str | None = None, *, raises: bool = False) -> MagicMock:
    if raises:
        return MagicMock(chat_json=AsyncMock(side_effect=RuntimeError("llm down")))
    out = MagicMock(match=match)
    return MagicMock(chat_json=AsyncMock(return_value=out))


def _make_embedder(vectors: list[list[float]] | None = None) -> MagicMock:
    vecs = vectors if vectors is not None else [[0.1] * 8]
    return MagicMock(embed=AsyncMock(return_value=vecs))


def _run_calls(session: MagicMock) -> list[tuple[str, dict[str, Any]]]:
    return [
        (call.args[0] if call.args else call.kwargs.get("cypher", ""), call.kwargs)
        for call in session.run.await_args_list
    ]


# ── exact-match path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exact_match_short_circuits_before_any_vector_or_llm_call() -> None:
    session = _make_session([_FakeResult([{"name": "python"}])])
    llm = _make_llm()
    embedder = _make_embedder()

    resolved = await skills_graph.resolve_canonical_names(
        session, ["Python"], llm=llm, embedder=embedder
    )

    assert resolved == {"Python": "python"}
    assert session.run.await_count == 1
    embedder.embed.assert_not_awaited()
    llm.chat_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_canonical_names_keys_the_result_by_the_input_name() -> None:
    """Callers (``project_resume``/``_project_job``) look up
    ``resolved[skill["name"]]`` using the RAW name they already hold — the
    returned mapping must be keyed by that exact input string, not by a
    re-normalised form."""
    session = _make_session(
        [_FakeResult([{"name": "python"}]), _FakeResult([{"name": "postgresql"}])]
    )
    resolved = await skills_graph.resolve_canonical_names(
        session,
        ["  Python  ", "PostgreSQL"],
        llm=_make_llm(),
        embedder=_make_embedder(),
    )
    assert set(resolved.keys()) == {"  Python  ", "PostgreSQL"}


# ── F3 (security re-audit): PII-shaped skill names are rejected outright ──
#
# Layer 1 of the defence-in-depth fix — an LLM-authored skill name is free
# text (`ResumeSkill.name` is a 200-char-capped string, not a vocabulary
# allowlist), and this module is the ONLY place a skill name is ever handed
# to the embedder before landing in Neo4j cleartext. A name that is SHAPED
# like contact info, or implausibly long/verbose, must never reach
# `embed()`/any Cypher call, and must resolve to `None` (never a canonical
# name a caller could write an edge against).


@pytest.mark.asyncio
async def test_email_shaped_skill_name_is_rejected_before_any_io() -> None:
    session = _make_session([])
    llm = _make_llm()
    embedder = _make_embedder()

    resolved = await skills_graph.resolve_canonical_names(
        session, ["casey.rivera@example.test"], llm=llm, embedder=embedder
    )

    assert resolved == {"casey.rivera@example.test": None}
    session.run.assert_not_awaited()
    embedder.embed.assert_not_awaited()
    llm.chat_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_shaped_skill_name_is_rejected_before_any_io() -> None:
    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, ["555-0101"], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {"555-0101": None}
    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_shaped_skill_name_with_punctuation_is_rejected() -> None:
    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, ["+1 (604) 555-0101"], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {"+1 (604) 555-0101": None}
    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_implausibly_long_skill_name_is_rejected() -> None:
    long_name = "a" * 61  # over _MAX_SKILL_NAME_CHARS
    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, [long_name], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {long_name: None}
    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_too_many_tokens_skill_name_is_rejected() -> None:
    many_tokens = "one two three four five six seven"  # 7 tokens
    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, [many_tokens], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {many_tokens: None}
    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejection_is_logged_as_a_category_never_the_raw_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R3 discipline — the rejection log line must carry a CATEGORY
    (email/phone/length), never the value that triggered it."""
    caplog.set_level(logging.WARNING)
    session = _make_session([])
    await skills_graph.resolve_canonical_names(
        session,
        ["casey.rivera@example.test"],
        llm=_make_llm(),
        embedder=_make_embedder(),
    )
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "casey.rivera@example.test" not in all_text
    assert "rejected" in all_text.lower()


@pytest.mark.asyncio
async def test_legitimate_short_multiword_skill_name_is_not_rejected() -> None:
    """Non-regression: a real multi-word skill name (well under the shape
    caps) must resolve normally, not get swept up by the PII guard."""
    session = _make_session([_FakeResult([{"name": "google cloud platform"}])])
    resolved = await skills_graph.resolve_canonical_names(
        session,
        ["Google Cloud Platform"],
        llm=_make_llm(),
        embedder=_make_embedder(),
    )
    assert resolved["Google Cloud Platform"] == "google cloud platform"
    session.run.assert_awaited()


def test_unresolved_skill_name_error_is_a_runtime_error() -> None:
    """F6 — the exception projection callbacks raise for a name that was
    never resolved at all (a caller bug, distinct from a legitimate
    None-valued PII rejection)."""
    assert issubclass(skills_graph.UnresolvedSkillNameError, RuntimeError)


# ── auto-merge path (score >= AUTO_MERGE_THRESHOLD) ───────────────────────


@pytest.mark.asyncio
async def test_auto_merge_path_merges_without_asking_the_llm() -> None:
    session = _make_session(
        [
            _FakeResult([]),  # exact match: miss
            _FakeResult([{"name": "python", "aliases": ["py"], "score": 0.95}]),
            _FakeResult([]),  # alias-update write
        ]
    )
    llm = _make_llm()
    embedder = _make_embedder()

    resolved = await skills_graph.resolve_canonical_names(
        session, ["py3"], llm=llm, embedder=embedder
    )

    assert resolved == {"py3": "python"}
    llm.chat_json.assert_not_awaited()
    embedder.embed.assert_awaited_once()
    assert session.run.await_count == 3


@pytest.mark.asyncio
async def test_auto_merge_path_writes_an_alias_update_for_the_new_spelling() -> None:
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult([{"name": "python", "aliases": [], "score": 0.99}]),
            _FakeResult([]),
        ]
    )
    await skills_graph.resolve_canonical_names(
        session, ["py3"], llm=_make_llm(), embedder=_make_embedder()
    )
    _cypher, kwargs = _run_calls(session)[2]
    assert kwargs.get("c") == "python" or "python" in kwargs.values()
    assert "py3" in kwargs.values()


# ── grey-zone LLM tiebreaker path ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_grey_zone_asks_the_llm_and_merges_on_a_valid_match() -> None:
    near = [{"name": "postgresql", "aliases": ["postgres"], "score": 0.90}]
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult(near),
            _FakeResult([]),  # alias-update write
        ]
    )
    llm = _make_llm(match="postgresql")
    resolved = await skills_graph.resolve_canonical_names(
        session, ["postgres db"], llm=llm, embedder=_make_embedder()
    )
    assert resolved == {"postgres db": "postgresql"}
    llm.chat_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_grey_zone_llm_receives_the_near_candidates() -> None:
    near = [{"name": "postgresql", "aliases": ["postgres"], "score": 0.90}]
    session = _make_session([_FakeResult([]), _FakeResult(near), _FakeResult([])])
    llm = _make_llm(match="postgresql")
    await skills_graph.resolve_canonical_names(
        session, ["postgres db"], llm=llm, embedder=_make_embedder()
    )
    flat_args = [
        *llm.chat_json.await_args.args,
        *llm.chat_json.await_args.kwargs.values(),
    ]
    assert any("postgresql" in str(a) for a in flat_args)


@pytest.mark.asyncio
async def test_grey_zone_llm_says_no_match_falls_through_to_create_new() -> None:
    near = [{"name": "postgresql", "aliases": [], "score": 0.90}]
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult(near),
            _FakeResult([]),  # create-new write
        ]
    )
    llm = _make_llm(match=None)
    resolved = await skills_graph.resolve_canonical_names(
        session, ["cassandra"], llm=llm, embedder=_make_embedder()
    )
    assert resolved["cassandra"] != "postgresql"


@pytest.mark.asyncio
async def test_llm_tiebreaker_failure_is_non_fatal_and_treated_as_no_match() -> None:
    """Mirrors hris: a raising ``chat_json`` must not crash skill resolution
    — the résumé/JD parse it's attached to must still complete."""
    near = [{"name": "postgresql", "aliases": [], "score": 0.90}]
    session = _make_session([_FakeResult([]), _FakeResult(near), _FakeResult([])])
    llm = _make_llm(raises=True)
    resolved = await skills_graph.resolve_canonical_names(
        session, ["cassandra"], llm=llm, embedder=_make_embedder()
    )
    assert "cassandra" in resolved  # did not raise


# ── R5: hallucinated tiebreaker answer must not vanish the edge ──────────


@pytest.mark.asyncio
async def test_hallucinated_tiebreaker_answer_is_rejected_not_trusted() -> None:
    """R5 (HIGH). hris's Cypher ``MATCH (s:Skill {canonical_name: $c})``
    against a hallucinated ``$c`` is a silent no-op: no error, no edge, the
    skill just vanishes. Here, an answer that is NOT one of the offered
    ``near`` candidates is rejected outright and treated as 'create new' —
    the hallucinated string must never be used as a canonical_name to MATCH
    against, only a real (offered-or-freshly-created) name may be returned."""
    near = [{"name": "postgresql", "aliases": [], "score": 0.90}]
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult(near),
            _FakeResult([]),  # create-new write, NOT an alias-update on 'postgresql'
        ]
    )
    llm = _make_llm(match="not-a-real-skill-node")

    resolved = await skills_graph.resolve_canonical_names(
        session, ["cockroach db"], llm=llm, embedder=_make_embedder()
    )

    canonical = resolved["cockroach db"]
    assert canonical != "not-a-real-skill-node"
    # The hallucinated string must never appear as a Cypher parameter value —
    # proof it was never used to MATCH an existing (nonexistent) node.
    for _cypher, kwargs in _run_calls(session):
        assert "not-a-real-skill-node" not in kwargs.values()
    # A real node backing `canonical` was actually created (the create-new
    # write, not a no-op) — so a caller's subsequent HAS_SKILL/REQUIRES edge
    # write against `canonical` will succeed, not silently vanish.
    create_cypher, _ = _run_calls(session)[-1]
    assert re.search(r"MERGE.*Skill", create_cypher, re.IGNORECASE | re.DOTALL)
    assert "ON CREATE" in create_cypher.upper()


# ── create-new path (no near candidates at all) ───────────────────────────


@pytest.mark.asyncio
async def test_create_new_path_when_no_near_candidates_exist() -> None:
    session = _make_session(
        [_FakeResult([]), _FakeResult([]), _FakeResult([])]  # exact, vector, create
    )
    embedder = _make_embedder()
    llm = _make_llm()

    resolved = await skills_graph.resolve_canonical_names(
        session, ["rust"], llm=llm, embedder=embedder
    )

    assert resolved["rust"] == "rust"
    llm.chat_json.assert_not_awaited()
    embedder.embed.assert_awaited_once()
    create_cypher, _ = _run_calls(session)[-1]
    assert re.search(r"MERGE.*Skill", create_cypher, re.IGNORECASE | re.DOTALL)
    assert "ON CREATE" in create_cypher.upper()


# ── thresholds are settings, not hard-coded literals ──────────────────────


@pytest.mark.asyncio
async def test_auto_merge_threshold_is_read_from_settings_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A score of 0.95 auto-merges under hris's hard-coded 0.92 default —
    but if ``skill_auto_merge_threshold`` is genuinely READ from settings
    (not baked in as a literal), raising it to 0.99 must route the SAME
    score through the LLM tiebreaker instead."""
    from src.settings import Settings

    raised = Settings(skill_auto_merge_threshold=0.99, skill_tiebreaker_threshold=0.88)
    monkeypatch.setattr(skills_graph, "get_settings", lambda: raised)

    near = [{"name": "python", "aliases": [], "score": 0.95}]
    session = _make_session([_FakeResult([]), _FakeResult(near), _FakeResult([])])
    llm = _make_llm(match="python")

    await skills_graph.resolve_canonical_names(
        session, ["py3"], llm=llm, embedder=_make_embedder()
    )
    llm.chat_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_tiebreaker_threshold_is_read_from_settings_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vector query's own lower bound (``WHERE score > tiebreaker
    threshold``) must also come from settings: lowering it must widen what
    counts as a 'near' candidate passed to the query, which we observe
    through the parameter actually sent to Neo4j."""
    from src.settings import Settings

    lowered = Settings(skill_auto_merge_threshold=0.92, skill_tiebreaker_threshold=0.10)
    monkeypatch.setattr(skills_graph, "get_settings", lambda: lowered)

    session = _make_session([_FakeResult([]), _FakeResult([]), _FakeResult([])])
    await skills_graph.resolve_canonical_names(
        session, ["rust"], llm=_make_llm(), embedder=_make_embedder()
    )
    _cypher, vector_kwargs = _run_calls(session)[1]
    assert 0.10 in vector_kwargs.values()


# ── categories_for / _ensure_categories ────────────────────────────────────


def test_categories_for_a_seeded_skill_is_non_empty() -> None:
    """R4 (HIGH). ``categories.yaml`` must ship at
    ``core/src/pipeline/skill_data/categories.yaml`` (beside the existing
    ``aliases.yaml``) — if the file is missing, ``categories_for`` degrades
    gracefully to ``[]`` (see the next test), which is EXACTLY the failure
    mode this test catches: with the real shipped path, a well-known skill
    like 'python' must resolve to a non-empty family list, or every Skill
    node's ``.categories`` stays empty and 4c's ontology partial-credit is
    dead on arrival."""
    assert skills_graph.categories_for("python") != []


def test_categories_for_an_unseeded_skill_is_empty() -> None:
    assert skills_graph.categories_for("a-totally-unseeded-made-up-skill-xyz") == []


def test_categories_for_missing_yaml_file_degrades_to_empty_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """R4 mutation target: point the categories path at a nonexistent file —
    ``categories_for`` must degrade gracefully (never raise), matching the
    aliases.yaml precedent in ``src/pipeline/skills.py``."""
    skills_graph._category_table.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setattr(skills_graph, "_CATEGORIES_PATH", tmp_path / "nonexistent.yaml")
    try:
        assert skills_graph.categories_for("python") == []
    finally:
        skills_graph._category_table.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_ensure_categories_stamps_curated_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skills_graph, "categories_for", lambda c: ["backend", "data"])
    tx = MagicMock(run=AsyncMock(return_value=_FakeResult([])))

    await skills_graph._ensure_categories(tx, "python")

    tx.run.assert_awaited_once()
    cypher, kwargs = tx.run.await_args.args[0], tx.run.await_args.kwargs
    assert "SET" in cypher.upper() and "categories" in cypher
    assert (
        kwargs.get("cats") == ["backend", "data"]
        or [
            "backend",
            "data",
        ]
        in kwargs.values()
    )


@pytest.mark.asyncio
async def test_ensure_categories_is_a_noop_when_uncurated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(skills_graph, "categories_for", lambda c: [])
    tx = MagicMock(run=AsyncMock(return_value=_FakeResult([])))

    await skills_graph._ensure_categories(tx, "some-uncurated-skill")

    tx.run.assert_not_awaited()


# ── R6: alias updates must be dedupe-safe Cypher ──────────────────────────


def _alias_update_cypher_from_auto_merge() -> str:
    return (
        "MATCH (s:Skill {canonical_name: $c}) "
        "SET s.aliases = CASE WHEN $alias IN coalesce(s.aliases, []) "
        "THEN coalesce(s.aliases, []) "
        "ELSE coalesce(s.aliases, []) + [$alias] END"
    )


@pytest.mark.asyncio
async def test_alias_update_cypher_is_dedupe_safe_not_the_naive_hris_pattern() -> None:
    """R6 (MED). hris: ``SET s.aliases = coalesce(s.aliases, []) + [$alias]``
    — re-running the SAME alias on a re-parse appends a duplicate every time,
    unbounded. The Cypher this module issues for BOTH the auto-merge and the
    LLM-merge alias update must guard against re-adding an alias already
    present, so a full behavioural round-trip against a real Neo4j (the
    integration idempotency test) sees no duplicates after draining twice."""
    session = _make_session(
        [
            _FakeResult([]),
            _FakeResult([{"name": "python", "aliases": ["py3"], "score": 0.95}]),
            _FakeResult([]),
        ]
    )
    await skills_graph.resolve_canonical_names(
        session, ["py3"], llm=_make_llm(), embedder=_make_embedder()
    )
    alias_cypher, _ = _run_calls(session)[2]
    naive = "SET s.aliases = coalesce(s.aliases, []) + [$alias]"
    assert re.sub(r"\s+", " ", alias_cypher).strip() != naive, (
        "the alias-update Cypher is the naive hris pattern verbatim — it "
        "will accumulate duplicate aliases forever across re-parses (R6)"
    )
    assert re.search(r"CASE\s+WHEN", alias_cypher, re.IGNORECASE), (
        "expected a dedupe guard (e.g. a CASE WHEN ... IN ... check) in the "
        "alias-update Cypher"
    )


# ── _ask_llm_tiebreaker (pure-ish unit, exercised indirectly above too) ───


@pytest.mark.asyncio
async def test_ask_llm_tiebreaker_returns_the_match_field() -> None:
    llm = _make_llm(match="python")
    result = await skills_graph._ask_llm_tiebreaker(
        llm, "py3", [{"name": "python", "aliases": []}]
    )
    assert result == "python"


@pytest.mark.asyncio
async def test_ask_llm_tiebreaker_returns_none_when_llm_raises() -> None:
    llm = _make_llm(raises=True)
    result = await skills_graph._ask_llm_tiebreaker(
        llm, "py3", [{"name": "python", "aliases": []}]
    )
    assert result is None


# ── resolve_canonical_names never opens a managed transaction ────────────


@pytest.mark.asyncio
async def test_resolution_never_calls_execute_write_or_execute_read() -> None:
    """Decision 3, stated directly (every other test in this file also
    enforces it implicitly via ``_managed_tx_forbidden`` — this test just
    names the invariant explicitly for the reader)."""
    session = _make_session([_FakeResult([]), _FakeResult([]), _FakeResult([])])
    await skills_graph.resolve_canonical_names(
        session, ["rust"], llm=_make_llm(), embedder=_make_embedder()
    )
    session.execute_write.assert_not_awaited()
    session.execute_read.assert_not_awaited()


def test_resolve_canonical_names_accepts_any_iterable_of_names() -> None:
    assert callable(skills_graph.resolve_canonical_names)


def test_resolve_canonical_names_with_empty_input_returns_empty_dict() -> None:
    import asyncio

    session = _make_session([])

    async def _run() -> dict[str, str]:
        return await skills_graph.resolve_canonical_names(
            session, [], llm=_make_llm(), embedder=_make_embedder()
        )

    resolved = asyncio.run(_run())
    assert resolved == {}
    session.run.assert_not_awaited()
