"""Extract the hiring manager's additional requirements (sponsor §I4).

*"Additional requirement in text. I.e. a prompt allow the hiring manager to
list out special skills, experience they are looking for. These might or might
not be in the job posting."*

A SECOND extraction pass, deliberately separate from the JD's:

* **The JD is the job of record.** ``description_raw`` is the posting as
  published and must stay byte-faithful to it; the manager's note is a
  different claim by a different author, so it gets its own field, its own
  prompt and its own stored extraction. A shortlist that cannot separate "the
  posting required this" from "the manager asked for this" cannot answer the
  question the defense pack exists to answer.
* **Editing the note must never re-run the JD parse.** ``parse_job`` re-runs
  the LLM and can change the extracted requirements underneath a shortlist
  someone is already reading (ROADMAP §5). Keeping this callable on its own
  is what makes a note-only re-extraction possible.

It reuses ``Skill`` — the same shape ``JDExtracted`` emits — so the ranking
engine never learns a second requirement representation. What distinguishes a
manager requirement from a JD one is its PROVENANCE (which field it was
extracted into), not its shape.
"""

from __future__ import annotations

import logging

from src.pipeline.llm import LLMClient, LLMOutputInvalidError
from src.prompts import load_prompt
from src.schemas.jobs import ManagerRequirements

log = logging.getLogger(__name__)

# Measured budget for this prompt. The note is capped at 4,000 characters and
# the output is a short JSON object, so this sits well clear.
#
# NOT inherited from ``REASONING_JSON_MIN_TOKENS`` (8192), and that is
# deliberate: the token floor in this repo is per-PROMPT, not per-model — the
# 8192 figure was measured against résumé/JD extraction, and the ``skills_graph``
# tiebreaker gives identical answers at 128. Re-measure with
# ``scripts/model-check.sh`` before trusting this against a different model
# rather than assuming a bigger number is safer.
MANAGER_PROMPT_MAX_TOKENS = 2048


async def extract_manager_requirements(
    llm: LLMClient, manager_text: str | None
) -> ManagerRequirements | None:
    """Turn the manager's free-text note into structured requirements.

    Returns ``None`` — never an empty ``ManagerRequirements`` — when there is
    no note or the note could not be parsed. The distinction is load-bearing:
    ``None`` means *nobody asked*, and it is what lets the combine mark the
    resulting 0.0 sub-score as unmeasured rather than asserting the candidate
    matched none of the manager's requirements. An empty object would say the
    manager asked for a list of nothing, which is a different (and false)
    claim.

    **Failure posture, split the same way the JD path splits it:**

    * ``LLMOutputInvalidError`` — the note will not parse. Non-fatal: return
      ``None`` and let the requisition finish parsing. The JD is the job of
      record and a degraded extra must not strand it.
    * ``LLMUnavailableError`` — the model is DOWN, which is not the same claim
      at all. Deliberately NOT caught, so it escapes to arq's retry. Swallowing
      it would silently drop the manager's requirements from every job parsed
      during an outage, with nothing on screen to say so.
    """
    if manager_text is None or not manager_text.strip():
        return None

    prompt = load_prompt("manager_prompt_v1", manager_text=manager_text)
    try:
        return await llm.chat_json(
            prompt.messages,
            ManagerRequirements,
            max_tokens=MANAGER_PROMPT_MAX_TOKENS,
            max_retries=1,
        )
    except LLMOutputInvalidError as exc:
        # Logged without the note itself: it is free text a manager typed and
        # may name a candidate, so it does not belong in the logs.
        log.error("manager_prompt.llm_invalid error=%s", exc)
        return None
