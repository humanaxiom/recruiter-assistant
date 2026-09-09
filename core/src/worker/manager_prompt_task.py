"""Re-extract the manager's additional requirements, and nothing else.

SPONSOR §I4. Enqueued by ``PATCH /jobs/{id}`` whenever that request touches
``additional_requirements`` — including clearing it.

**Why this is its own task rather than a ``parse_job`` re-run.**
``JobUpdate``'s comment has said since the field was added that editing the
note "re-extracts ONLY the manager prompt — it must never re-run the JD
parse", and nothing implemented it: extraction lived solely inside
``parse_job``, so an edited note kept an extraction describing the previous
text. Re-running ``parse_job`` would have been the easy fix and the wrong one.
It re-derives the POSTING's requirements from the LLM, which can change them
underneath a shortlist somebody is reading (ROADMAP §5), and it is gated on
``status = 'draft'`` — so on an open requisition, the case this whole feature
exists for, it would have done nothing at all and said it succeeded.

**Failure posture**, inherited from ``extract_manager_requirements`` and
deliberately not softened here:

* A note that will not parse yields ``None``, which is written. The row ends
  up saying *nobody asked*, which is honest: nothing was successfully asked
  for. It does not strand the job.
* ``LLMUnavailableError`` escapes to arq's retry. "The model is down" and
  "this note is nonsense" are different claims and must not share an outcome —
  swallowing the first would silently drop a manager's requirements during an
  outage with nothing on screen to say so.

**What it does NOT do: re-rank.** Changing the requirements changes the
ranking, and regenerating a shortlist on the manager's behalf would move a
list under somebody reading it. The job page says so instead.
"""

from __future__ import annotations

import logging
from uuid import UUID

from src.pipeline.llm import LLMClient
from src.pipeline.parsing import extract_manager_requirements
from src.services import job_service

log = logging.getLogger(__name__)

_NOTE_SQL = "SELECT additional_requirements FROM jobs WHERE id = $1"


async def extract_manager_prompt(ctx: dict, job_id_str: str) -> str:  # type: ignore[type-arg]
    """Re-extract ``additional_requirements`` into
    ``additional_requirements_parsed``.

    Reads the note back from the row rather than taking it as an argument: the
    PATCH has already committed, the row is the source of truth, and passing
    free text a manager typed through the Redis queue would put it somewhere
    with a different retention story than the column it came from.
    """
    pool = ctx["pg_pool"]
    llm: LLMClient = ctx["llm"]
    job_id = UUID(job_id_str)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(_NOTE_SQL, job_id)
        if row is None:
            log.warning("extract_manager_prompt.missing job_id=%s", job_id_str)
            return "missing"

        requirements = await extract_manager_requirements(
            llm, row["additional_requirements"]
        )
        await job_service.record_manager_requirements(conn, job_id, requirements)

    # Never logs the note itself — it is free text a manager typed and may name
    # a candidate, so it does not belong in the logs.
    log.info(
        "extract_manager_prompt.done job_id=%s extracted=%s",
        job_id_str,
        requirements is not None,
    )
    return "ok" if requirements is not None else "empty"
