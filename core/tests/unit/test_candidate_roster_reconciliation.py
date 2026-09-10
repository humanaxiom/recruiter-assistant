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

**CONTRACT CHANGE from the original slice-1 shape**, made together with this
slice rather than bent to fit it: ``CandidateRosterRow.internal_apsa``/
``internal_cupe`` are now ``bool | None`` — ``None`` means the column was
absent from THIS export; ``False`` means the column was present and this row
declares "not internal"; ``True`` means internal. The earlier "no row in the
whole roster declares the field ``True``" heuristic (inferring absence from
an all-``False`` roster) conflated "the column is absent" with "the column
is present and nobody is internal", and made a wrongly-set flag impossible
to clear by re-uploading a corrected roster. ``None`` says directly what the
heuristic had to guess: when every row contributing to a résumé's group is
``None`` for a field, the reconciler must never attempt to write that field
for that résumé — an unconditional apply would silently CLEAR a flag a
PREVIOUS roster legitimately set — but a declared ``False`` DOES clear a
previously-set ``True``, because that is the point: Taleo exports are
snapshots re-uploaded as applicants trickle in, and a stale ``True``
outliving the roster that set it is a live data-quality bug. This is pinned
directly below.

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
    internal_apsa: bool | None = None,
    internal_cupe: bool | None = None,
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


