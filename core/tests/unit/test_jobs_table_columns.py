"""RED — the jobs table's columns must be able to hold something.

Reported from the running product: Department and Location are empty for every
job. Three DIFFERENT causes hide behind that one symptom — the report named
two columns and the investigation found a third — and only two are bugs:

* **Location was never plumbed at all.** The template has rendered
  ``job.location`` since Phase 7, but ``JobListItem`` has no such field and
  ``_LIST_JOBS_BASE_SQL`` never selected the column — so that cell has printed
  an em-dash for every job since the page existed, and always would have. A
  column that CANNOT populate is worse than a missing one: it reads as "this
  job has no location" rather than "this product does not know".
* **Résumés was never plumbed either, and rendered worse.** ``resume_count``
  is a field of ``JobDeleteOut``, a different DTO; the cell guarded it with
  ``is not none``, a Jinja Undefined is not None, so the guard passed and the
  cell printed nothing at all — not even the 0 it meant to fall back to. This
  is the column the sponsor's premise turns on ("postings receiving large
  numbers of applications").
* **Department is plumbed correctly and genuinely sparse** — 6 of 26 rows on
  the pilot box. Bulk-uploaded JDs never set it, and ``JDExtracted`` has no
  department field, so nothing fills it. That is a data-capture gap, not a
  code defect, and this file does not pretend otherwise.

Also added here, both requested by the sponsor after seeing the list:

* **Source, as a LINK** — §I3's provenance where the question is actually
  asked. Answering "which of these came from Taleo?" on the job DETAIL page
  means clicking into 40 jobs to find out.
* **Last updated** — the list has ``created_at`` only, so a job that was
  re-synced or re-parsed this morning is indistinguishable from one untouched
  since June.

None of these fields exist on ``JobListItem`` yet — RED half of the TDD cycle.
"""

from __future__ import annotations

import datetime as dt
import re
from uuid import uuid4

_TS = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)


