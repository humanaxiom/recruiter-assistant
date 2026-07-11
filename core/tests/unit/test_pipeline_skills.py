"""Unit tests for the Neo4j-free skill-matching slice
(``src/pipeline/skills.py``).

Ported from hris ``apps/worker/src/worker/skill_normalize.py``
(``phase3-source-dossier.md`` §12), trimmed to the three pure functions
that don't touch Neo4j: ``match_skills_in_text``,
``canonicalize_skill_names``, ``build_summary_text``. The alias table
itself (``core/src/pipeline/skill_data/aliases.yaml``) is ported
verbatim from hris ``infra/skills/aliases.yaml`` — ``javascript``/``js``
and ``kubernetes``/``k8s`` are real entries there (verified by reading
the shipped hris file), used below as real-world regression anchors.

**Path-resolution regression (the actual bug this file guards against)**:
hris's ``skill_normalize.py`` resolves its YAML via a hardcoded
``Path(__file__).resolve().parents[4]``-relative walk to a repo-root
``infra/skills/`` directory. That depth is specific to hris's deeper
package nesting and is WRONG for the flatter ``core/src/pipeline/
skills.py`` module depth in this repo — and a repo-root ``infra/`` would
also be invisible inside the Docker build context anyway (``./core``
only). ``test_match_skills_resolves_real_aliases_yaml_regardless_of_cwd``
is what catches a wrong-depth port.

**Naming assumption**: the "missing YAML degrades gracefully" test
monkeypatches a module attribute named ``_ALIASES_PATH`` (the same name
hris's ``skill_normalize.py`` uses for its resolved path constant) since
the trimmed port is not expected to rename it. If the implementation
uses a different constant name, that one test will need updating to
match — it is not a behavioural assumption, just a naming one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.pipeline.skills import (
    build_summary_text,
    canonicalize_skill_names,
    match_skills_in_text,
)
from src.schemas.jobs import JDExtracted, Skill

# ── match_skills_in_text ──────────────────────────────────────────────────


def test_match_skills_finds_aliased_term_case_insensitively() -> None:
    text = "I have shipped production apps using JS extensively."
    assert "javascript" in match_skills_in_text(text)


def test_match_skills_respects_word_boundaries_not_substring() -> None:
    """ "js" must not fire inside a longer token like "abcjs"."""
    text = "I maintain a legacy internal tool called abcjs for reporting."
    assert "javascript" not in match_skills_in_text(text)


def test_match_skills_returns_first_seen_order_deduped() -> None:
    text = "Python and Python again, then SQL, then Python once more."
    matches = match_skills_in_text(text)
    assert matches.count("python") == 1
    assert matches.index("python") < matches.index("sql")


# ── canonicalize_skill_names ──────────────────────────────────────────────


def test_canonicalize_dedupes_case_and_whitespace_preserving_order() -> None:
    names = ["Python", "python", "  PYTHON  ", "SQL", "sql"]
    assert canonicalize_skill_names(names) == ["python", "sql"]


def test_canonicalize_skill_names_empty_input_is_empty_output() -> None:
    assert canonicalize_skill_names([]) == []


# ── Path-resolution regression ────────────────────────────────────────────


def test_match_skills_resolves_real_aliases_yaml_regardless_of_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Import fresh and run from an unrelated cwd: the YAML load must be
    ``Path(__file__)``-relative, not cwd-relative."""
    monkeypatch.chdir(tmp_path)
    import importlib

    import src.pipeline.skills as skills_mod

    importlib.reload(skills_mod)

    matches = skills_mod.match_skills_in_text(
        "Deployed workloads to K8S in production."
    )
    assert "kubernetes" in matches


# ── Missing YAML degrades gracefully ──────────────────────────────────────


def test_missing_aliases_yaml_degrades_to_empty_table_not_an_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.pipeline.skills as skills_mod

    missing = tmp_path / "does-not-exist.yaml"
    monkeypatch.setattr(skills_mod, "_ALIASES_PATH", missing)
    for cached_name in ("_alias_table", "_skill_scanner"):
        cached = getattr(skills_mod, cached_name, None)
        if cached is not None and hasattr(cached, "cache_clear"):
            cached.cache_clear()

    result = skills_mod.match_skills_in_text("Python, Kubernetes, and SQL experience.")

    assert result == []


# ── build_summary_text ────────────────────────────────────────────────────


def test_build_summary_text_composes_documented_order() -> None:
    extracted = JDExtracted(
        title="Senior Backend Engineer",
        responsibilities=["Own the payments service", "Mentor junior engineers"],
        required_skills=[Skill(name="Python"), Skill(name="PostgreSQL")],
        nice_to_have_skills=[Skill(name="Kubernetes")],
    )

    summary = build_summary_text(extracted)

    assert summary == (
        "Senior Backend Engineer. "
        "Own the payments service Mentor junior engineers. "
        "Required: Python, PostgreSQL. "
        "Nice to have: Kubernetes"
    )


def test_build_summary_text_with_only_title() -> None:
    extracted = JDExtracted(title="Data Analyst")
    assert build_summary_text(extracted) == "Data Analyst"


def test_build_summary_text_never_includes_required_when_absent() -> None:
    extracted = JDExtracted(title="Data Analyst", nice_to_have_skills=[Skill(name="R")])
    summary = build_summary_text(extracted)
    assert "Required:" not in summary
    assert "Nice to have: R" in summary
