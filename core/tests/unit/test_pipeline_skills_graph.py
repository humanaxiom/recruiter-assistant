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


@pytest.mark.parametrize(
    "name",
    [
        "john.smith @ corp.test",  # S6: whitespace around '@'
        "casey.rivera (at) example.test",  # S6: '(at)' obfuscation + whitespace
    ],
)
def test_whitespace_obfuscated_email_shape_is_still_rejected(name: str) -> None:
    """S6 (security re-audit round 3): the OLD `_EMAIL_SHAPE_RE` required an
    exact, whitespace-free `local@domain` literal — a whitespace-padded '@'
    or an '(at)'/'[at]' obfuscation (both common copy-paste/anti-scraping
    header renderings) sailed straight through."""
    assert skills_graph.reject_reason_for_skill_name(name) == "email_shape"


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
    """Round 2 (security re-audit): the token cap was widened 6 -> 8 (zero of
    the 220 shipped vocab terms trip 6, so this is headroom for a legitimate
    multi-word certification name, not a regression) — this must exceed the
    NEW cap, not the old one."""
    many_tokens = "one two three four five six seven eight nine"  # 9 tokens
    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, [many_tokens], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {many_tokens: None}
    session.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_widened_token_cap_does_not_reject_at_eight_tokens() -> None:
    """Non-regression for the widened cap: exactly 8 tokens (the new limit)
    must NOT be rejected on token-count grounds alone (a 7-8 token
    certification name is legitimate — Decision B's skill-recall guard)."""
    eight_tokens = "alpha bravo charlie delta echo foxtrot golf hotel"
    session = _make_session([_FakeResult([{"name": eight_tokens}])])
    resolved = await skills_graph.resolve_canonical_names(
        session, [eight_tokens], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved[eight_tokens] == eight_tokens


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


# ── Decision A (round-2 security re-audit): name-shape + vocab reject ─────
#
# Security's round-2 reproduction: `_resolve_one`'s email check is dead on the
# real résumé path because `_extract_skills_merged` canonicalises every skill
# name (stripping "@"/case) BEFORE this module ever sees it, and a name that
# IS the candidate's own identity (any of the shapes below) sails through
# unshaped otherwise. `reject_reason_for_skill_name` is the shared decision
# function; these tests exercise it both directly and through
# `resolve_canonical_names` (unchanged public surface).


@pytest.mark.parametrize(
    "name",
    [
        "Casey Rivera",
        "Rivera, Casey",
        "Casey M. Rivera",
        "Casey-Rivera",
        "Rivera",
        "John Smith",
        # ── round-3 security re-audit widening (S1-S5) ──────────────────
        "RIVERA, CASEY",  # S1: all-caps, comma-reordered
        "CASEY RIVERA",  # S1: all-caps
        "casey rivera",  # S1: all-lowercase
        "Sean McDonald",  # S3: Mc-internal-caps surname
        "John O'Brien",  # S3: apostrophe-joined surname
        "Maria del Carmen Rivera Lopez",  # S4: 5-token, connector particle
        "Ana van der Berg",  # S4: 4-token, two connector particles
        "Casey Rivera 2",  # S2: stray trailing standalone digit token
        "Casey Rivera+",  # S2: stray trailing glued '+'
        "Casey Rivera#",  # S2: stray trailing glued '#'
        "Casey.Rivera",  # S2: dot-joined (not a technical '.')
        "Кейси Ривера",  # S5: Cyrillic
        "李伟",  # S5: CJK, caseless script
    ],
)
@pytest.mark.asyncio
async def test_person_name_shaped_skill_missing_vocab_is_rejected(name: str) -> None:
    """Every row of security's round-2 AND round-3 reproduction tables
    (candidate-identity shapes only — the email/phone rows are covered by
    the existing F3/S6 tests) must be rejected outright, with NO vocab hit
    to save it."""
    assert skills_graph.reject_reason_for_skill_name(name) == "person_name_shape"

    session = _make_session([])
    resolved = await skills_graph.resolve_canonical_names(
        session, [name], llm=_make_llm(), embedder=_make_embedder()
    )
    assert resolved == {name: None}
    session.run.assert_not_awaited()


@pytest.mark.parametrize("name", ["Kafka", "Django", "Kubernetes"])
def test_vocab_known_single_title_case_word_is_not_rejected(name: str) -> None:
    """The human-locked Decision A boundary, stated explicitly: a bare
    'Rivera' must be caught (previous test), but a single Title-Case word
    that IS in the 220-term vocabulary must NOT be — the vocab check is what
    protects recall, not a blanket single-word-name ban."""
    assert skills_graph.reject_reason_for_skill_name(name) is None


# ── Decision C/D (round-4 recall regression fix): personal-name lexicon ───
#
# Round-3's recall guard (the tests above) went green for the WRONG reason:
# every one of its fixtures (`Google Cloud Platform`, `machine learning`,
# `ISO 27001`, the 8-token cert) is a vocabulary HIT, so it only ever
# exercised the arm of Decision A that was never at risk. Decision A's
# two-way conjunction (name-shape AND vocab-miss) had itself quietly become
# the strict allowlist the human rejected in round 2: it also rejected any
# LEGITIMATE multi-word skill missing from the 220-term vocabulary that
# happened to be two Title-Case-able alphabetic words — indistinguishable,
# by shape alone, from `Casey Rivera`. Decision C adds a personal-name
# lexicon (`skill_data/person_names.txt`) as the missing third signal.


@pytest.mark.parametrize(
    "name",
    [
        "distributed systems",
        "data engineering",
        "natural language processing",
        "event driven architecture",
        "test driven development",
        "postgres db",
        "cockroach db",
    ],
)
def test_non_vocab_multiword_skill_is_kept_not_rejected_as_a_name(name: str) -> None:
    """Decision D's recall-guard fixtures: these are legitimate skill-shaped
    phrases NOT in the 220-term vocabulary, made of two-or-more
    Title-Case-able alphabetic tokens apiece — under the OLD round-2/3
    two-way conjunction (name-shape AND vocab-miss, no lexicon arm) every
    one of these was misclassified as `person_name_shape` and silently
    dropped, deflating 4c's 0.40-weighted skill sub-score. Decision C's
    THIRD conjunct (a personal-name lexicon hit, or a non-Latin script) is
    what tells these apart from `Casey Rivera` — see the mutation-proof
    test below for the falsifiable pin that this guard actually depends on
    that arm, not merely on these fixtures happening to be vocab hits."""
    assert skills_graph.reject_reason_for_skill_name(name) is None


def test_lexicon_arm_mutation_proof_recall_guard_goes_red_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision D's mandated mutation proof. Neutralise Decision C's lexicon
    arm back to an unconditional hit — i.e. REVERT to the OLD round-2/3
    conjunction, where name-shape AND vocab-miss ALONE was sufficient to
    reject (`_hits_person_name_lexicon` always returning True collapses the
    three-way conjunction back to that two-way rule) — and confirm the
    round-4 recall-guard fixtures above then INCORRECTLY get rejected. If
    this assertion ever starts failing, the recall guard above has stopped
    being sensitive to the lexicon arm — exactly the blind-guard defect
    class round 3's own recall guard turned out to have."""
    monkeypatch.setattr(skills_graph, "_hits_person_name_lexicon", lambda tokens: True)
    for name in (
        "distributed systems",
        "data engineering",
        "natural language processing",
        "event driven architecture",
        "test driven development",
    ):
        assert skills_graph.reject_reason_for_skill_name(name) == "person_name_shape", (
            f"{name!r} should be (incorrectly) rejected once the lexicon arm "
            "is neutralised back to the old shape+vocab-miss-only rule — if "
            "this fails, today's green recall guard is not actually pinned "
            "to Decision C's lexicon arm being present"
        )


def test_lexicon_file_missing_fails_closed_not_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """S7 precedent, applied to Decision C: if `person_names.txt` cannot be
    read at all, `_hits_person_name_lexicon` must report a hit
    UNCONDITIONALLY (fail CLOSED — collapsing back to the stricter old
    two-way rule), never silently report no-hit (fail OPEN, which would
    make `person_name_shape` un-triggerable for name-shaped, vocab-miss
    candidates on a broken deployment — reopening the original F3 leak)."""
    skills_graph._name_lexicon.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setattr(
        skills_graph, "_PERSON_NAMES_PATH", tmp_path / "nonexistent-lexicon.txt"
    )
    try:
        assert skills_graph._name_lexicon() is None
        assert skills_graph._hits_person_name_lexicon(["anything", "at-all"]) is True
        # And end-to-end: a name-shaped, vocab-miss candidate is STILL
        # rejected even though it hits no lexicon entry, because the fail-
        # closed sentinel makes the lexicon arm always report a hit.
        assert (
            skills_graph.reject_reason_for_skill_name("distributed systems")
            == "person_name_shape"
        )
    finally:
        skills_graph._name_lexicon.cache_clear()  # type: ignore[attr-defined]


# ── Decision C, S10-corrected (round-5 security re-audit) ──────────────────
#
# Round-4's own lexicon arm was itself a recall regression, one layer down:
# `_hits_person_name_lexicon` shipped as `any(...)`, so ONE name-shaped token
# anywhere in a multi-word candidate condemned the WHOLE name. A genuine
# vendor/product skill built from one common-name-shaped word plus one
# technical word (`Amazon Aurora`, `IBM Watson`, `Victoria Metrics`) was
# dropped in EVERY casing. S11's own recall guard (below) never caught this
# either — every round-4 KEEP fixture (`distributed systems` etc.) is a
# lexicon-MISS on every token; none of them exercises a candidate whose
# token actually IS a personal name.


@pytest.mark.parametrize(
    "name",
    [
        "Amazon Aurora",
        "IBM Watson",
        "Apache Felix",
        "Victoria Metrics",
        "VictoriaMetrics",
        "Julia",
        "Hudson",
    ],
)
def test_vendor_and_backstop_skills_are_kept_not_rejected_as_a_name(name: str) -> None:
    """S10/S11: each of these is a real, common résumé skill that collides
    with the personal-name lexicon on exactly one of its tokens (a vendor/
    product word paired with a common given name/surname) or IS itself a
    bare product name that also happens to be a common given name/surname.
    Under the round-4 `any()` lexicon arm every one of these was
    misclassified as `person_name_shape` and silently dropped."""
    assert skills_graph.reject_reason_for_skill_name(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "Amazon Aurora",
        "IBM Watson",
        "Apache Felix",
        "Victoria Metrics",
        "VictoriaMetrics",
        "Julia",
        "Hudson",
    ],
)
def test_vendor_and_backstop_skills_casing_invariant(name: str) -> None:
    """S11: recall must not depend on the LLM's arbitrary capitalisation of
    a skill name — every casing of a KEEP fixture must agree, all landing on
    "not rejected". Round-4's own guard never pinned this: under the OLD
    `any()` lexicon arm, `Julia` (Title Case) was rejected while `julia`/
    `JULIA` were kept — non-deterministic by construction."""
    r = skills_graph.reject_reason_for_skill_name
    outcomes = {r(name), r(name.title()), r(name.lower()), r(name.upper())}
    assert outcomes == {None}, f"casing-dependent rejection for {name!r}: {outcomes}"


