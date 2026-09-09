"""RED — the JD extraction has to reach the job ROW, not just the JSONB blob.

**Measured on the pilot box, 26 jobs, 23 of them real SFU JDs bulk-uploaded as
files.** Every one shows a filename stem where its title should be:

    row title:    20260612 00138559 APSA JDFN 20260612
    parsed title: Multimedia Specialist

The LLM extracted "Multimedia Specialist" correctly, on the first try, weeks
ago. ``record_parsed`` wrote it into ``jobs.description_parsed`` and into no
column, so nothing a human looks at ever saw it. Department is the same shape
(6 of 26 filled, all by hand) and so is Location (0 of 26).

This is one defect with three faces, and it is the reason the list looked
broken: **the extraction was stored and read by nothing.**

Three different merge rules, because the three columns are not alike:

* ``department`` / ``location`` are nullable. Fill when EMPTY, never
  overwrite — a human's edit, a bulk manifest value, or a Taleo listing's
  value has to survive a re-parse, or the "override" half of what the sponsor
  asked for is a lie.
* ``title`` is ``NOT NULL``: there is always something there, so "fill when
  empty" can never fire. It is replaced only when the row itself records that
  its title was DERIVED rather than chosen — ``title_provisional``, set by the
  bulk path when it falls back to the filename stem. A recruiter who typed a
  title keeps it.

``title_provisional`` re-opens a Phase 0 cut (hris's ``title_autofilled``).
The cut was made before anyone had bulk-uploaded 23 requisitions whose
filenames are req numbers; that is the fact that changed. The hris column name
stays absent — see the guard in ``test_services_writeback.py``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.schemas.jobs import JDExtracted

_NOW = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _mock_conn(result: str = "UPDATE 1") -> Any:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=result)
    return conn


async def _record(**extracted_kwargs: Any) -> tuple[str, tuple[Any, ...]]:
    """Call ``record_parsed`` and hand back (sql, bind args)."""
    from src.services import job_service

    conn = _mock_conn()
    base: dict[str, Any] = {"title": "Multimedia Specialist"}
    base.update(extracted_kwargs)
    await job_service.record_parsed(conn, uuid4(), JDExtracted(**base), _NOW)
    call = conn.execute.await_args
    return call.args[0], call.args[1:]


# ── the extraction schema has to carry a department at all ──────────────────


def test_the_extraction_schema_carries_a_department() -> None:
    """It did not. Nothing could fill ``jobs.department`` from a JD, which is
    why 6 of 26 rows have one and all six were typed by a human."""
    assert "department" in JDExtracted.model_fields


def test_the_department_is_optional_and_bounded() -> None:
    """A JD that does not name a unit must parse, and a model that returns an
    essay must not blow the column."""
    assert JDExtracted(title="X").department is None
    assert JDExtracted(title="X", department="  Faculty of Health Sciences  ")


# ── department / location: fill when empty, never clobber ───────────────────


@pytest.mark.asyncio
async def test_the_extracted_department_is_written_to_its_column() -> None:
    sql, args = await _record(department="School of Medicine")
    assert re.search(r"\bdepartment\s*=", sql), sql
    assert "School of Medicine" in args


@pytest.mark.asyncio
async def test_the_extracted_location_is_written_to_its_column() -> None:
    sql, args = await _record(location="Burnaby")
    assert re.search(r"\blocation\s*=", sql), sql
    assert "Burnaby" in args


@pytest.mark.asyncio
async def test_the_written_location_is_canonicalised_to_a_campus() -> None:
    """ "Burnaby, BC" and "bby" must land on the same value as "Burnaby", or
    the column cannot be grouped, filtered, or counted."""
    _, args = await _record(location="SFU Burnaby Campus, BC")
    assert "Burnaby" in args
    assert "SFU Burnaby Campus, BC" not in args


@pytest.mark.asyncio
async def test_an_existing_department_or_location_survives_a_reparse() -> None:
    """THE test for the "override" half of the request. A recruiter fixes the
    campus, the JD is re-parsed, and the LLM's guess must not win. Expressed
    in SQL rather than in Python because the row is not read first — the
    UPDATE itself has to be the one that declines."""
    sql, _ = await _record(department="Wrong", location="Surrey")
    for column in ("department", "location"):
        assert re.search(
            rf"{column}\s*=\s*COALESCE\(\s*NULLIF\(\s*jobs\.{column}",
            sql,
            re.IGNORECASE,
        ), f"{column} must be COALESCE(NULLIF(jobs.{column}, ''), $n): {sql}"


@pytest.mark.asyncio
async def test_an_empty_string_column_counts_as_unset() -> None:
    """``NULLIF(col, '')`` and not a bare ``COALESCE``: the create form posts
    an empty string for a field left blank, so without this a job created
    through the UI is permanently unfillable — the column is '' rather than
    NULL and COALESCE would treat it as a value."""
    sql, _ = await _record(department="Anything")
    assert "NULLIF" in sql.upper()


# ── title: replaced only when the row says its title was derived ────────────


@pytest.mark.asyncio
async def test_a_provisional_title_is_replaced_by_the_extracted_one() -> None:
    sql, args = await _record()
    assert "title_provisional" in sql
    assert "Multimedia Specialist" in args


@pytest.mark.asyncio
async def test_a_chosen_title_is_never_replaced() -> None:
    """The guard is in the SQL: the UPDATE only touches ``title`` when the row
    itself flags it provisional, so a title somebody typed is untouchable
    without the flag being set — which only the bulk fallback does."""
    sql, _ = await _record()
    assert re.search(
        r"title\s*=\s*CASE\s+WHEN\s+jobs\.title_provisional", sql, re.IGNORECASE
    ), sql


@pytest.mark.asyncio
async def test_replacing_the_title_clears_the_flag() -> None:
    """Otherwise the row stays permanently overwritable and the NEXT re-parse
    silently renames a title a human may since have corrected."""
    sql, _ = await _record()
    assert re.search(
        r"title_provisional\s*=\s*CASE\s+WHEN\s+jobs\.title_provisional",
        sql,
        re.IGNORECASE,
    ), sql


# ── unchanged contracts ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_draft_scoping_and_blob_write_are_untouched() -> None:
    """The optimistic-concurrency guard and the JSONB blob are why this
    function exists; widening it must not disturb either."""
    sql, args = await _record(department="School of Medicine")
    assert re.search(r"status\s*=\s*'draft'", sql, re.IGNORECASE), sql
    blob = json.loads(args[1])
    assert blob["department"] == "School of Medicine"
