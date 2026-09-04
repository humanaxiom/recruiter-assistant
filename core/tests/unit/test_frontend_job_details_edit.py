"""RED — the UI has to be able to fill and override Department and Campus.

The second half of the sponsor's 2026-09-03 request: *"have the fields in the
UI to override/fill"*. The parse half is worth little on its own, because the
JDs on the pilot box mostly do not state a campus — one of 26 mentions one at
all, and it mentions two. **A human typing it is the primary way this column
gets filled**, not the fallback.

Before this there was nowhere to do it. Department and Location exist on the
CREATE form only, so the 23 requisitions already uploaded as files were
permanently stuck: the backend ``PATCH /jobs/{id}`` has accepted both columns
since Phase 6 and no screen ever called it.

The route is deliberately thin — it forwards to ``api_client.patch_job`` and
lets the backend own validation, because a second copy of the rules in the
frontend is how the two silently diverge. Anti-forgery needs no code here:
``_csrf_gate`` is opt-OUT, so a new POST route is guarded the moment it exists
(``test_frontend_csrf_covers_every_write_route.py`` proves it).
"""

from __future__ import annotations

from typing import Any
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
        "department": None,
        "location": None,
        "min_years": None,
        "status": "draft",
        "blind_review": True,
        "parsed_at": "2026-09-01T00:00:00Z",
        "description_parsed": None,
        "failure_reason": None,
    }
    base.update(over)
    return base


# ── the form is on the page ─────────────────────────────────────────────────


def test_the_detail_page_offers_a_department_and_campus_form(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(jid))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])

    html = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert f"/jobs/{job_id}/details" in html
    assert 'name="department"' in html
    assert 'name="location"' in html


def test_the_campus_options_come_from_the_canonicaliser(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form and ``src.campus`` must not be able to disagree about what a
    campus is — one hard-coded list in a template is one that goes stale."""
    from src.campus import CAMPUS_CODES

    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(jid))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])

    html = client.get(f"/jobs/{uuid4()}").get_data(as_text=True)
    for campus in CAMPUS_CODES:
        assert f'value="{campus}"' in html, campus


def test_the_create_form_offers_the_same_campus_options(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both surfaces, one vocabulary. The jobs list renders the create form."""
    from src.campus import CAMPUS_CODES

    monkeypatch.setattr(api_client, "list_jobs", lambda **kw: [])
    html = client.get("/").get_data(as_text=True)
    assert 'list="campus-options"' in html
    for campus in CAMPUS_CODES:
        assert f'value="{campus}"' in html, campus


def test_a_provisional_title_is_explained_on_the_page(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """23 pilot requisitions are called things like "20260612 00138559 APSA
    JDFN 20260612". A recruiter seeing that needs to know it came from the
    uploaded filename, not that somebody typed it."""
    monkeypatch.setattr(
        api_client,
        "get_job",
        lambda jid, **kw: _job(
            jid, title="20260612 00138559 APSA JDFN 20260612", title_provisional=True
        ),
    )
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    html = client.get(f"/jobs/{uuid4()}").get_data(as_text=True)
    assert "filename" in html.lower()


def test_a_chosen_title_gets_no_such_note(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        api_client, "get_job", lambda jid, **kw: _job(jid, title_provisional=False)
    )
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    html = client.get(f"/jobs/{uuid4()}").get_data(as_text=True)
    assert "uploaded filename" not in html.lower()


# ── POST /jobs/<id>/details ─────────────────────────────────────────────────


def test_the_route_patches_both_fields_and_returns_to_the_page(
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
        f"/jobs/{job_id}/details",
        data={"department": "School of Medicine", "location": "bby"},
    )
    assert resp.status_code == 302
    assert str(job_id) in resp.headers["Location"]
    assert seen["job_id"] == job_id
    assert seen["payload"] == {
        "department": "School of Medicine",
        "location": "bby",
    }


def test_the_route_sends_the_value_unnormalised(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "bby" goes to the backend as typed. Canonicalisation lives in
    ``JobUpdate`` (one place, shared with the manifest and a raw API caller);
    a second implementation here is how the two drift apart. Only the
    surrounding whitespace a browser sends is stripped."""
    seen: dict[str, Any] = {}

    def fake(jid: UUID, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        seen.update(payload)
        return _job(jid)

    monkeypatch.setattr(api_client, "patch_job", fake)
    client.post(f"/jobs/{uuid4()}/details", data={"location": "  bby  "})
    assert seen["location"] == "bby"


def test_clearing_a_field_sends_null_not_an_empty_string(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recruiter emptying the box means "unset". An empty string would sit in
    the column as a value — and ``record_parsed``'s ``NULLIF`` exists to
    tolerate exactly that, so do not create more of it."""
    seen: dict[str, Any] = {}

    def fake(jid: UUID, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        seen.update(payload)
        return _job(jid)

    monkeypatch.setattr(api_client, "patch_job", fake)
    client.post(f"/jobs/{uuid4()}/details", data={"department": "", "location": ""})
    assert seen == {"department": None, "location": None}


def test_a_field_the_form_did_not_submit_is_not_patched(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH omit means "unchanged" (``JobUpdate``'s whole convention). Sending
    every key on every post would let this route clobber a column it was never
    asked about."""
    seen: dict[str, Any] = {}

    def fake(jid: UUID, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        seen["payload"] = payload
        return _job(jid)

    monkeypatch.setattr(api_client, "patch_job", fake)
    client.post(f"/jobs/{uuid4()}/details", data={"location": "Surrey"})
    assert seen["payload"] == {"location": "Surrey"}


def test_the_route_404s_when_the_job_does_not_exist(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(jid: UUID, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        raise api_client.NotFound("no such job")

    monkeypatch.setattr(api_client, "patch_job", fake)
    resp = client.post(f"/jobs/{uuid4()}/details", data={"location": "x"})
    assert resp.status_code == 404


def test_the_route_surfaces_a_403_rather_than_500ing(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-033: every backend write 403s a non-writer CAS session. A view that
    leaves that uncaught turns a hiring manager's click into a 500 — the same
    defect ``transition_status`` and ``reparse_job`` both had."""

    def fake(jid: UUID, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        raise api_client.BadRequest("forbidden", status_code=403, detail="forbidden")

    monkeypatch.setattr(api_client, "patch_job", fake)
    resp = client.post(f"/jobs/{uuid4()}/details", data={"location": "x"})
    assert resp.status_code == 403


def test_an_empty_submission_does_not_reach_the_backend(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing submitted is nothing to do. ``update_job`` tolerates an empty
    payload, but a round trip that cannot change anything should not spend one
    — and it must not bump ``updated_at`` on a job nobody edited, which is now
    a column the list renders."""
    called: dict[str, Any] = {}

    def fake(jid: UUID, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
        called["yes"] = True
        return _job(jid)

    monkeypatch.setattr(api_client, "patch_job", fake)
    resp = client.post(f"/jobs/{uuid4()}/details", data={})
    assert resp.status_code == 302
    assert not called
