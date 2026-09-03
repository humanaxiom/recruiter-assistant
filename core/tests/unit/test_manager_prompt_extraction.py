"""RED — extracting the hiring manager's additional requirements.

SPONSOR 2026-09-02 §I4: *"Additional requirement in text. I.e. a prompt allow
the hiring manager to list out special skills, experience they are looking for.
These might or might not be in the job posting."*

The schema, the DDL column and the 0.10 weight shipped already. This is the
pass that turns the manager's free text into the `Skill` shapes the ranking
engine scores, and the four properties below are the ones worth pinning:

**1. Must-have is the default.** A manager writing this note is stating a
requirement, not a preference. Only an explicit softener ("nice to have",
"bonus", "ideally") demotes one.

**2. It is a SECOND pass, and failing it must not fail the JD parse.** The JD
is the job of record; a manager note that the model cannot parse is a degraded
extra, not a reason to leave the requisition unparsed. Same non-fatal posture
as the cover-letter parse.

**3. It must never re-run the JD extraction.** `parse_job` re-runs the LLM and
can change the extracted requirements underneath a shortlist someone is already
reading (ROADMAP §5). Editing the manager note alone must not touch the JD.

**4. It must not encode a protected characteristic.** A free-text box invites
"looking for someone young and energetic". The prompt refuses those, and the
refusal is silent rather than an error — an error would tell the manager which
phrasing to route around.

None of this exists yet — RED half of the TDD cycle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.prompts import load_prompt
from src.schemas.jobs import ManagerRequirements

# ------------------------------------------------------------- the prompt


def test_the_prompt_pair_loads_and_carries_the_note() -> None:
    p = load_prompt("manager_prompt_v1", manager_text="Must have MEG analysis.")
    assert p.version == "manager_prompt_v1"
    assert "MEG analysis" in p.user
    assert len(p.messages) == 2


def test_the_prompt_makes_must_have_the_default() -> None:
    """Property 1. The instruction has to be explicit in the prompt text — a
    small local model defaults to whatever the wording suggests, and "skills
    they are looking for" reads as preference unless told otherwise."""
    system = load_prompt("manager_prompt_v1", manager_text="x").system
    assert "MUST-HAVE IS THE DEFAULT" in system
    for softener in ("nice to have", "bonus", "ideally", "preferred"):
        assert softener in system.lower(), (
            f"the prompt must name {softener!r} as a demoting phrase, or the "
            "model has no stated basis for the nice-to-have branch"
        )


def test_the_prompt_refuses_protected_characteristics() -> None:
    """Property 4. A free-text requirements box is the single most likely place
    in this product for a protected-ground requirement to be typed in good
    faith. The prompt must refuse to encode one — and stay silent about it, so
    the refusal cannot be read as a hint about which phrasing would work."""
    system = load_prompt("manager_prompt_v1", manager_text="x").system
    for ground in ("age", "sex", "race", "religion", "disability", "national origin"):
        assert ground in system.lower(), f"{ground} is not named as excluded"
    assert "silently" in system.lower()


def test_the_prompt_treats_the_note_as_data_not_instructions() -> None:
    """Same prompt-injection posture as the cover-letter and résumé prompts.
    The note is typed by a trusted user, but "trusted" is not "sanitised" — it
    can be pasted from anywhere."""
    system = load_prompt("manager_prompt_v1", manager_text="x").system
    assert "SECURITY" in system
    assert "data" in system.lower() and "instructions" in system.lower()


def test_the_prompt_forbids_inventing_requirements() -> None:
    """The same anti-fabrication rule the JD prompt carries. A manager who
    writes "Kafka" must not have "distributed streaming systems" scored
    against candidates on their behalf."""
    system = load_prompt("manager_prompt_v1", manager_text="x").system
    assert "invent" in system.lower()


# ---------------------------------------------------- the extraction call


@pytest.mark.asyncio
async def test_extract_returns_the_parsed_requirements() -> None:
    from src.pipeline.parsing import extract_manager_requirements

    expected = ManagerRequirements(must_have_skills=[])
    llm = MagicMock(chat_json=AsyncMock(return_value=expected))
    got = await extract_manager_requirements(llm, "Must have MEG analysis.")

    assert got is expected
    [messages, schema] = llm.chat_json.call_args.args
    assert schema is ManagerRequirements
    assert "MEG analysis" in messages[1]["content"]


@pytest.mark.asyncio
async def test_extract_returns_none_for_an_empty_note() -> None:
    """No note is not an empty note. Returning ``None`` (rather than an empty
    ``ManagerRequirements``) is what lets the scorer tell "nobody asked" from
    "asked and matched nothing" — the distinction ``manager_prompt_measured``
    exists for. It also saves an LLM round trip per job."""
    from src.pipeline.parsing import extract_manager_requirements

    llm = MagicMock(chat_json=AsyncMock())
    for empty in (None, "", "   ", "\n\t "):
        assert await extract_manager_requirements(llm, empty) is None
    llm.chat_json.assert_not_called()


@pytest.mark.asyncio
async def test_extract_is_non_fatal_on_invalid_llm_output() -> None:
    """Property 2, at the unit that owns it. The JD is the job of record; a
    manager note the model cannot parse degrades to "no extra requirements",
    it does not strand the requisition."""
    from src.pipeline.llm import LLMOutputInvalidError
    from src.pipeline.parsing import extract_manager_requirements

    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMOutputInvalidError("bad")))
    assert await extract_manager_requirements(llm, "Must have Kafka.") is None


@pytest.mark.asyncio
async def test_extract_lets_a_transient_outage_escape() -> None:
    """A model that is DOWN is not the same as a note that will not parse.
    Swallowing an outage here would silently drop the manager's requirements
    on every job parsed during it, with nothing on screen to say so — so the
    transient error escapes and arq retries, exactly as the JD path does."""
    from src.pipeline.llm import LLMUnavailableError
    from src.pipeline.parsing import extract_manager_requirements

    llm = MagicMock(chat_json=AsyncMock(side_effect=LLMUnavailableError("down")))
    with pytest.raises(LLMUnavailableError):
        await extract_manager_requirements(llm, "Must have Kafka.")


# ------------------------------------------------------- the worker wiring


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _ctx(conn: MagicMock, llm: MagicMock, embedder: MagicMock) -> dict[str, Any]:
    pool = MagicMock(name="pg_pool")
    pool.acquire = MagicMock(return_value=_acm(conn))
    return {
        "pg_pool": pool,
        "llm": llm,
        "embedder": embedder,
        "blob_store": MagicMock(name="blob_store"),
    }


def test_the_worker_reads_the_manager_note_off_the_job_row() -> None:
    """The column has to be SELECTed or the extraction can never see it — the
    shape of defect where a field is written, stored, and read by nothing."""
    from src.worker.tasks import _JOB_META_SQL

    assert "additional_requirements" in _JOB_META_SQL