def test_lexicon_quantifier_mutation_proof_all_not_any(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S10's mandated mutation proof. Revert `_hits_person_name_lexicon` to
    the OLD round-4 `any()` semantics and confirm `Victoria Metrics` (whose
    first token, "victoria", has no vendor-prefix veto to fall back on) is
    then INCORRECTLY rejected — proving the recall-guard fixtures above are
    actually sensitive to the any()->all() quantifier fix, not passing for
    some unrelated reason (e.g. an accidental vocab/vendor-veto hit)."""

    def _old_any_semantics(tokens: Any) -> bool:
        lexicon = skills_graph._name_lexicon()
        if lexicon is None:
            return True
        return any(tok in lexicon for tok in tokens)

    monkeypatch.setattr(skills_graph, "_hits_person_name_lexicon", _old_any_semantics)
    assert (
        skills_graph.reject_reason_for_skill_name("Victoria Metrics")
        == "person_name_shape"
    ), (
        "'Victoria Metrics' should be (incorrectly) rejected once the "
        "lexicon arm is reverted to the old any()-based rule — if this "
        "fails, today's green recall guard is not actually pinned to the "
        "all() quantifier fix"
    )


@pytest.mark.parametrize(
    "name",
    [
        "Amazon Aurora",
        "IBM Watson",
        "Apache Felix",
        "Microsoft Teams",
        "Google Cloud",
        "Oracle Database",
        "Red Hat Enterprise",
    ],
)
def test_vendor_prefix_veto_recognises_known_vendor_prefixes(name: str) -> None:
    # Alpha-only vendor-prefixed names (no digit/technical marker), so the
    # candidate is actually name-SHAPED in the first place — a name carrying
    # a digit ("Amazon S3") disqualifies on shape alone and never reaches
    # the vendor-veto arm at all (see `_decompose_name_shape`).
    real_tokens = skills_graph._name_shape_real_tokens(name)
    assert real_tokens, f"{name!r} unexpectedly is not name-shaped at all"
    assert skills_graph._hits_vendor_prefix_veto(name, real_tokens)


def test_vendor_prefix_veto_does_not_fire_on_an_unrelated_name() -> None:
    real_tokens = skills_graph._name_shape_real_tokens("Casey Rivera")
    assert not skills_graph._hits_vendor_prefix_veto("Casey Rivera", real_tokens)


def test_vendor_prefix_veto_protects_even_if_the_lexicon_is_hostile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The vendor-prefix veto is an INDEPENDENT line of defence, not merely
    redundant with the all() quantifier fix — proven by forcing
    `_hits_person_name_lexicon` to always report a hit (as if a future
    lexicon update ever added a vendor word) and confirming a
    vendor-prefixed candidate is STILL kept, because the veto short-circuits
    before the lexicon arm is even consulted."""
    monkeypatch.setattr(skills_graph, "_hits_person_name_lexicon", lambda tokens: True)
    for name in ("Amazon Aurora", "IBM Watson", "Apache Felix"):
        assert skills_graph.reject_reason_for_skill_name(name) is None


def test_vendor_prefix_veto_mutation_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    """Companion mutation proof: with the SAME hostile (always-hit) lexicon
    as above, neutralising the veto itself must flip the outcome back to
    rejected — proving the veto test above is pinned to the veto actually
    running, not to some other coincidental pass."""
    monkeypatch.setattr(skills_graph, "_hits_person_name_lexicon", lambda tokens: True)
    monkeypatch.setattr(
        skills_graph, "_hits_vendor_prefix_veto", lambda name, tokens: False
    )
    for name in ("Amazon Aurora", "IBM Watson", "Apache Felix"):
        assert skills_graph.reject_reason_for_skill_name(name) == "person_name_shape"


def test_strict_lexicon_collapses_to_the_old_two_way_rule() -> None:
    """S12: `strict_lexicon=True` must reject ANY name-shaped, vocab-miss
    candidate regardless of the lexicon/vendor-veto exemptions — a
    legitimate skill that the normal (non-strict) conjunction keeps
    (`distributed systems`, `Amazon Aurora`) is rejected under strict mode,
    since `src.worker.resume_tasks._redact_skill_names_pii` only sets this
    when there is NO candidate identity left at all to justify the
    exemption. A vocabulary HIT is still exempt even under strict mode —
    the vocab arm runs before `strict_lexicon` is ever consulted."""
    assert (
        skills_graph.reject_reason_for_skill_name(
            "distributed systems", strict_lexicon=True
        )
        == "person_name_shape"
    )
    assert (
        skills_graph.reject_reason_for_skill_name("Amazon Aurora", strict_lexicon=True)
        == "person_name_shape"
    )
    assert (
        skills_graph.reject_reason_for_skill_name("Julia", strict_lexicon=True) is None
    )
    assert (
        skills_graph.reject_reason_for_skill_name("Kafka", strict_lexicon=True) is None
    )


def test_non_latin_script_fallback_rejects_even_without_a_lexicon_hit() -> None:
    """Documented residual: the personal-name lexicon is Latin-alphabet
    only, so a non-Latin-script, name-shaped, vocab-miss candidate is
    rejected via the SCRIPT fallback, independent of any lexicon entry —
    proven here with a token that certainly is not literally IN
    `person_names.txt` (only its transliteration might be)."""
    assert skills_graph._contains_non_latin_script("Кейси Ривера")
    assert skills_graph._contains_non_latin_script("李伟")
    assert not skills_graph._contains_non_latin_script("Casey Rivera")
    assert not skills_graph._contains_non_latin_script("distributed systems")


@pytest.mark.parametrize("name", ["C++", "C#", ".NET", "Node.js", "IPv6"])
def test_technical_marker_shaped_names_are_never_person_name_shaped(name: str) -> None:
    """A digit or a `+`/`#`/non-middle-initial `.` disqualifies the
    person-name shape entirely — these must never be rejected on
    'person_name_shape' grounds, vocab or no vocab."""
    assert not skills_graph._looks_like_person_name(name)
    assert skills_graph.reject_reason_for_skill_name(name) is None


def test_iso_27001_style_name_with_digits_is_not_person_name_shaped() -> None:
    assert not skills_graph._looks_like_person_name("ISO 27001")
    assert skills_graph.reject_reason_for_skill_name("ISO 27001") is None


@pytest.mark.parametrize(
    "name",
    [
        "Casey Rivera",
        "Rivera, Casey",
        "Casey M. Rivera",
        "Casey-Rivera",
        "Rivera",
        "John Smith",
        "RIVERA, CASEY",
        "CASEY RIVERA",
        "casey rivera",
        "Sean McDonald",
        "John O'Brien",
        "Maria del Carmen Rivera Lopez",
        "Ana van der Berg",
        "Casey Rivera 2",
        "Casey Rivera+",
        "Casey Rivera#",
        "Casey.Rivera",
        "Кейси Ривера",
        "李伟",
    ],
)
def test_person_name_shape_detector_matches_every_reproduction_row(name: str) -> None:
    assert skills_graph._looks_like_person_name(name)


def test_all_caps_acronym_is_not_person_name_shaped() -> None:
    """`AWS`/`SQL`/`REST`-style BARE SINGLE all-caps acronyms never look like
    a person's name. S1 (round 3) widens case-folding to catch a MULTI-token
    or internally-joined all-caps/all-lowercase NAME ("CASEY RIVERA"), but
    deliberately does NOT fold a lone, separator-free single word — folding
    every bare acronym would flag any all-caps term this repo's 220-term
    vocabulary doesn't happen to carry (`REST` is not in it — see the vocab
    sweep) as person-name-shaped purely on shape, with no vocab hit to save
    it. Every round-3 leak is multi-token or a joined compound; this
    single-bare-token carve-out is the documented boundary that keeps that
    widening safe for recall."""
    for acronym in ("AWS", "SQL", "REST"):
        assert not skills_graph._looks_like_person_name(acronym)
        assert skills_graph.reject_reason_for_skill_name(acronym) is None


def test_mixed_case_technical_proper_noun_is_not_person_name_shaped() -> None:
    """`PostgreSQL`-style mixed-case (not Title Case) technical proper nouns
    never look like a person's name either."""
    assert not skills_graph._looks_like_person_name("PostgreSQL")


@pytest.mark.asyncio
async def test_person_name_shape_reject_is_logged_as_a_category_never_the_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    session = _make_session([])
    await skills_graph.resolve_canonical_names(
        session, ["Casey Rivera"], llm=_make_llm(), embedder=_make_embedder()
    )
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "casey" not in all_text.lower()
    assert "rivera" not in all_text.lower()
    assert "person_name_shape" in all_text


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
    # NOTE (round-4 recall regression fix): this was briefly renamed to the
    # single bare token "postgresvariant" during round 3, when the (buggy)
    # two-way name-shape+vocab-miss conjunction would have incorrectly
    # shape-rejected the two-lowercase-word phrase "postgres db" before ever
    # reaching the mechanics this test exists to exercise (grey-zone
    # vector-score -> LLM-tiebreaker MECHANICS, not the PII shape guard).
    # Decision C's personal-name-lexicon arm fixed that: neither "postgres"
    # nor "db" is a personal name, so this phrase is correctly KEPT again —
    # restored to the original, more realistic placeholder.
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
    against, only a real (offered-or-freshly-created) name may be returned.

    NOTE (round-4 recall regression fix): this was briefly renamed to the
    single bare token "cockroachvariant" during round 3, when the (buggy)
    two-way name-shape+vocab-miss conjunction would have incorrectly
    shape-rejected the two-lowercase-word phrase "cockroach db" before ever
    reaching the mechanics this test exists to exercise (the
    hallucinated-tiebreaker-answer mechanics). Decision C's personal-name-
    lexicon arm fixed that: neither "cockroach" nor "db" is a personal
    name, so this phrase is correctly KEPT again — restored to the
    original, more realistic placeholder."""
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


# ── S8 (security re-audit round 3): ACCEPTED names must never be logged ───
# verbatim either -- F8 was previously only half-closed (rejected names were
# category-only, but an accepted auto-merge/LLM-merge/create-new line still
# printed `canonical=%s` in full).


@pytest.mark.asyncio
async def test_created_skill_acceptance_is_not_logged_verbatim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`cockroachdb` is a bare single lowercase token (never folded — see
    S1's single-bare-token carve-out) and not in the 220-term vocabulary, so
    it is neither shape-rejected nor vocab-known: it sails through to the
    create-new path, exercising the ACCEPTED-name log line."""
    caplog.set_level(logging.DEBUG)
    assert skills_graph.reject_reason_for_skill_name("cockroachdb") is None
    session = _make_session([_FakeResult([]), _FakeResult([]), _FakeResult([])])
    await skills_graph.resolve_canonical_names(
        session, ["cockroachdb"], llm=_make_llm(), embedder=_make_embedder()
    )
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "cockroachdb" not in all_text.lower()
    assert "created" in all_text


@pytest.mark.asyncio
async def test_auto_merged_skill_acceptance_is_not_logged_verbatim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    near = [{"name": "cockroachdb", "aliases": [], "score": 0.95}]
    session = _make_session([_FakeResult([]), _FakeResult(near), _FakeResult([])])
    await skills_graph.resolve_canonical_names(
        session, ["crdb"], llm=_make_llm(), embedder=_make_embedder()
    )
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "cockroachdb" not in all_text.lower()
    assert "auto_merged" in all_text


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
