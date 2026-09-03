"""RED — every column the job read SELECTs must reach ``JobOut``.

Found while wiring the Taleo provenance UI, and it is the same defect class
this branch has now hit three times: a field that is written, stored, and read
by nothing.

``_row_to_jobout`` maps row → DTO **field by field, explicitly**, which is the
right call (ADR-006's "low": ``blind_review`` must be read explicitly, never
left to a fail-open pydantic default). The cost is that adding a column to
``_JOB_COLS`` does not add it to the API. Both of the columns this branch
added were being SELECTed and dropped on the floor:

* ``additional_requirements`` — the manager's own note. Stored, extracted,
  scored, and never returned, so the UI could not show a manager what they had
  typed, and ``JobOut.additional_requirements`` was ``None`` on every read.
* ``source`` / ``external_id`` / ``external_url`` / ``external_last_seen_at``
  (ADR-046) — so a Taleo-synced job was indistinguishable over the API from
  one somebody typed, and the link back to the posting was unreachable.

Neither had a failing test, because ``JobOut``'s fields all default and
``extra="forbid"`` only rejects UNKNOWN keys — it says nothing about known
ones being skipped. So the guard below is not another per-field assertion: it
compares the SELECT list against the DTO and fails on any column the mapping
forgets, which is the only shape that catches the NEXT one.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from uuid import uuid4

_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid4(),
        "title": "Research Analyst",
        "department": "Neuroscience",
        "location": "Burnaby",
        "employment_type": "full_time",
        "seniority": None,
        "min_years": None,
        "description_raw": "A detailed job description of the role. " * 3,
        "description_parsed": None,
        "status": "draft",
        "retention_days": 180,
        "shortlist_top_percent": 100,
        "blind_review": True,
        "failure_reason": None,
        "created_by": "asalah",
        "created_at": _TS,
        "updated_at": _TS,
        "parsed_at": None,
        "closed_at": None,
        "additional_requirements": "Must have MEG analysis.",
        "additional_requirements_parsed": None,
        "source": "taleo",
        "external_id": "7124",
        "external_url": "https://tre.tbe.taleo.net/req?rid=7124",
        "external_last_seen_at": _TS,
    }
    base.update(kw)
    return base


# ------------------------------------------------------------- the structural guard


def test_every_selected_column_reaches_the_dto() -> None:
    """THE test. Not a per-field assertion — those are what got skipped.

    Compares ``_JOB_COLS`` (what the read SELECTs) against ``JobOut``'s fields
    and fails on any column the mapping silently drops. A column that is
    genuinely internal belongs in the allow-list below, with a reason, rather
    than being quietly absent.
    """
    import inspect

    from src.schemas.jobs import JobOut
    from src.services.job_service import _JOB_COLS, _row_to_jobout

    selected = {
        c.strip() for c in re.split(r",\s*", _JOB_COLS.replace("\n", " ")) if c.strip()
    }
    # A STATIC check of the mapping's source, not a value check on a built DTO.
    # The obvious version — "build one and look for None fields" — conflates
    # "the mapping forgot this column" with "this column is legitimately null",
    # and half of JobOut is nullable (seniority, min_years, parsed_at,
    # closed_at, failure_reason, both parsed blobs). It reported seven false
    # positives on its first run. Reading the mapping is unambiguous.
    source = inspect.getsource(_row_to_jobout)
    dto_fields = set(JobOut.model_fields)

    unmapped = sorted(
        col
        for col in selected
        if col in dto_fields
        and f'raw["{col}"]' not in source
        and f'raw.get("{col}"' not in source
    )
    assert not unmapped, (
        f"{unmapped} are SELECTed by _JOB_COLS and exist on JobOut, but "
        "_row_to_jobout never reads them — the column is stored and returned to "
        "nobody. Map it, or drop it from _JOB_COLS."
    )


# ---------------------------------------------------- the two that were dropped


def test_the_managers_note_is_returned() -> None:
    """Sponsor §I4. Without this the manager cannot see what they typed, on any
    surface, ever — the note goes in and never comes back out."""
    from src.services.job_service import _row_to_jobout

    job = _row_to_jobout(_row())
    assert job.additional_requirements == "Must have MEG analysis."


def test_the_job_source_and_posting_link_are_returned() -> None:
    """ADR-046. Without these a synced job is indistinguishable over the API
    from one somebody typed, and the link back to the live posting — the whole
    provenance half of §I3 — is unreachable from any client."""
    from src.services.job_service import _row_to_jobout

    job = _row_to_jobout(_row())
    assert job.source == "taleo"
    assert job.external_id == "7124"
    assert job.external_url == "https://tre.tbe.taleo.net/req?rid=7124"
    assert job.external_last_seen_at == _TS


def test_a_manual_job_reports_its_source_as_manual() -> None:
    """Every human-created job. ``source`` is NOT NULL with a 'manual' default,
    so this is what the overwhelming majority of reads return — and the UI
    branches on it to decide whether to show provenance at all."""
    from src.services.job_service import _row_to_jobout

    job = _row_to_jobout(
        _row(
            source="manual",
            external_id=None,
            external_url=None,
            external_last_seen_at=None,
        )
    )
    assert job.source == "manual"
    assert job.external_url is None
