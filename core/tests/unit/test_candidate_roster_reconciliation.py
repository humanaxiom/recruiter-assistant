"""RED — ``reconcile_candidate_roster`` (Sponsor Requirements PR2 slice 2).

The function under test lives in a NEW module,
``src.services.candidate_roster_service``, which does not exist yet — this
whole file fails at collection with an ``ImportError``. That failure IS the
expected RED state for this slice, matching
``tests/unit/test_candidate_roster_csv.py``'s own convention for slice 1.

Signature under test::

    async def reconcile_candidate_roster(
        conn, job_id, rows: list[CandidateRosterRow], *,
        actor_kind, actor_user_id, actor_service,
    ) -> RosterReconciliationReport: ...

## Matching order (pinned by several tests below)

1. **Email hash first**: ``pii.email_hash`` (a PURE function over plaintext —
   no decryption needed) against ``resumes.candidate_email_hash``, scoped to
   ``job_id``.
2. **Then, only for CSV rows that did not match by hash**, a normalised-name
   fallback against ``pii.decrypt``-ed ``candidate_name``. Name
   normalisation: split on any non-letter character, lower-case every token,
   drop empties, compare as an ORDER-INVARIANT token set — the CSV writes
   "Last, First" and a parsed résumé yields free-form order, so a set
   comparison privileges neither source's convention.

## The report contract this file pins for ``RosterReconciliationReport``

A pydantic model, **NOT persisted** — the identical "response summary + one
audit event, no new table" shape ADR-017 already uses for bulk ingest.
Fields:

* ``matched: int`` — résumés a CSV row was matched to (by hash OR name),
  regardless of whether any field actually changed.
* ``work_authorization_changed`` / ``work_authorization_unchanged: int``
* ``internal_apsa_changed`` / ``internal_apsa_unchanged: int``
* ``internal_cupe_changed`` / ``internal_cupe_unchanged: int``
* ``unmatched_csv_rows: list[int]`` — CSV ``line_no``s with no résumé match.
* ``unmatched_resumes: list[UUID]`` — résumé ids in the job with no CSV-row
  match.
* ``ambiguous_name_matches: list[AmbiguousNameMatch]`` — ``AmbiguousNameMatch``
  has ``csv_line_nos: list[int]`` and ``resume_ids: list[UUID]``. Fires when a
  CSV row's token set matches >= 2 résumés, OR a résumé's name matches >= 2
  CSV rows. NEVER carries a name — only line numbers and ids.
* ``conflicting: list[ConflictingField]`` — ``ConflictingField`` has
  ``resume_id: UUID``, ``field: Literal["work_authorization",
  "internal_apsa", "internal_cupe"]``, ``csv_line_nos: list[int]``. Fires when
  two-or-more CSV rows resolve to the SAME résumé with disagreeing
  declarations for that field. **No write happens for a conflicting field —
  never last-wins** — the grouping decision happens BEFORE any write call,
  since ``set_work_authorization``/``set_internal_status`` are single-value
  guarded UPDATEs with no concept of a conflict.
* ``unrecognised_work_authorization_source: list[int]`` — CSV line numbers
  whose ``work_authorization_source`` is a non-``None`` raw string the parser
  didn't recognise (``work_authorization == "unknown"`` AND
  ``work_authorization_source is not None`` — distinct from a genuinely
  blank cell, where the source is ``None``).

**No PII, anywhere.** Neither the report NOR the single
``audit_service.record_audit(action="reconcile_candidate_roster", ...)``
call's ``details`` blob may contain a decrypted candidate name or email —
counts and line numbers/ids only, mirroring ``set_work_authorization``'s own
``details={"status": ...}`` (never a name).

## internal_apsa/internal_cupe and a roster with no APSA/CUPE columns

``CandidateRosterRow.internal_apsa``/``internal_cupe`` are always populated
booleans (``bulk_ingest_service.parse_candidate_csv`` defaults an absent
column to ``False`` per row, identically to a present-but-blank cell — slice
1 is already merged and this file must not touch it). That means the ONLY
signal ``reconcile_candidate_roster`` can use to tell "this roster has no
APSA/CUPE columns at all" from "every candidate in it happens to be
non-internal" is: **no row in the WHOLE roster declares that field ``True``**.
When that holds for a field, the reconciler must never attempt to write it
for anyone this run — an unconditional apply would silently CLEAR a flag a
PREVIOUS roster legitimately set. This is pinned directly below.

All I/O is mocked. ``pii.email_hash`` is a pure function and is used for
real (no mocking needed); ``pii.decrypt`` is monkeypatched at
``src.services.pii.decrypt`` — this codebase's own convention
(``resume_service.py``: ``from src.services import pii as pii_service``) is
that callers reach it via module-attribute access, so patching the attribute
on the ``pii`` module itself is visible to any caller using that convention.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.services import audit_service, resume_service
from src.services import pii as pii_service
from src.services.bulk_ingest_service import CandidateRosterRow
from src.services.pii import email_hash


def _row(
    line_no: int,
    *,
    name: str | None = None,
    email: str | None = None,
    work_authorization: str = "unknown",
    work_authorization_source: str | None = None,
    internal_apsa: bool = False,
    internal_cupe: bool = False,
    sfu_id: str | None = None,
    submission_date: str | None = None,
) -> CandidateRosterRow:
    return CandidateRosterRow(
        line_no=line_no,
        name=name,
        email=email,
        sfu_id=sfu_id,
        work_authorization=work_authorization,  # type: ignore[arg-type]
        work_authorization_source=work_authorization_source,
        internal_apsa=internal_apsa,
        internal_cupe=internal_cupe,
        submission_date=submission_date,
    )


def _resume_row(
    *,
    resume_id: UUID | None = None,
    email: str | None = None,
    name_ciphertext: bytes | None = None,
    work_authorization: str = "unknown",
    internal_apsa: bool = False,
    internal_cupe: bool = False,
) -> dict[str, Any]:
    """A stand-in for one row of whatever ``conn.fetch`` the implementation
    issues to load the job's résumés — a plain dict supports ``row["col"]``
    exactly like an asyncpg ``Record``, so it is agnostic to the actual SQL
    text the implementation writes."""
    return {
        "id": resume_id or uuid4(),
        "candidate_email_hash": email_hash(email),
        "candidate_name": name_ciphertext,
        "work_authorization": work_authorization,
        "internal_apsa": internal_apsa,
        "internal_cupe": internal_cupe,
    }


def _mock_conn(resume_rows: list[dict[str, Any]]) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=resume_rows)
    return conn


def _patch_decrypt(monkeypatch: pytest.MonkeyPatch, mapping: dict[bytes, str]) -> None:
    async def _decrypt(_conn: Any, ciphertext: bytes | None) -> str | None:
        if ciphertext is None:
            return None
        return mapping[ciphertext]

    monkeypatch.setattr(pii_service, "decrypt", AsyncMock(side_effect=_decrypt))


def _patch_writes(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    set_wa = AsyncMock(return_value=True)
    set_internal = AsyncMock(return_value=True)
    monkeypatch.setattr(resume_service, "set_work_authorization", set_wa)
    monkeypatch.setattr(
        resume_service, "set_internal_status", set_internal, raising=False
    )
    return set_wa, set_internal


def _patch_audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []

    async def _record(_conn: Any, **kw: Any) -> None:
        recorded.append(kw)

    monkeypatch.setattr(audit_service, "record_audit", _record)
    return recorded


def _resume_ids_written_by(mock: AsyncMock) -> list[UUID]:
    ids: list[UUID] = []
    for call in mock.await_args_list:
        args = list(call.args) + list(call.kwargs.values())
        for a in args:
            if isinstance(a, UUID):
                ids.append(a)
    return ids


async def _reconcile(conn: Any, job_id: UUID, rows: list[Any], **kwargs: Any) -> Any:
    """Lazy import so a missing ``candidate_roster_service`` module fails EACH
    test individually (a clear ``ModuleNotFoundError`` at call time) instead of
    aborting collection for the WHOLE file — this file has too many
    independent pins to let one missing module mask the rest, unlike
    ``test_candidate_roster_csv.py``'s single-file slice-1 RED state."""
    from src.services import candidate_roster_service

    return await candidate_roster_service.reconcile_candidate_roster(
        conn, job_id, rows, **kwargs
    )


# ── happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_matches_by_email_hash_and_writes_all_three_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    resume = _resume_row(
        resume_id=resume_id,
        email="jordan@example.invalid",
        work_authorization="unknown",
        internal_apsa=False,
        internal_cupe=False,
    )
    conn = _mock_conn([resume])
    set_wa, set_internal = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    row = _row(
        2,
        email="jordan@example.invalid",
        work_authorization="eligible",
        internal_apsa=True,
        internal_cupe=False,
    )
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert report.matched == 1
    assert resume_id in _resume_ids_written_by(set_wa)
    assert resume_id in _resume_ids_written_by(set_internal)
    assert report.unmatched_csv_rows == []
    assert report.unmatched_resumes == []
    assert report.conflicting == []
    assert report.ambiguous_name_matches == []


# ── email-hash vs. name-fallback matching order ─────────────────────────


@pytest.mark.asyncio
async def test_falls_back_to_name_match_only_when_email_hash_matches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    name_cipher = b"cipher-bree-nolan"
    resume = _resume_row(resume_id=resume_id, email=None, name_ciphertext=name_cipher)
    conn = _mock_conn([resume])
    set_wa, _ = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {name_cipher: "Nolan, Bree"})

    row = _row(
        2,
        email="no-match-anywhere@example.invalid",
        name="Bree Nolan",
        work_authorization="eligible",
    )
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert report.matched == 1
    assert resume_id in _resume_ids_written_by(set_wa)
    assert report.unmatched_csv_rows == []
    assert report.unmatched_resumes == []


