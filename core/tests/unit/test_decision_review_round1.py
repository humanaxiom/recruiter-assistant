"""RED -- human decision record, review round 1.

F1  CRLF/CR normalisation of the rationale (browsers submit CRLF).
F2  ``record`` takes a per-(job, resume) transaction advisory lock first.
F3  the shortlist-cards fragment (poll + generate) carries the decision badges.
F4  blind-review jobs show a "do not include candidate identity" hint.
F5  rationale rejects C1 controls, zero-width and bidi characters.

Invisible characters are built with ``chr()`` only -- never written literally.
"""

from __future__ import annotations

import importlib
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from frontend import api_client
from src.schemas.decisions import DecisionIn
from src.services import audit_service
from tests.unit import test_decision_service as _svc_t
from tests.unit import test_frontend_decision_panel as _fe

_OK = "Strong fit for the role."


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _rat(text: str) -> str:
    return str(DecisionIn(decision="accept", rationale=text).rationale)


# -- F1: CRLF / CR ----------------------------------------------------------


def test_crlf_rationale_validates_and_is_stored_as_lf() -> None:
    out = _rat("Strong fit for the role.\r\nNeeds visa sponsorship.")
    assert out == "Strong fit for the role.\nNeeds visa sponsorship."
    assert "\r" not in out


def test_lone_cr_becomes_lf() -> None:
    assert _rat("Line one is here.\rLine two is here.") == (
        "Line one is here.\nLine two is here."
    )


def test_cr_followed_by_crlf_becomes_two_lfs() -> None:
    assert _rat("Line one is here.\r\r\nLine two is here.") == (
        "Line one is here.\n\nLine two is here."
    )


def test_crlf_counts_as_one_character_for_the_minimum() -> None:
    # 9 chars after normalisation (raw length is 10): still too short.
    with pytest.raises(ValidationError):
        DecisionIn(decision="accept", rationale="1234567\r\n9")
    # 10 chars after normalisation (raw length is 11): valid.
    assert _rat("12345678\r\n9") == "12345678\n9"


def test_crlf_counts_as_one_character_for_the_maximum() -> None:
    assert len(_rat("a" * 499 + "\r\n" + "b" * 500)) == 1000
    with pytest.raises(ValidationError):
        DecisionIn(decision="accept", rationale="a" * 500 + "\r\n" + "b" * 500)


def test_trailing_crlf_is_stripped() -> None:
    assert _rat(_OK + "\r\n\r\n") == _OK


@pytest.mark.parametrize("code", [0, 1, 7, 8, 11, 12, 27, 127])
def test_other_c0_controls_are_still_rejected_beside_crlf(code: int) -> None:
    with pytest.raises(ValidationError):
        text = "Good fit" + chr(13) + chr(10) + chr(code) + " for the team"
        DecisionIn(decision="accept", rationale=text)


def test_flask_post_with_crlf_rationale_calls_backend_and_redirects(
    monkeypatch: Any, client: Any
) -> None:
    eid = uuid4()
    rec = _fe._Recorder()
    _fe._install(monkeypatch, rec)
    resp = _fe._post(client, eid, rationale="Strong fit for the role.\r\nNeeds a visa.")
    assert resp.status_code in (302, 303)
    assert resp.headers["Location"].endswith(f"/shortlist/{eid}")
    assert len(rec.calls) == 1
    with client.session_transaction() as session:
        assert not session.get("_flashes")


# -- F2: advisory lock ------------------------------------------------------


def _traced_conn(entry_rows: list[Any]) -> MagicMock:
    conn = _svc_t._conn(fetchrow=entry_rows)
    trace: list[tuple[str, str, tuple[Any, ...]]] = []
    conn.trace = trace

    def wrap(name: str, inner: AsyncMock) -> AsyncMock:
        async def _call(sql: str, *args: Any) -> Any:
            trace.append((name, re.sub(r"\s+", " ", sql).lower(), args))
            return await inner(sql, *args)

        return AsyncMock(side_effect=_call)

    for name in ("execute", "fetchval", "fetchrow"):
        setattr(conn, name, wrap(name, getattr(conn, name)))
    return conn


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(audit_service, "record_audit", mock)
    return mock


def _svc() -> Any:
    return importlib.import_module("src.services.decision_service")


@pytest.mark.asyncio
async def test_record_takes_an_xact_advisory_lock_before_reading_latest(
    audit: AsyncMock,
) -> None:
    entry = _svc_t._entry()
    conn = _traced_conn([None, _svc_t._row(entry)])
    await _svc().record(conn, entry=entry, user=_svc_t._user(), body=_svc_t._body())
    assert "pg_advisory_xact_lock" in conn.trace[0][1]
    assert conn.trace[0][0] in ("execute", "fetchval")
    first_read = next(i for i, t in enumerate(conn.trace) if t[0] == "fetchrow")
    assert first_read > 0


@pytest.mark.asyncio
async def test_lock_is_taken_inside_the_transaction(audit: AsyncMock) -> None:
    entry = _svc_t._entry()
    conn = _traced_conn([None, _svc_t._row(entry)])
    seen: list[str] = []
    original = conn.fetchval

    async def _spy(sql: str, *args: Any) -> Any:
        seen.append(conn.events[-1] if conn.events else "none")
        return await original(sql, *args)

    original_exec = conn.execute

    async def _spy_exec(sql: str, *args: Any) -> Any:
        seen.append(conn.events[-1] if conn.events else "none")
        return await original_exec(sql, *args)

    conn.fetchval = AsyncMock(side_effect=_spy)
    conn.execute = AsyncMock(side_effect=_spy_exec)
    await _svc().record(conn, entry=entry, user=_svc_t._user(), body=_svc_t._body())
    assert seen and seen[0] == "txn_enter"


