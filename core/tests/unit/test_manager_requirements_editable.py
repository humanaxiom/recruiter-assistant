"""RED — the manager's requirements can be entered on a job that already exists.

**Reported from the running product: "i see no manager skills preference
input".** The feature was reported as done end to end. It was not, and the gap
is total for every job on the pilot box:

* The only input is on the CREATE form. All 23 pilot requisitions arrived
  through the BULK uploader, which has no such field, so not one of them could
  ever have had a note.
* The job page renders the note **read-only** — deliberately, but that leaves
  no way to add one.
* ``PATCH /jobs/{id}`` looks like the escape hatch: ``JobUpdate`` has carried
  ``additional_requirements`` since the field was added. It is **not in
  ``_UPDATABLE_JOB_COLUMNS``**, so ``update_job`` filters it out and returns
  200 having changed nothing. The same "accepted and dropped on the floor"
  shape this branch has now hit three times, pointed a third way.
* ``JobUpdate``'s own comment says *"Editing this re-extracts ONLY the manager
  prompt — it must never re-run the JD parse"*. Nothing implemented that.
  Extraction happens only inside ``parse_job``, so even a working PATCH would
  have left ``additional_requirements_parsed`` describing the previous text.

So four separate pieces, and the last one is the one that makes the difference
between a note that is stored and a note that is SCORED.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from frontend import api_client


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _job(job_id: Any, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": str(job_id),
        "title": "Multimedia Specialist",
        "department": "School of Medicine",
        "location": "Burnaby",
        "min_years": None,
        "status": "open",
        "blind_review": True,
        "parsed_at": "2026-09-01T00:00:00Z",
        "description_parsed": None,
        "failure_reason": None,
        "additional_requirements": None,
        "additional_requirements_parsed": None,
    }
    base.update(over)
    return base


# ── 1. the column is writable at all ────────────────────────────────────────


def test_the_manager_note_is_an_updatable_column() -> None:
    """``JobUpdate`` has accepted this field since it was added and
    ``update_job`` has silently discarded it the whole time — a 200 that
    changed nothing, which is worse than a 422."""
    from src.services.job_service import _UPDATABLE_JOB_COLUMNS

    assert "additional_requirements" in _UPDATABLE_JOB_COLUMNS


def test_the_allowlist_still_refuses_what_it_was_built_to_refuse() -> None:
    """Widening it must not turn it into a passthrough. ``status`` has its own
    state-machine-guarded route and must never be settable here, and the
    extraction blobs are the worker's to write."""
    from src.services.job_service import _UPDATABLE_JOB_COLUMNS

    for forbidden in (
        "status",
        "additional_requirements_parsed",
        "description_parsed",
        "parsed_at",
        "source",
        "external_id",
        "title_provisional",
    ):
        assert forbidden not in _UPDATABLE_JOB_COLUMNS, forbidden


# ── 2. changing it re-extracts, and re-extracts ONLY the note ───────────────


@pytest.mark.asyncio
async def test_the_worker_task_writes_only_the_manager_extraction() -> None:
    """The promise ``JobUpdate``'s comment made and nothing kept. Re-running
    the JD parse here would re-derive the posting's requirements underneath a
    shortlist somebody is reading, which is exactly what editing a note must
    not do."""
    from src.services import job_service

    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    await job_service.record_manager_requirements(conn, uuid4(), None)

    sql = conn.execute.await_args.args[0]
    assert "additional_requirements_parsed" in sql
    for untouched in ("description_parsed", "parsed_at", "status ="):
        assert untouched not in sql, f"{untouched} must not be written: {sql}"


@pytest.mark.asyncio
async def test_the_reextraction_is_not_gated_on_draft() -> None:
    """Unlike ``record_parsed``. A manager adds requirements to a requisition
    that is OPEN and taking résumés — that is the whole point of being able to
    edit it later, and a 'draft'-scoped UPDATE would silently apply to nothing
    on every real job."""
    from src.services import job_service

    conn = MagicMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    await job_service.record_manager_requirements(conn, uuid4(), None)
    assert "'draft'" not in conn.execute.await_args.args[0]


def test_the_task_is_registered_so_it_can_be_enqueued_by_name() -> None:
    from src.worker.main import WorkerSettings
    from src.worker.manager_prompt_task import extract_manager_prompt

    assert extract_manager_prompt in WorkerSettings.functions


# ── 3. the frontend surface ─────────────────────────────────────────────────


def test_the_job_page_offers_an_input_for_the_note(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE report. A job created by bulk upload has no note and no way to
    acquire one."""
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(jid))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])

    html = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert f"/jobs/{job_id}/requirements" in html
    assert 'name="additional_requirements"' in html


def test_the_existing_note_is_prefilled_for_editing(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit box that starts empty is a delete button wearing a disguise."""
    monkeypatch.setattr(
        api_client,
        "get_job",
        lambda jid, **kw: _job(jid, additional_requirements="Must have MEG."),
    )
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    html = client.get(f"/jobs/{uuid4()}").get_data(as_text=True)
    assert "Must have MEG." in html


def test_the_page_says_the_shortlist_needs_regenerating(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the requirements changes the ranking, and the product does NOT
    re-rank on its own — deliberately, because that would move a shortlist
    under someone reading it (ROADMAP §5). Silence here would let a manager
    believe an edit had taken effect on a list that still reflects the old
    note."""
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(jid))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    html = client.get(f"/jobs/{uuid4()}").get_data(as_text=True).lower()
    assert "regenerate" in html


def test_posting_the_note_patches_it_and_returns_to_the_page(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    seen: dict[str, Any] = {}

    def fake(jid: UUID, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        seen["job_id"] = jid
        seen["payload"] = payload
        return _job(jid)

    monkeypatch.setattr(api_client, "patch_job", fake)
    resp = client.post(
        f"/jobs/{job_id}/requirements",
        data={"additional_requirements": "Must have MEG analysis experience."},
    )
    assert resp.status_code == 302
    assert seen["job_id"] == job_id
    assert seen["payload"] == {
        "additional_requirements": "Must have MEG analysis experience."
    }


def test_the_note_route_sends_only_its_own_field(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its own route rather than sharing the department/campus one, because the
    consequences differ: this costs an LLM call and invalidates the shortlist.
    Folding it in would burn an extraction every time somebody fixed a campus.
    A crafted extra field must not ride along either."""
    seen: dict[str, Any] = {}

    def fake(jid: UUID, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        seen["payload"] = payload
        return _job(jid)

    monkeypatch.setattr(api_client, "patch_job", fake)
    client.post(
        f"/jobs/{uuid4()}/requirements",
        data={
            "additional_requirements": "Kafka in production is a bonus.",
            "blind_review": "false",
            "department": "Somewhere Else",
        },
    )
    assert seen["payload"] == {
        "additional_requirements": "Kafka in production is a bonus."
    }


def test_clearing_the_note_sends_null(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "I no longer want this scored" has to be expressible, and it must be
    NULL — the combine reads null as *nobody asked* and marks the sub-score
    unmeasured, where an empty string would assert the manager listed nothing.
    """
    seen: dict[str, Any] = {}

    def fake(jid: UUID, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        seen["payload"] = payload
        return _job(jid)

    monkeypatch.setattr(api_client, "patch_job", fake)
    client.post(f"/jobs/{uuid4()}/requirements", data={"additional_requirements": ""})
    assert seen["payload"] == {"additional_requirements": None}