@pytest.mark.asyncio
async def test_name_normalisation_matches_last_comma_first_against_free_form_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact pair the spec pins: the CSV's "Last, First" convention must
    match a résumé's free-form "First Last" via the order-invariant token
    set, not a literal string comparison."""
    resume_id = uuid4()
    name_cipher = b"cipher-bree-nolan-freeform"
    resume = _resume_row(resume_id=resume_id, email=None, name_ciphertext=name_cipher)
    conn = _mock_conn([resume])
    set_wa, _ = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {name_cipher: "Bree Nolan"})

    row = _row(2, name="Nolan, Bree", work_authorization="eligible")
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert report.matched == 1
    assert resume_id in _resume_ids_written_by(set_wa)
    assert report.unmatched_csv_rows == []


@pytest.mark.asyncio
async def test_email_hash_match_wins_when_a_name_match_would_point_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both an email match (résumé A) and a name match (résumé B) would fire
    for this CSV row, and they point at DIFFERENT résumés — the email-hash
    match must win. Because the row already matched by hash, the name
    fallback is never even consulted for it, so résumé B is left unmatched."""
    resume_a = uuid4()
    resume_b = uuid4()
    cipher_a = b"cipher-unrelated-name"
    cipher_b = b"cipher-target-name"
    resumes = [
        _resume_row(
            resume_id=resume_a,
            email="alex@example.invalid",
            name_ciphertext=cipher_a,
        ),
        _resume_row(resume_id=resume_b, email=None, name_ciphertext=cipher_b),
    ]
    conn = _mock_conn(resumes)
    set_wa, _ = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(
        monkeypatch,
        {cipher_a: "Zed, Unrelated", cipher_b: "Smith, Alex"},
    )

    row = _row(
        2,
        email="alex@example.invalid",
        name="Alex Smith",
        work_authorization="eligible",
    )
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    written = _resume_ids_written_by(set_wa)
    assert resume_a in written
    assert resume_b not in written
    assert resume_b in report.unmatched_resumes


# ── conflicting duplicate CSV rows ──────────────────────────────────────