@pytest.mark.asyncio
async def test_lock_key_is_per_pair_stable_and_distinct(audit: AsyncMock) -> None:
    a, b = _svc_t._entry(), _svc_t._entry()
    keys: list[tuple[Any, ...]] = []
    for entry in (a, a, b):
        conn = _traced_conn([_svc_t._row(entry)])
        await _svc().record(conn, entry=entry, user=_svc_t._user(), body=_svc_t._body())
        keys.append(conn.trace[0][2])
    assert keys[0] == keys[1]
    assert keys[0] != keys[2]


@pytest.mark.asyncio
async def test_exact_repeat_path_still_takes_the_lock(audit: AsyncMock) -> None:
    entry = _svc_t._entry()
    conn = _traced_conn([_svc_t._row(entry)])
    _out, created = await _svc().record(
        conn, entry=entry, user=_svc_t._user(), body=_svc_t._body()
    )
    assert created is False
    assert "pg_advisory_xact_lock" in conn.trace[0][1]


# -- F3: badges survive the HTMX swap ---------------------------------------


def _two_entries(job_id: UUID) -> tuple[list[Any], UUID]:
    r1 = uuid4()
    entry = _fe._entry(uuid4(), job_id=job_id, resume_id=r1, label="Candidate One")
    return [entry], r1


def test_poll_fragment_renders_the_decision_badge(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries, r1 = _two_entries(job_id)
    _fe._job_page_stubs(monkeypatch, job_id, entries)
    mock = _fe._stub_latest(monkeypatch, [_fe._badge(r1, "reject", "poll-reviewer")])
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    assert resp.status_code == 200
    mock.assert_called_once()
    assert mock.call_args.args[0] == job_id
    body = resp.get_data(as_text=True)
    assert "poll-reviewer" in body
    assert re.search(r"reject", body, re.IGNORECASE)


def test_generate_response_fragment_renders_the_decision_badge(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries, r1 = _two_entries(job_id)
    _fe._job_page_stubs(monkeypatch, job_id, entries)
    monkeypatch.setattr(api_client, "generate_shortlist", MagicMock(return_value={}))
    _fe._stub_latest(monkeypatch, [_fe._badge(r1, "hold", "gen-reviewer")])
    resp = client.post(f"/jobs/{job_id}/shortlist")
    assert resp.status_code == 200
    assert "gen-reviewer" in resp.get_data(as_text=True)


@pytest.mark.parametrize(
    "exc",
    [
        api_client.BackendUnavailable("down"),
        api_client.NotFound("gone"),
        api_client.BadRequest("403", status_code=403, detail={"detail": "no"}),
    ],
)
def test_poll_fragment_badge_fetch_failure_renders_cards_without_badge(
    monkeypatch: Any, client: Any, exc: Exception
) -> None:
    job_id = uuid4()
    entries, _r1 = _two_entries(job_id)
    _fe._job_page_stubs(monkeypatch, job_id, entries)
    mock = _fe._stub_latest(monkeypatch, exc)
    resp = client.get(f"/jobs/{job_id}/shortlist-cards")
    assert resp.status_code == 200
    mock.assert_called_once()
    assert "Candidate One" in resp.get_data(as_text=True)


def test_poll_fragment_with_no_decisions_has_no_badge(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    entries, _r1 = _two_entries(job_id)
    _fe._job_page_stubs(monkeypatch, job_id, entries)
    mock = _fe._stub_latest(monkeypatch, [])
    body = client.get(f"/jobs/{job_id}/shortlist-cards").get_data(as_text=True)
    mock.assert_called_once()
    assert "Human decision:" not in body


# -- F4: blind-review hint --------------------------------------------------

_HINT = re.compile(r"do not include.{0,120}(identif|name)", re.IGNORECASE | re.DOTALL)


def _form_for(monkeypatch: Any, client: Any, *, blinded: bool) -> str:
    eid = uuid4()
    _fe._stub_entry_page(monkeypatch, eid, _fe._history(_fe._decision()))
    monkeypatch.setattr(
        api_client,
        "get_shortlist_entry",
        MagicMock(return_value=_fe._entry(eid, blinded=blinded)),
    )
    form = _fe._decision_form(_fe._page(client, eid), eid)
    assert form is not None
    return form


def test_blind_job_decision_form_tells_reviewer_not_to_include_identity(
    monkeypatch: Any, client: Any
) -> None:
    assert _HINT.search(_form_for(monkeypatch, client, blinded=True))


def test_non_blind_job_decision_form_has_no_identity_hint(
    monkeypatch: Any, client: Any
) -> None:
    form = _form_for(monkeypatch, client, blinded=False)
    assert "do not include" not in form.lower()
    assert not _HINT.search(form)


# -- F5: invisible / C1 / bidi characters -----------------------------------

_C1 = [0x80, 0x85, 0x9F]
_ZERO_WIDTH = list(range(0x200B, 0x2010))
_BIDI = list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))


@pytest.mark.parametrize("code", _C1 + _ZERO_WIDTH + _BIDI, ids=lambda c: f"U+{c:04X}")
def test_invisible_and_c1_characters_are_rejected(code: int) -> None:
    with pytest.raises(ValidationError):
        DecisionIn(decision="accept", rationale=f"Good fit {chr(code)}for the team")


def test_ordinary_non_ascii_text_is_still_allowed() -> None:
    text = "Solid " + chr(0xE9) + "quipe fit; lead " + chr(0x2014) + " good."
    assert _rat(text) == text