def _acm(return_value: Any = None) -> MagicMock:
    """An async-context-manager double, matching the helper in
    ``test_internal_status_write_path.py``."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _mock_conn(resume_rows: list[dict[str, Any]]) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetch = AsyncMock(return_value=resume_rows)
    conn.execute = AsyncMock(return_value="SET")
    # The reconciler decrypts names inside ``conn.transaction()`` with the PII
    # key set -- ``app.pii_key`` is transaction-scoped (``set_config(...,
    # is_local => true)``), so a bare-connection decrypt raises
    # ``ExternalRoutineInvocationError`` against real pgcrypto. This double
    # has to model the transaction or the call fails on ``__aenter__``.
    #
    # It cannot VERIFY the fix, though, and that is the point worth
    # remembering: ``pii_service`` is monkeypatched away in these tests, so the
    # decrypt is a no-op here whatever the transaction state. The 503 this
    # guards against is only reachable against a real database --
    # ``tests/integration/test_candidate_roster_reconciliation_pg.py`` is what
    # actually proves it.
    conn.transaction = MagicMock(return_value=_acm())
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
    """Refuse-and-report policy for the internal-employee flags, per FIELD --
    not per pair. This docstring previously claimed "there is no way to apply
    'just the agreed half' through that API shape" -- that was factually
    wrong (2026-09-09 review finding): the implementation twelve lines below
    ``apsa_conflict``/``cupe_conflict`` already fills an untouched field from
    ``existing[...]`` for the ``None`` case, and the same mechanism can fill
    the AGREED field when only its sibling conflicts. This test only
    exercises ONE field conflicting with the other field carrying no signal
    at all (``internal_apsa``-only rows leave ``internal_cupe`` absent, and
    vice versa), so it says nothing about what happens when the two fields
    disagree AND agree at once -- see
    ``test_a_conflict_on_one_internal_flag_does_not_suppress_the_agreed_
    sibling`` below for that case, which the code as shipped gets WRONG."""
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
    """This roster's CSV row carries ``internal_apsa=None``/
    ``internal_cupe=None`` — the "column absent from this export" contract
    (see the module docstring's CONTRACT CHANGE section). The matched résumé
    was PREVIOUSLY marked ``internal_apsa=True, internal_cupe=True`` by an
    earlier roster. Applying ``None`` as though it were a declared ``False``
    would silently CLEAR that — exactly what must never happen: the
    reconciler must not attempt the internal-status write for anyone this
    run when it has no signal at all for that field."""
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
        internal_apsa=None,
        internal_cupe=None,
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


# ── name normalisation: the two sides are encoded differently ────────────


@pytest.mark.parametrize(
    ("csv_spelling", "resume_spelling"),
    [
        ("Ferran", "that surname"),
        ("Delacroix", "Delacroîx"),
        ("Muller", "Müller"),
        ("Nunez", "Núñez"),
        ("Strom", "Ström"),
    ],
)
def test_accented_and_folded_spellings_of_one_surname_normalise_alike(
    csv_spelling: str, resume_spelling: str
) -> None:
    """A surname must not fail to match itself because of which side it came
    from.

    Taleo ASCII-folds its export — zero of the 315 rows in the sponsor's real
    roster carry a non-ASCII byte — while the résumé side is parsed from the
    candidate's own PDF and keeps its diacritics. The delivered bundle holds
    exactly this pair: a surname written without accents in the CSV and with
    an acute accent on the résumé.

    Before the NFKD fold, ``[^A-Za-z]+`` treated a character like ``í`` as a
    separator and shattered such a surname into two meaningless fragments, so
    the two spellings shared no token at all.

    The pairs below are synthetic. Do not substitute the real candidate's
    name from the bundle — see the warning at the top of
    ``docs/pilot-feedback.md``; this file is tracked and the remotes are
    public.
    """
    from src.services.candidate_roster_service import _normalize_name

    assert _normalize_name(csv_spelling) == _normalize_name(resume_spelling)


def test_accent_folding_does_not_collapse_genuinely_different_names() -> None:
    """The fold must only ever make one name match ITSELF across encodings.

    It widens nothing else: two different surnames stay different, so the fold
    cannot introduce a false match that strict set equality would have
    refused.
    """
    from src.services.candidate_roster_service import _normalize_name

    assert _normalize_name("that surname") != _normalize_name("Diez")
    assert _normalize_name("Núñez") != _normalize_name("Nunes")


def test_name_normalisation_is_order_invariant_across_the_two_conventions() -> None:
    """The CSV writes ``Last, First``; a parsed résumé does not. Comparing as
    a set is what keeps the match symmetric rather than privileging either
    source's convention."""
    from src.services.candidate_roster_service import _normalize_name

    assert _normalize_name("Nolan, Bree") == _normalize_name("Bree Nolan")


# ── Major 1 (2026-09-09 review finding): the email path must refuse an ─────
#    ambiguous hash exactly like the name path already refuses an ambiguous
#    name, never collapse two résumés sharing one email hash to "last row
#    wins" out of an ORDER BY-less SELECT.
#
# ``resumes.candidate_email_hash`` carries only a plain partial index (see
# ``core/src/models/ddl.py``) -- the table's only uniqueness is
# ``UNIQUE (job_id, sha256)``, on FILE CONTENT, not on the candidate's email.
# Two résumés in one job CAN legitimately carry the same email hash (the
# Taleo splitter's known duplicate-person mis-split; a candidate re-applying
# with an updated PDF). ADR-047 Consequences says reconciliation "refuses
# rather than guesses" UNCONDITIONALLY, and the ADR-017 amendment promises
# ambiguity is "reported, never resolved arbitrarily" -- neither carve-out
# for "but it's the email path, not the name path" exists in either ADR.


@pytest.mark.asyncio
async def test_duplicate_email_hash_across_two_resumes_matches_neither_and_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two résumés in ONE job share an email hash; one CSV row carries that
    email. The row must match NEITHER résumé, write NOTHING for either, and
    be reported as ambiguous -- never collapse to whichever résumé happened
    to sort last out of an ORDER BY-less ``SELECT``."""
    resume_a = uuid4()
    resume_b = uuid4()
    shared_email = "dup.person@example.invalid"
    resumes = [
        _resume_row(resume_id=resume_a, email=shared_email),
        _resume_row(resume_id=resume_b, email=shared_email),
    ]
    conn = _mock_conn(resumes)
    set_wa, set_internal = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    row = _row(
        2,
        email=shared_email,
        work_authorization="eligible",
        internal_apsa=True,
    )
    await _reconcile(
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
    assert resume_a not in written, (
        "the email-hash path collapsed a 2-résumé collision instead of "
        "refusing it -- last row out of an unordered SELECT won"
    )
    assert resume_b not in written


@pytest.mark.asyncio
async def test_duplicate_email_hash_ambiguity_is_reported_under_its_own_email_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label a human reads must name the EMAIL cause, not the NAME cause
    -- ``report.unmatched_resumes`` is the wrong bucket (it reads as "the
    roster had no row for this person", the opposite of the truth: the
    roster had a row, and it was ambiguous). Pinned as a SIBLING field,
    ``ambiguous_email_matches``, distinct from ``ambiguous_name_matches`` --
    reusing the name bucket would tell a recruiter the wrong thing matched."""
    from src.services.candidate_roster_service import AmbiguousEmailMatch

    resume_a = uuid4()
    resume_b = uuid4()
    shared_email = "dup.person@example.invalid"
    resumes = [
        _resume_row(resume_id=resume_a, email=shared_email),
        _resume_row(resume_id=resume_b, email=shared_email),
    ]
    conn = _mock_conn(resumes)
    _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    row = _row(2, email=shared_email, work_authorization="eligible")
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert report.ambiguous_email_matches, (
        "a colliding email hash must surface under its OWN field, "
        "ambiguous_email_matches -- not silently absorbed into "
        "unmatched_resumes/unmatched_csv_rows, and not folded into the "
        "unrelated ambiguous_name_matches bucket"
    )
    [match] = report.ambiguous_email_matches
    assert isinstance(match, AmbiguousEmailMatch)
    assert match.csv_line_nos == [2]
    assert set(match.resume_ids) == {resume_a, resume_b}
    # The report must never carry the colliding email itself -- see the
    # existing no-PII test below; this only pins the SHAPE, not re-proves
    # no-PII (that is `test_report_and_audit_details_never_contain_pii`).
    assert report.ambiguous_name_matches == []


@pytest.mark.asyncio
async def test_email_hash_ambiguity_does_not_double_report_the_csv_row_as_unmatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row absorbed into ``ambiguous_email_matches`` must not ALSO appear in
    ``unmatched_csv_rows`` -- that would tell a recruiter two contradictory
    things about the same line (mirrors the existing name-side convention:
    ``ambiguous_all_line_nos`` is excluded from ``unmatched_csv_rows``)."""
    resume_a = uuid4()
    resume_b = uuid4()
    shared_email = "dup.person@example.invalid"
    resumes = [
        _resume_row(resume_id=resume_a, email=shared_email),
        _resume_row(resume_id=resume_b, email=shared_email),
    ]
    conn = _mock_conn(resumes)
    _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    row = _row(2, email=shared_email, work_authorization="eligible")
    report = await _reconcile(
        conn,
        uuid4(),
        [row],
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    assert 2 not in report.unmatched_csv_rows
    assert report.matched == 0


@pytest.mark.asyncio
async def test_a_single_resume_per_email_hash_still_matches_by_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION GUARD, not a new behaviour: the ordinary, non-colliding
    case (exactly one résumé per email hash in the job) must keep matching
    exactly as ``test_happy_path_matches_by_email_hash_and_writes_all_three_
    fields`` above already proves. This is expected to PASS ON ARRIVAL --
    it exists so the ambiguity guard this file's other new tests demand
    cannot be implemented by, e.g., refusing every email-hash match
    unconditionally."""
    resume_id = uuid4()
    resume = _resume_row(resume_id=resume_id, email="solo.person@example.invalid")
    conn = _mock_conn([resume])
    set_wa, _ = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    row = _row(2, email="solo.person@example.invalid", work_authorization="eligible")
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


# ── Minor 3 (2026-09-09 review finding): a conflict on ONE internal flag ────
#    must not suppress a write for the OTHER, agreeing flag.


@pytest.mark.asyncio
async def test_a_conflict_on_one_internal_flag_does_not_suppress_the_agreed_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two CSV rows resolve to one résumé. They DISAGREE on ``internal_apsa``
    (conflict, must be refused and reported) but AGREE on ``internal_cupe``
    (``True`` on both -- no conflict at all). The reconciler already fills an
    untouched field from ``existing[...]`` when a field carries NO signal at
    all; the same mechanism must fill the AGREED field here, rather than the
    current ``if not apsa_conflict and not cupe_conflict:`` guard refusing
    BOTH because one of the two disagrees. Today's code writes neither field
    and the report reads ``internal_cupe_changed == internal_cupe_unchanged
    == 0`` -- indistinguishable from "no CUPE signal at all", which is
    false: two rows agreed on it."""
    resume_id = uuid4()
    resume = _resume_row(
        resume_id=resume_id,
        email="mixed-conflict@example.invalid",
        internal_cupe=False,
    )
    conn = _mock_conn([resume])
    _, set_internal = _patch_writes(monkeypatch)
    _patch_audit(monkeypatch)
    _patch_decrypt(monkeypatch, {})

    rows = [
        _row(
            2,
            email="mixed-conflict@example.invalid",
            internal_apsa=True,
            internal_cupe=True,
        ),
        _row(
            14,
            email="mixed-conflict@example.invalid",
            internal_apsa=False,
            internal_cupe=True,
        ),
    ]
    report = await _reconcile(
        conn,
        uuid4(),
        rows,
        actor_kind="user",
        actor_user_id=None,
        actor_service=None,
    )

    apsa_conflicts = [
        c
        for c in report.conflicting
        if c.resume_id == resume_id and c.field == "internal_apsa"
    ]
    assert len(apsa_conflicts) == 1, "the disagreeing field must still be refused"

    cupe_conflicts = [
        c
        for c in report.conflicting
        if c.resume_id == resume_id and c.field == "internal_cupe"
    ]
    assert cupe_conflicts == [], "the agreeing field must never be reported conflicting"

    assert resume_id in _resume_ids_written_by(set_internal), (
        "internal_cupe agreed on by both rows must still be written even "
        "though internal_apsa disagreed -- a conflict on one flag must not "
        "suppress the write for the other"
    )
    call = set_internal.await_args_list[0]
    kwargs = call.kwargs
    assert kwargs.get("internal_cupe") is True
    assert report.internal_cupe_changed == 1
    assert report.internal_apsa_changed == 0
    assert report.internal_apsa_unchanged == 0