@pytest.mark.asyncio
async def test_conflicting_work_authorization_is_refused_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real Taleo export contains exactly this (the "Bree Nolan" row,
    twice, with disagreeing declarations). Two CSV rows resolving to the SAME
    résumé with disagreeing ``work_authorization`` must result in NO write at
    all for that résumé's field — never last-wins — and the résumé surfaces
    in ``report.conflicting``."""
    resume_id = uuid4()
    resume = _resume_row(resume_id=resume_id, email="bree.nolan@example.invalid")
    conn = _mock_conn([resume])
    set_wa, _ = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    rows = [
        _row(2, email="bree.nolan@example.invalid", work_authorization="eligible"),
        _row(14, email="bree.nolan@example.invalid", work_authorization="not_eligible"),
    ]
    report = await _reconcile(
        conn,
        uuid4(),
        rows,
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert resume_id not in _resume_ids_written_by(set_wa)
    conflicts = [
        c
        for c in report.conflicting
        if c.resume_id == resume_id and c.field == "work_authorization"
    ]
    assert len(conflicts) == 1
    assert sorted(conflicts[0].csv_line_nos) == [2, 14]
    assert resume_id not in report.unmatched_resumes


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["internal_apsa", "internal_cupe"])
async def test_conflicting_internal_flag_is_refused_and_reported(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same refuse-and-report policy for the internal-employee flags. Because
    ``set_internal_status`` writes BOTH flags in one guarded call, a conflict
    on EITHER sub-field blocks the whole call for that résumé this run — there
    is no way to apply "just the agreed half" through that API shape, and
    guessing would risk writing a value nobody actually agreed on."""
    resume_id = uuid4()
    resume = _resume_row(resume_id=resume_id, email="dup@example.invalid")
    conn = _mock_conn([resume])
    _, set_internal = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    if field == "internal_apsa":
        row_a = _row(2, email="dup@example.invalid", internal_apsa=True)
        row_b = _row(14, email="dup@example.invalid", internal_apsa=False)
    else:
        row_a = _row(2, email="dup@example.invalid", internal_cupe=True)
        row_b = _row(14, email="dup@example.invalid", internal_cupe=False)
    rows = [row_a, row_b]
    report = await _reconcile(
        conn,
        uuid4(),
        rows,
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert resume_id not in _resume_ids_written_by(set_internal)
    conflicts = [
        c for c in report.conflicting if c.resume_id == resume_id and c.field == field
    ]
    assert len(conflicts) == 1
    assert sorted(conflicts[0].csv_line_nos) == [2, 14]


# ── ambiguous name matches, both directions ─────────────────────────────


@pytest.mark.asyncio
async def test_ambiguous_name_match_a_csv_row_matches_two_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_a = uuid4()
    resume_b = uuid4()
    cipher_a = b"cipher-a"
    cipher_b = b"cipher-b"
    resumes = [
        _resume_row(resume_id=resume_a, email=None, name_ciphertext=cipher_a),
        _resume_row(resume_id=resume_b, email=None, name_ciphertext=cipher_b),
    ]
    conn = _mock_conn(resumes)
    set_wa, set_internal = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {cipher_a: "Nolan, Bree", cipher_b: "Bree Nolan"})

    row = _row(2, name="Bree Nolan", work_authorization="eligible")
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    written = set(_resume_ids_written_by(set_wa)) | set(
        _resume_ids_written_by(set_internal)
    )
    assert resume_a not in written
    assert resume_b not in written
    matches = [m for m in report.ambiguous_name_matches if 2 in m.csv_line_nos]
    assert len(matches) == 1
    assert set(matches[0].resume_ids) == {resume_a, resume_b}


@pytest.mark.asyncio
async def test_ambiguous_name_match_reverse_a_resume_matches_two_csv_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    cipher = b"cipher-only"
    resume = _resume_row(resume_id=resume_id, email=None, name_ciphertext=cipher)
    conn = _mock_conn([resume])
    set_wa, _ = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {cipher: "Nolan, Bree"})

    rows = [
        _row(2, name="Bree Nolan", work_authorization="eligible"),
        _row(9, name="nolan bree", work_authorization="not_eligible"),
    ]
    report = await _reconcile(
        conn,
        uuid4(),
        rows,
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert resume_id not in _resume_ids_written_by(set_wa)
    matches = [m for m in report.ambiguous_name_matches if resume_id in m.resume_ids]
    assert len(matches) == 1
    assert set(matches[0].csv_line_nos) == {2, 9}


# ── nothing silently dropped, in either direction ───────────────────────


@pytest.mark.asyncio
async def test_unmatched_csv_rows_and_unmatched_resumes_both_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unmatched_resume = uuid4()
    cipher = b"cipher-resume-only"
    resume = _resume_row(resume_id=unmatched_resume, email=None, name_ciphertext=cipher)
    conn = _mock_conn([resume])
    _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {cipher: "Nobody, Here"})

    row = _row(2, email="nobody-matches@example.invalid", name="Totally Different")
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert report.unmatched_csv_rows == [2]
    assert report.unmatched_resumes == [unmatched_resume]
    assert report.matched == 0


