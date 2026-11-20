"""Slice C1 (RED) -- ``src.services.split_request_service``: the persistence and
state machine behind the in-app Taleo split. The module does not exist yet, so
this file fails at import. Unit tests mock all I/O; the real SQL behaviour is
pinned in ``tests/integration/test_split_requests_pg.py``.

API pinned here (every function takes the ``DbConn`` first; ``blob_store`` is a
``BlobStore``; all keyword-only unless shown)::

    SOURCE_PREFIX = "split_sources/"
    STATUSES = ("queued","segmenting","proposed","accepted","discarded","failed")
    LEGAL_TRANSITIONS: dict[str, frozenset[str]]
    METHODS = ("llm", "heuristic")
    FALLBACK_REASONS: frozenset[str]   # fixed enum strings, never free text
    FAILURE_REASONS: frozenset[str]    # static strings, never free text

    async def create(conn, blob_store, *, job_id, filename, data,
                     consent_acknowledged, created_by) -> UUID
    async def transition(conn, split_id, *, expected, to) -> bool   # CAS
    async def claim(conn, split_id) -> SplitClaim | None  # CAS queued->segmenting
        # SplitClaim has attrs: id, job_id, source_blob_key, blind_review
        # (blind_review is joined from ``jobs`` in the same statement)
    async def store_proposal(conn, split_id, proposal, *, diagnostics, method,
                             fallback_reason, blind_review, page_count) -> bool
        # CAS segmenting->proposed, proposal pgp-encrypted via src.services.pii
    async def load_proposal(conn, split_id) -> SplitProposal | None
    async def fail(conn, blob_store, split_id, reason) -> bool
        # CAS queued|segmenting -> failed; blob deleted; key+proposal NULL
    async def discard(conn, blob_store, split_id) -> bool
        # CAS queued|proposed|failed -> discarded; blob deleted; key+proposal NULL
    async def mark_accepted(conn, blob_store, split_id, *, accepted_by,
                            ingest_result) -> bool
        # CAS proposed -> accepted; blob deleted; key+proposal NULL
    async def requeue_stale(conn, *, minutes) -> list[UUID]
        # queued/segmenting older than ``minutes``: segmenting reset to queued;
        # returns the ids to re-run heuristic-only
    async def expire(conn, blob_store, *, ttl_hours=None) -> int
        # proposed/queued/failed older than ttl (default settings
        # split_proposal_ttl_h) -> discarded; returns count
    async def sweep_orphan_blobs(conn, blob_store) -> int
        # deletes split_sources/* blobs no row references (one ``conn.fetch``
        # whose rows expose ``source_blob_key``)

The module imports ``pii`` (``from src.services import pii``) and
``get_settings`` by those names so tests can patch them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from src.schemas.taleo_split import (
    ProposalApplicant,
    ProposalDiagnostics,
    SplitProposal,
)
from src.services import split_request_service as srs

_KEY_RE = re.compile(r"^split_sources/[0-9a-f]{8}-[0-9a-f-]{27}\.pdf$")


def _acm() -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _conn(*, won: bool = True) -> MagicMock:
    row = {
        "id": uuid4(),
        "job_id": uuid4(),
        "source_blob_key": "split_sources/x.pdf",
        "blind_review": False,
    }
    conn = MagicMock(name="conn")
    conn.fetchrow = AsyncMock(return_value=row if won else None)
    conn.fetchval = AsyncMock(return_value=row["id"] if won else None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="UPDATE 1" if won else "UPDATE 0")
    conn.transaction = MagicMock(return_value=_acm())
    return conn


def _store() -> MagicMock:
    store = MagicMock(name="blob_store")
    store.put = AsyncMock(return_value=None)
    store.delete = AsyncMock(return_value=None)
    store.list_keys = AsyncMock(return_value=[])
    return store


def _calls(conn: MagicMock) -> list[Any]:
    out: list[Any] = []
    for name in ("execute", "fetchval", "fetchrow", "fetch"):
        out.extend(getattr(conn, name).await_args_list)
    return out


def _all_args(conn: MagicMock) -> list[Any]:
    return [a for c in _calls(conn) for a in c.args]


def _sqls(conn: MagicMock) -> list[str]:
    return [a for a in _all_args(conn) if isinstance(a, str) and " " in a]


@contextmanager
def _fake_pii(plaintexts: list[str] | None = None) -> Iterator[MagicMock]:
    async def _enc(_conn: Any, text: str | None) -> bytes | None:
        if plaintexts is not None and text is not None:
            plaintexts.append(text)
        return None if text is None else b"CIPHERTEXT"

    manager = MagicMock()
    manager.set_key = AsyncMock()
    with (
        patch.object(srs.pii, "set_pii_key", manager.set_key),
        patch.object(srs.pii, "encrypt", AsyncMock(side_effect=_enc)),
        patch.object(
            srs.pii,
            "decrypt",
            AsyncMock(return_value=SplitProposal().model_dump_json()),
        ),
    ):
        yield manager


def _proposal(name: str = "") -> SplitProposal:
    return SplitProposal(
        applicants=[
            ProposalApplicant(
                candidate_name=name, resume_pages=[1, 2], cover_letter_pages=[3]
            ),
            ProposalApplicant(candidate_name=name, resume_pages=[4]),
        ]
    )


def _diag() -> ProposalDiagnostics:
    return ProposalDiagnostics()


async def _create(conn: MagicMock, store: MagicMock, **over: Any) -> UUID:
    kw: dict[str, Any] = {
        "job_id": uuid4(),
        "filename": "a.pdf",
        "data": b"%PDF-1.4 x",
        "consent_acknowledged": True,
        "created_by": None,
    }
    kw.update(over)
    return await srs.create(conn, store, **kw)


# -- constants ---------------------------------------------------------------


def test_statuses_and_prefix_are_the_planned_ones() -> None:
    assert srs.SOURCE_PREFIX == "split_sources/"
    assert srs.STATUSES == (
        "queued",
        "segmenting",
        "proposed",
        "accepted",
        "discarded",
        "failed",
    )


def test_legal_transitions_are_exactly_the_planned_state_machine() -> None:
    assert srs.LEGAL_TRANSITIONS == {
        "queued": frozenset({"segmenting", "failed", "discarded"}),
        "segmenting": frozenset({"proposed", "failed", "queued"}),
        "proposed": frozenset({"accepted", "discarded"}),
        "failed": frozenset({"discarded"}),
        "accepted": frozenset(),
        "discarded": frozenset(),
    }


def test_fallback_and_failure_reasons_are_fixed_enums() -> None:
    assert srs.FALLBACK_REASONS == frozenset(
        {"llm_unavailable", "llm_invalid_output", "llm_error", "requeued_after_stall"}
    )
    assert srs.FAILURE_REASONS == frozenset(
        {
            "unreadable_pdf",
            "encrypted_pdf",
            "no_text_layer",
            "too_many_pages",
            "source_missing",
        }
    )
    assert srs.METHODS == ("llm", "heuristic")


# -- create ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_puts_the_blob_under_a_server_generated_key() -> None:
    conn, store = _conn(), _store()
    await _create(conn, store, filename="../../evil name.pdf")
    store.put.assert_awaited_once()
    key = store.put.await_args.args[0]
    assert _KEY_RE.match(key), key
    assert "evil" not in key


@pytest.mark.asyncio
async def test_create_key_stem_is_the_returned_row_id() -> None:
    conn, store = _conn(), _store()
    split_id = await _create(conn, store)
    assert isinstance(split_id, UUID)
    assert store.put.await_args.args[0] == f"split_sources/{split_id}.pdf"


@pytest.mark.asyncio
async def test_create_without_consent_raises_before_any_io() -> None:
    conn, store = _conn(), _store()
    with pytest.raises(ValueError):
        await _create(conn, store, consent_acknowledged=False)
    store.put.assert_not_awaited()
    assert _calls(conn) == []


@pytest.mark.asyncio
async def test_create_records_sha256_and_creator_and_consent() -> None:
    conn, store = _conn(), _store()
    data = b"%PDF-1.4 distinct"
    await _create(conn, store, data=data, created_by="rec@example.com")
    args = _all_args(conn)
    assert hashlib.sha256(data).hexdigest() in args
    assert "rec@example.com" in args
    assert True in args


@pytest.mark.asyncio
async def test_create_inserts_a_queued_row() -> None:
    conn, store = _conn(), _store()
    await _create(conn, store)
    sql = " ".join(_sqls(conn)).lower()
    assert "insert into resume_split_requests" in sql
    assert "queued" in sql or "queued" in _all_args(conn)


# -- transition (compare-and-set) --------------------------------------------


@pytest.mark.asyncio
async def test_transition_returns_true_when_the_cas_wins() -> None:
    conn = _conn(won=True)
    assert await srs.transition(conn, uuid4(), expected="queued", to="segmenting")


@pytest.mark.asyncio
async def test_transition_returns_false_when_the_cas_loses() -> None:
    conn = _conn(won=False)
    assert not await srs.transition(conn, uuid4(), expected="queued", to="segmenting")


@pytest.mark.asyncio
async def test_transition_sql_is_an_update_guarded_on_the_expected_status() -> None:
    conn = _conn()
    await srs.transition(conn, uuid4(), expected="proposed", to="discarded")
    sql = " ".join(_sqls(conn)).lower()
    assert "update resume_split_requests" in sql
    assert re.search(r"where[^;]*status\s*=", sql)
    assert "'proposed'" in sql or "proposed" in _all_args(conn)


@pytest.mark.parametrize(
    ("expected", "to"),
    [
        ("accepted", "queued"),
        ("discarded", "proposed"),
        ("proposed", "queued"),
        ("failed", "proposed"),
        ("queued", "accepted"),
        ("queued", "proposed"),
        ("segmenting", "accepted"),
        ("proposed", "failed"),
    ],
)
@pytest.mark.asyncio
async def test_transition_refuses_illegal_edges_without_touching_the_db(
    expected: str, to: str
) -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        await srs.transition(conn, uuid4(), expected=expected, to=to)
    assert _calls(conn) == []


@pytest.mark.asyncio
async def test_transition_refuses_an_unknown_status() -> None:
    conn = _conn()
    with pytest.raises(ValueError):
        await srs.transition(conn, uuid4(), expected="queued", to="bogus")


# -- claim -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_returns_the_row_facts_the_worker_needs() -> None:
    claim = await srs.claim(_conn(won=True), uuid4())
    assert claim is not None
    assert claim.source_blob_key == "split_sources/x.pdf"
    assert claim.blind_review is False
    assert isinstance(claim.job_id, UUID)


@pytest.mark.asyncio
async def test_claim_is_none_when_another_worker_already_claimed() -> None:
    assert await srs.claim(_conn(won=False), uuid4()) is None


@pytest.mark.asyncio
async def test_claim_sql_moves_queued_to_segmenting_and_joins_blind_review() -> None:
    conn = _conn()
    await srs.claim(conn, uuid4())
    sql = " ".join(_sqls(conn)).lower()
    assert "segmenting" in sql and "queued" in sql
    assert "blind_review" in sql


# -- store_proposal ----------------------------------------------------------


async def _store_proposal(
    conn: MagicMock, proposal: SplitProposal, **over: Any
) -> bool:
    kw: dict[str, Any] = {
        "diagnostics": _diag(),
        "method": "llm",
        "fallback_reason": None,
        "blind_review": False,
        "page_count": 4,
    }
    kw.update(over)
    return await srs.store_proposal(conn, uuid4(), proposal, **kw)


@pytest.mark.asyncio
async def test_store_proposal_encrypts_the_proposal_not_plaintext() -> None:
    conn = _conn()
    plain: list[str] = []
    with _fake_pii(plain):
        assert await _store_proposal(conn, _proposal())
    assert len(plain) == 1
    parsed = SplitProposal.model_validate_json(plain[0])
    assert parsed.applicants[0].resume_pages == [1, 2]
    args = _all_args(conn)
    assert b"CIPHERTEXT" in args
    assert not any(isinstance(a, str) and "resume_pages" in a for a in args)


@pytest.mark.asyncio
async def test_store_proposal_sets_the_pii_key_inside_a_transaction() -> None:
    conn = _conn()
    with _fake_pii() as pii_mock:
        await _store_proposal(conn, _proposal())
    pii_mock.set_key.assert_awaited()
    conn.transaction.assert_called()


@pytest.mark.asyncio
async def test_store_proposal_blind_review_never_persists_guessed_names() -> None:
    conn = _conn()
    plain: list[str] = []
    with _fake_pii(plain):
        await _store_proposal(conn, _proposal(name="Pat Example"), blind_review=True)
    assert "Pat Example" not in plain[0]
    names = [
        a.candidate_name for a in SplitProposal.model_validate_json(plain[0]).applicants
    ]
    assert names == ["", ""]


@pytest.mark.asyncio
async def test_store_proposal_non_blind_keeps_names_it_was_given() -> None:
    conn = _conn()
    plain: list[str] = []
    with _fake_pii(plain):
        await _store_proposal(conn, _proposal(name="Pat Example"), blind_review=False)
    assert "Pat Example" in plain[0]


@pytest.mark.asyncio
async def test_store_proposal_diagnostics_are_plain_non_pii_json() -> None:
    conn = _conn()
    diag = ProposalDiagnostics(missing=[7], merged=[2], cover_only=[3])
    with _fake_pii():
        await _store_proposal(conn, _proposal(), diagnostics=diag)
    found: dict[str, Any] | None = None
    for a in _all_args(conn):
        if isinstance(a, str) and a.startswith("{"):
            found = json.loads(a)
        elif isinstance(a, dict):
            found = a
    assert found is not None
    assert found["missing"] == [7]
    assert found["merged"] == [2]
    assert set(found) == set(ProposalDiagnostics.model_fields)


@pytest.mark.asyncio
async def test_store_proposal_is_false_when_the_cas_loses() -> None:
    with _fake_pii():
        assert not await _store_proposal(_conn(won=False), _proposal())


@pytest.mark.asyncio
async def test_store_proposal_sql_guards_on_segmenting() -> None:
    conn = _conn()
    with _fake_pii():
        await _store_proposal(conn, _proposal())
    sql = " ".join(_sqls(conn)).lower()
    assert "segmenting" in sql and "proposed" in sql


@pytest.mark.parametrize(
    ("method", "reason"),
    [
        ("llm", "llm_error"),  # llm path must not carry a fallback reason
        ("heuristic", None),  # heuristic must say why
        ("heuristic", "the model said: Pat Example <pat@example.com>"),
        ("magic", None),
    ],
)
@pytest.mark.asyncio
async def test_store_proposal_refuses_inconsistent_method_and_reason(
    method: str, reason: str | None
) -> None:
    conn = _conn()
    with _fake_pii(), pytest.raises(ValueError):
        await _store_proposal(conn, _proposal(), method=method, fallback_reason=reason)
    assert _calls(conn) == []


@pytest.mark.asyncio
async def test_store_proposal_accepts_every_fixed_fallback_reason() -> None:
    for reason in sorted(srs.FALLBACK_REASONS):
        with _fake_pii():
            assert await _store_proposal(
                _conn(), _proposal(), method="heuristic", fallback_reason=reason
            )


# -- load_proposal -----------------------------------------------------------


@pytest.mark.asyncio
async def test_load_proposal_decrypts_and_parses() -> None:
    conn = _conn()
    conn.fetchval = AsyncMock(return_value=b"CIPHERTEXT")
    conn.fetchrow = AsyncMock(return_value={"proposal": b"CIPHERTEXT"})
    stored = _proposal()
    with (
        patch.object(srs.pii, "set_pii_key", AsyncMock()),
        patch.object(
            srs.pii, "decrypt", AsyncMock(return_value=stored.model_dump_json())
        ),
    ):
        out = await srs.load_proposal(conn, uuid4())
    assert out == stored


@pytest.mark.asyncio
async def test_load_proposal_is_none_when_the_proposal_was_nulled() -> None:
    conn = _conn()
    conn.fetchval = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value={"proposal": None})
    with (
        patch.object(srs.pii, "set_pii_key", AsyncMock()),
        patch.object(srs.pii, "decrypt", AsyncMock(return_value=None)),
    ):
        assert await srs.load_proposal(conn, uuid4()) is None


# -- fail / discard / mark_accepted ------------------------------------------


@pytest.mark.asyncio
async def test_fail_refuses_free_text_reasons() -> None:
    conn, store = _conn(), _store()
    with pytest.raises(ValueError):
        await srs.fail(conn, store, uuid4(), "Traceback: Pat Example resume.pdf")
    assert _calls(conn) == []
    store.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_fail_with_a_static_reason_returns_true() -> None:
    assert await srs.fail(_conn(), _store(), uuid4(), "encrypted_pdf")


@pytest.mark.asyncio
async def test_fail_lost_cas_does_not_delete_a_blob() -> None:
    conn, store = _conn(won=False), _store()
    assert not await srs.fail(conn, store, uuid4(), "unreadable_pdf")
    store.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_discard_lost_cas_returns_false_and_keeps_the_blob() -> None:
    conn, store = _conn(won=False), _store()
    assert not await srs.discard(conn, store, uuid4())
    store.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_accepted_lost_cas_returns_false_and_keeps_the_blob() -> None:
    conn, store = _conn(won=False), _store()
    ok = await srs.mark_accepted(
        conn, store, uuid4(), accepted_by="rec", ingest_result=[]
    )
    assert not ok
    store.delete.assert_not_awaited()


# -- sweep_orphan_blobs ------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_only_lists_and_deletes_under_the_split_prefix() -> None:
    conn, store = _conn(), _store()
    store.list_keys = AsyncMock(
        return_value=["split_sources/a.pdf", "resumes/keepme.pdf"]
    )
    n = await srs.sweep_orphan_blobs(conn, store)
    store.list_keys.assert_awaited_once()
    assert store.list_keys.await_args.args[0] == "split_sources/"
    deleted = [c.args[0] for c in store.delete.await_args_list]
    assert deleted == ["split_sources/a.pdf"]
    assert n == 1


@pytest.mark.asyncio
async def test_sweep_keeps_blobs_a_row_still_references() -> None:
    conn, store = _conn(), _store()
    store.list_keys = AsyncMock(
        return_value=["split_sources/live.pdf", "split_sources/orphan.pdf"]
    )
    conn.fetch = AsyncMock(return_value=[{"source_blob_key": "split_sources/live.pdf"}])
    n = await srs.sweep_orphan_blobs(conn, store)
    deleted = [c.args[0] for c in store.delete.await_args_list]
    assert deleted == ["split_sources/orphan.pdf"]
    assert n == 1


@pytest.mark.asyncio
async def test_sweep_with_nothing_on_disk_deletes_nothing() -> None:
    conn, store = _conn(), _store()
    assert await srs.sweep_orphan_blobs(conn, store) == 0
    store.delete.assert_not_awaited()


# -- requeue_stale / expire --------------------------------------------------


@pytest.mark.asyncio
async def test_requeue_stale_returns_the_ids_to_rerun() -> None:
    a, b = uuid4(), uuid4()
    conn = _conn()
    conn.fetch = AsyncMock(return_value=[{"id": a}, {"id": b}])
    assert await srs.requeue_stale(conn, minutes=30) == [a, b]


@pytest.mark.asyncio
async def test_requeue_stale_with_no_stale_rows_is_empty() -> None:
    conn = _conn()
    conn.fetch = AsyncMock(return_value=[])
    assert await srs.requeue_stale(conn, minutes=30) == []


@pytest.mark.asyncio
async def test_expire_defaults_its_ttl_to_the_split_proposal_setting() -> None:
    conn, store = _conn(), _store()
    conn.fetch = AsyncMock(return_value=[])
    settings = SimpleNamespace(split_proposal_ttl_h=5)
    with patch.object(srs, "get_settings", return_value=settings):
        n = await srs.expire(conn, store)
    assert n == 0
    flat = [a for c in _calls(conn) for a in c.args]
    assert any(a in (5, 5.0) or "5" in str(a) for a in flat)