def _item(**kw: object):  # type: ignore[no-untyped-def]
    from src.schemas.jobs import JobListItem

    base: dict[str, object] = {
        "id": uuid4(),
        "title": "Analyst",
        "department": "Neuroscience",
        "status": "open",
        "created_at": _TS,
        "parsed_at": _TS,
    }
    base.update(kw)
    return JobListItem(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------ the broken one


def test_the_list_row_can_carry_a_location() -> None:
    """The template has rendered ``job.location`` since Phase 7 against a DTO
    that has no such field. Every row printed an em-dash, permanently, and no
    test noticed because an em-dash is exactly what a null location renders
    as too."""
    assert _item(location="Burnaby").location == "Burnaby"


def test_the_list_query_selects_every_column_the_row_exposes() -> None:
    """The guard that would have caught it. A field on ``JobListItem`` that
    the SELECT never fetches is a field that is always ``None`` — the same
    "stored and read by nothing" shape, pointed the other way.

    Matched as whole words rather than by splitting on commas: the select
    list now carries a correlated subquery (``resume_count``) whose own
    commas and ``FROM`` would break a naive split.
    """
    from src.schemas.jobs import JobListItem
    from src.services.job_service import _LIST_JOBS_BASE_SQL

    select_clause = _LIST_JOBS_BASE_SQL.rsplit("FROM jobs", 1)[0]
    missing = sorted(
        f
        for f in JobListItem.model_fields
        if not re.search(rf"\b{re.escape(f)}\b", select_clause)
    )
    assert not missing, (
        f"{missing} exist on JobListItem but are never SELECTed by "
        "_LIST_JOBS_BASE_SQL — they will read None on every row forever"
    )


# ------------------------------------------------- the third broken one

# Found while fixing Location, and the same defect exactly: the template has
# rendered ``job.resume_count`` since Phase 7, ``JobListItem`` has no such
# field, and ``resume_count`` lives on ``JobDeleteOut`` — a different DTO
# entirely. Worse than Location, because the cell does not even fall back:
# ``{{ job.resume_count if job.resume_count is not none else 0 }}`` sees a
# Jinja Undefined, which *is not None*, so the guard passes and the column
# renders EMPTY rather than "0".
#
# It matters more than the other two. The sponsor's whole premise is
# "postings receiving large numbers of applications" — how many résumés a
# req has is the first thing a manager scans this list for.


def test_the_list_row_carries_its_resume_count() -> None:
    assert _item(resume_count=17).resume_count == 17


def test_a_job_with_no_resumes_counts_zero_not_null() -> None:
    """``count(*)`` cannot return NULL, and the column must not be able to
    render blank — an empty cell reads as "unknown", a 0 reads as "nobody
    has applied", and only one of those is true."""
    assert _item().resume_count == 0


def test_the_list_query_counts_resumes_per_job() -> None:
    """The DTO field is only half of it — the SELECT has to produce the
    number. Pinned structurally because no unit test can run the SQL."""
    from src.services.job_service import _LIST_JOBS_BASE_SQL

    sql = " ".join(_LIST_JOBS_BASE_SQL.split())
    assert "resume_count" in sql
    assert "FROM resumes" in sql
    # Correlated to THIS job, not a bare table count.
    assert "job_id = jobs.id" in sql


def test_the_resume_count_cell_needs_no_undefined_guard() -> None:
    """With the field on the DTO, the template's ``is not none`` dance is
    dead weight that hid the bug — a real int always renders."""
    from pathlib import Path

    tpl = (
        Path(__file__).resolve().parents[2] / "frontend" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert "job.resume_count is not none" not in tpl


# ------------------------------------------------------- the two new columns


def test_the_row_carries_its_source_and_posting_link() -> None:
    """Sponsor §I3, moved to where the question is asked. On the detail page
    only, answering "which of these are imported?" costs one click per job."""
    row = _item(source="taleo", external_url="https://tre.tbe.taleo.net/req?rid=7124")
    assert row.source == "taleo"
    assert row.external_url == "https://tre.tbe.taleo.net/req?rid=7124"


def test_a_manual_job_defaults_to_manual_with_no_link() -> None:
    """What almost every row is. The table must be able to tell the two apart
    without a null check at every call site."""
    row = _item()
    assert row.source == "manual"
    assert row.external_url is None


def test_the_row_carries_last_updated() -> None:
    """``created_at`` alone cannot distinguish a job re-synced this morning
    from one untouched since June — which is exactly the question a daily
    Taleo sync makes worth asking."""
    later = dt.datetime(2026, 9, 3, 8, 0, tzinfo=dt.UTC)
    assert _item(updated_at=later).updated_at == later


# ------------------------------------------------------------- the rendering


def test_the_table_links_a_synced_job_to_its_posting() -> None:
    """The link is the deliverable, not the badge. A source label that is not
    clickable answers "where did this come from" with a word, when the
    sponsor asked for the way back to the posting."""
    from pathlib import Path

    tpl = (
        Path(__file__).resolve().parents[2] / "frontend" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert "job.external_url" in tpl
    # Opens a third-party origin from an authenticated page.
    assert 'rel="noopener' in tpl


def test_the_table_does_not_label_every_manual_job() -> None:
    """26 rows reading "manual" is a column that informs nobody. The cell is
    only interesting when a job did NOT come from a human."""
    from pathlib import Path

    tpl = (
        Path(__file__).resolve().parents[2] / "frontend" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    assert "!= 'manual'" in tpl or '!= "manual"' in tpl


# ------------------------------------------------------- the catch-the-next-one


def test_every_job_attribute_the_table_renders_exists_on_the_dto() -> None:
    """THE guard, and the only one of these that generalises.

    Three columns of seven — Location, Source, Résumés — rendered an
    attribute ``JobListItem`` did not have. Jinja's default Undefined is
    silent: it renders as an empty string and compares as "not none", so a
    template referencing a field that does not exist looks exactly like a
    row whose field is genuinely null. Nothing in ~6,000 tests could tell
    those apart, because nothing compared the template against the DTO.

    Any future ``job.<x>`` added to this page now has to be a real field.
    """
    import re as _re
    from pathlib import Path

    from src.schemas.jobs import JobListItem

    tpl = (
        Path(__file__).resolve().parents[2] / "frontend" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    referenced = set(_re.findall(r"\bjob\.([a-z_]+)", tpl))
    unknown = sorted(referenced - set(JobListItem.model_fields))
    assert not unknown, (
        f"index.html renders job.{{{','.join(unknown)}}} but JobListItem has "
        "no such field — Jinja renders an Undefined as an empty cell, which "
        "is indistinguishable from a null value. Add the field, or stop "
        "rendering it."
    )