# ── unrecognised work_authorization_source ──────────────────────────────


@pytest.mark.asyncio
async def test_unrecognised_work_authorization_source_surfaces_in_its_own_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume_id = uuid4()
    resume = _resume_row(resume_id=resume_id, email="pending@example.invalid")
    conn = _mock_conn([resume])
    _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    row = _row(
        2,
        email="pending@example.invalid",
        work_authorization="unknown",
        work_authorization_source="Pending Review",
    )
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert report.unrecognised_work_authorization_source == [2]


@pytest.mark.asyncio
async def test_a_genuinely_blank_work_authorization_cell_is_not_unrecognised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank cell (``work_authorization_source is None``) is a different
    thing entirely from a string the parser didn't understand — it must NOT
    land in the unrecognised bucket, or a recruiter would be told the tool
    "didn't understand" a value that was simply never supplied."""
    resume_id = uuid4()
    resume = _resume_row(resume_id=resume_id, email="blank@example.invalid")
    conn = _mock_conn([resume])
    _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    row = _row(
        2,
        email="blank@example.invalid",
        work_authorization="unknown",
        work_authorization_source=None,
    )
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert report.unrecognised_work_authorization_source == []


# ── a roster with no APSA/CUPE columns must never clear existing flags ───


@pytest.mark.asyncio
async def test_roster_with_no_apsa_cupe_signal_never_writes_internal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every row in this roster carries ``internal_apsa=False`` and
    ``internal_cupe=False`` — indistinguishable, from ``CandidateRosterRow``
    alone, from "these columns weren't in the CSV at all". The matched
    résumé was PREVIOUSLY marked ``internal_apsa=True, internal_cupe=True`` by
    an earlier roster. Applying this roster's flags unconditionally would
    silently CLEAR that — exactly what must never happen: the reconciler must
    not attempt the internal-status write for anyone this run."""
    resume_id = uuid4()
    resume = _resume_row(
        resume_id=resume_id,
        email="already-internal@example.invalid",
        internal_apsa=True,
        internal_cupe=True,
    )
    conn = _mock_conn([resume])
    set_wa, set_internal = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    row = _row(
        2,
        email="already-internal@example.invalid",
        work_authorization="eligible",
        internal_apsa=False,
        internal_cupe=False,
    )
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert resume_id not in _resume_ids_written_by(set_internal)
    assert report.internal_apsa_changed == 0
    assert report.internal_cupe_changed == 0
    # work_authorization is an independent field and is unaffected by the
    # internal-status heuristic — it still applies normally.
    assert resume_id in _resume_ids_written_by(set_wa)


# ── audit + no-PII ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exactly_one_audit_event_is_recorded_per_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume = _resume_row(email="solo@example.invalid")
    conn = _mock_conn([resume])
    _patch_writes(monkeypatch)
    recorded = _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    row = _row(2, email="solo@example.invalid", work_authorization="eligible")
    await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert len(recorded) == 1
    assert recorded[0]["action"] == "reconcile_candidate_roster"


@pytest.mark.asyncio
async def test_report_and_audit_details_never_contain_pii(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the report nor the audit ``details`` blob may carry a
    decrypted candidate name or email — counts and line numbers only."""
    resume_id = uuid4()
    cipher = b"cipher-secret-name"
    resume = _resume_row(resume_id=resume_id, email=None, name_ciphertext=cipher)
    conn = _mock_conn([resume])
    _patch_writes(monkeypatch)
    recorded = _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {cipher: "Secret Candidate Name"})

    row = _row(2, name="Secret Candidate Name", email="secret@example.invalid")
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    report_blob = report.model_dump_json()
    assert "Secret Candidate Name" not in report_blob
    assert "secret@example.invalid" not in report_blob

    details_blob = str(recorded[0].get("details", {}))
    assert "Secret Candidate Name" not in details_blob
    assert "secret@example.invalid" not in details_blob


@pytest.mark.asyncio
async def test_the_resume_query_is_scoped_to_the_given_job_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _mock_conn([])
    _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    job_id = uuid4()
    await _reconcile(
        conn,
        job_id,
        [],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    conn.fetch.assert_awaited()
    flat = [
        a
        for call in conn.fetch.await_args_list
        for a in list(call.args) + list(call.kwargs.values())
    ]
    assert job_id in flat
