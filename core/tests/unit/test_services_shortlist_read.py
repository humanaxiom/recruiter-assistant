"""Unit tests for ``src.services.shortlist_service.list_for_job`` /
``get_one`` — the Phase 5 read + blind-review redaction boundary for the
shortlist. All I/O is mocked (a bare ``MagicMock`` connection, following the
``_Row``/``_acm`` conventions in ``test_worker_shortlist_job.py`` /
``test_pii.py``); the same contract proven against a real Postgres lives in
``tests/integration/test_shortlist_read_export_pg.py``.

``src.services.shortlist_service.list_for_job`` / ``.get_one`` do not exist
yet — RED half of the TDD cycle; every test below fails, either at collection
(``ImportError`` — the two names aren't exported yet) or at the first
``await`` (``AttributeError``).

Column-alias contract these mocks assume for the BLIND branch (ported from
hris ``shortlist_service._BLIND_COLS``): the SQL the implementation issues
under ``jobs.blind_review = TRUE`` must alias the joined ``resumes`` row's
decrypted PII / parsed json as ``_c_name`` / ``_c_email`` / ``_c_phone`` /
``_c_parsed`` so the read layer can redact them out of the evidence text
before they are ever assigned onto a response field.

THE TWO LOAD-BEARING TESTS IN THIS FILE:

1. ``test_score_breakdown_fold_does_not_break_read`` — ``persist_shortlist``
   (4d) folds ``score_structured``/``score_evidence`` INTO the
   ``score_breakdown`` jsonb it writes (ADR-010 §2). ``ScoreBreakdown`` has
   ``model_config = ConfigDict(extra="forbid")``, so a naive
   ``ScoreBreakdown.model_validate(row["score_breakdown"])`` on ANY row 4d
   ever wrote RAISES ``pydantic.ValidationError``. The read layer MUST strip
   the two folded keys before validating. If a future edit reverts to a bare
   ``.model_validate()`` call, THIS test is what catches it.
2. ``test_blind_entry_redacts_cover_letter_evidence_and_motivation_text`` —
   hris's ``_redact_evidence`` redacts ``requirements[].evidence`` /
   ``overall_summary`` but NEVER touches ``cover_letter_evidence[].evidence``
   / ``overall_motivation`` — a real gap: a cover-letter quote can carry the
   candidate's own name. This project's read layer MUST close that gap (a
   must-fix-beyond-verbatim-port, not an accepted residual).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.errors import NotFoundError
from src.schemas.matching import EvidenceObject, ScoreBreakdown


class _Row(dict[str, Any]):
    """Dict-like fake asyncpg Record: an absent key returns ``None`` instead
    of raising ``KeyError``."""

    def __getitem__(self, key: str) -> Any:
        return dict.get(self, key)


def _acm(return_value: Any = None) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _breakdown_dict() -> dict[str, Any]:
    return ScoreBreakdown(
        skill=0.7,
        experience=0.6,
        education=0.5,
        seniority=0.5,
        vector=0.4,
        structured=0.6,
    ).model_dump()


def _folded_breakdown_dict() -> dict[str, Any]:
    """Exactly what ``persist_shortlist`` writes to ``score_breakdown``: a
    full ``ScoreBreakdown.model_dump()`` PLUS the two folded keys."""
    raw = ScoreBreakdown(
        skill=0.8,
        experience=0.6,
        education=0.4,
        seniority=0.5,
        vector=0.3,
        structured=0.55,
        motivation=0.1,
    ).model_dump()
    raw["score_structured"] = 0.77
    raw["score_evidence"] = 0.66
    return raw


def _entry_row(
    *,
    entry_id: UUID | None = None,
    job_id: UUID,
    resume_id: UUID | None = None,
    rank: int = 1,
    score_breakdown: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    generated_at: dt.datetime | None = None,
) -> _Row:
    return _Row(
        {
            "id": entry_id or uuid4(),
            "job_id": job_id,
            "resume_id": resume_id or uuid4(),
            "rank": rank,
            "score_final": 0.9,
            "score_breakdown": json.dumps(score_breakdown or _breakdown_dict()),
            "evidence": json.dumps(evidence if evidence is not None else {}),
            "generated_at": generated_at or dt.datetime(2026, 7, 15, tzinfo=dt.UTC),
        }
    )


def _blind_entry_row(
    *,
    entry_id: UUID | None = None,
    job_id: UUID,
    resume_id: UUID | None = None,
    rank: int = 1,
    score_breakdown: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    name: str | None = "Jane Smith",
    email: str | None = "jane.smith@example.test",
    phone: str | None = "604-555-0192",
    parsed: dict[str, Any] | None = None,
    generated_at: dt.datetime | None = None,
) -> _Row:
    row = _entry_row(
        entry_id=entry_id,
        job_id=job_id,
        resume_id=resume_id,
        rank=rank,
        score_breakdown=score_breakdown,
        evidence=evidence,
        generated_at=generated_at,
    )
    row["_c_name"] = name
    row["_c_email"] = email
    row["_c_phone"] = phone
    row["_c_parsed"] = json.dumps(parsed) if parsed is not None else None
    return row


def _mock_conn(
    *,
    blind: bool,
    rows: list[_Row] | None = None,
    row: _Row | None = None,
) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.fetchval = AsyncMock(return_value=blind)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_acm())
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.fetchrow = AsyncMock(return_value=row)
    return conn


# ── landmine 1: the ScoreBreakdown fold read guard ──────────────────────────


@pytest.mark.asyncio
async def test_score_breakdown_fold_does_not_break_read() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    folded = _folded_breakdown_dict()
    row = _entry_row(job_id=job_id, score_breakdown=folded, evidence={})
    conn = _mock_conn(blind=False, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry.score_breakdown, ScoreBreakdown)
    assert entry.score_breakdown.skill == pytest.approx(0.8)
    assert entry.score_breakdown.experience == pytest.approx(0.6)
    assert entry.score_breakdown.structured == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_score_breakdown_fold_does_not_break_read_via_get_one() -> None:
    """Same landmine, proven through get_one's own row-to-model path (a
    separate code path from list_for_job in most implementations)."""
    from src.services.shortlist_service import get_one

    entry_id = uuid4()
    folded = _folded_breakdown_dict()
    row = _entry_row(
        job_id=uuid4(), entry_id=entry_id, score_breakdown=folded, evidence={}
    )
    conn = _mock_conn(blind=False, row=row)

    entry = await get_one(conn, entry_id)

    assert isinstance(entry.score_breakdown, ScoreBreakdown)
    assert entry.score_breakdown.skill == pytest.approx(0.8)


# ── landmine 2 setup: the evidence={} residual (documented, not a bug) ──────


@pytest.mark.asyncio
async def test_shortlist_evidence_empty_dict_deserializes_not_none() -> None:
    """``shortlist_entries.evidence == {}`` is the 4d DELETE-first coercion
    for a ``NOT NULL`` column (``persist_shortlist``'s ``evidence=None -> {}``
    rule). The read layer must deserialize ``{}`` into a valid, empty-fielded
    ``EvidenceObject``, NOT ``None`` — and it does NOT (and structurally
    CANNOT) disambiguate "never evidence-scored" from "scored, found nothing"
    at this layer. That information loss is an ACCEPTED ADR-010 §2 residual,
    not a bug this test is meant to catch."""
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    row = _entry_row(job_id=job_id, evidence={})
    conn = _mock_conn(blind=False, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    assert entries[0].evidence is not None
    assert isinstance(entries[0].evidence, EvidenceObject)
    assert entries[0].evidence.requirements == []
    assert entries[0].evidence.overall_summary == ""


# ── non-blind vs blind branch ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_blind_job_returns_real_evidence_unblinded() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    real_evidence = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "Jane Smith built the payments service.",
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            }
        ],
        "overall_summary": "Strong candidate.",
    }
    row = _entry_row(job_id=job_id, rank=1, evidence=real_evidence)
    conn = _mock_conn(blind=False, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    entry = entries[0]
    assert entry.blinded is False
    assert entry.display_label is None
    assert entry.evidence is not None
    assert "Jane Smith" in entry.evidence.requirements[0].evidence
    assert entry.evidence.overall_summary == "Strong candidate."


@pytest.mark.asyncio
async def test_blind_job_masks_identity_and_labels_display_by_rank() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    evidence_1 = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "Jane Smith led the migration at Acme Corp.",
                "evidence_chunk_ids": ["c_001"],
                "confidence": 0.9,
            }
        ],
        "overall_summary": "Jane Smith is a strong candidate.",
    }
    parsed_1 = {
        "candidate": {
            "name": "Jane Smith",
            "email": "jane.smith@example.test",
            "phone": "604-555-0192",
            "location": None,
        },
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [{"company": "Acme Corp", "title": "Engineer", "bullets": []}],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    evidence_2 = {"requirements": [], "overall_summary": "Bob Lee is also strong."}
    parsed_2 = {
        "candidate": {"name": "Bob Lee"},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }

    row1 = _blind_entry_row(
        job_id=job_id,
        rank=1,
        evidence=evidence_1,
        parsed=parsed_1,
        name="Jane Smith",
        email="jane.smith@example.test",
        phone="604-555-0192",
    )
    row2 = _blind_entry_row(
        job_id=job_id,
        rank=2,
        evidence=evidence_2,
        parsed=parsed_2,
        name="Bob Lee",
        email=None,
        phone=None,
    )
    conn = _mock_conn(blind=True, rows=[row1, row2])

    entries = await list_for_job(conn, job_id=job_id)

    assert entries[0].blinded is True
    assert entries[0].display_label == "Candidate A"
    assert entries[1].blinded is True
    assert entries[1].display_label == "Candidate B"

    assert entries[0].evidence is not None
    req = entries[0].evidence.requirements[0]
    assert "Jane Smith" not in req.evidence
    assert "jane.smith@example.test" not in req.evidence
    assert "Acme Corp" not in req.evidence
    assert "Employer A" in req.evidence
    assert "Jane Smith" not in entries[0].evidence.overall_summary


# ── landmine 2: cover-letter evidence redaction gap, CLOSED ─────────────────


@pytest.mark.asyncio
async def test_blind_entry_redacts_cover_letter_evidence_and_motivation_text() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    evidence = {
        "requirements": [],
        "cover_letter_presence": True,
        "cover_letter_evidence": [
            {
                "theme": "motivation",
                "evidence": "I, Jane Smith, am drawn to this role.",
                "evidence_chunk_ids": ["cl_001"],
                "confidence": 0.8,
            }
        ],
        "overall_motivation": "Jane Smith is genuinely motivated by the mission.",
    }
    parsed = {
        "candidate": {"name": "Jane Smith"},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    row = _blind_entry_row(
        job_id=job_id, rank=1, evidence=evidence, parsed=parsed, name="Jane Smith"
    )
    conn = _mock_conn(blind=True, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)

    ev = entries[0].evidence
    assert ev is not None
    assert "Jane Smith" not in ev.cover_letter_evidence[0].evidence
    assert "Jane Smith" not in ev.overall_motivation


@pytest.mark.asyncio
async def test_black_box_scan_no_pii_in_blind_shortlist_entry() -> None:
    """Black-box guard on the FULL serialized ``ShortlistEntry`` — catches a
    masking call applied AFTER DTO construction (only some fields nulled) as
    well as an outright missing masking call, not just the individually
    checked fields above."""
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    real_name = "Zzyzxqrst Wibblesworth"
    real_email = "zzyzxqrst.wibblesworth@example.test"
    real_phone = "778-555-0199"
    evidence = {
        "requirements": [
            {
                "requirement": "X",
                "status": "met",
                "evidence": f"{real_name} built the whole thing.",
                "evidence_chunk_ids": [],
                "confidence": 0.9,
            }
        ],
        "cover_letter_evidence": [
            {
                "theme": "motivation",
                "evidence": f"Contact {real_name} at {real_email} or {real_phone}.",
                "evidence_chunk_ids": [],
                "confidence": 0.7,
            }
        ],
        "overall_summary": f"{real_name} is impressive.",
        "overall_motivation": f"{real_name} really wants this job.",
    }
    parsed = {
        "candidate": {"name": real_name, "email": real_email, "phone": real_phone},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    row = _blind_entry_row(
        job_id=job_id,
        rank=1,
        evidence=evidence,
        parsed=parsed,
        name=real_name,
        email=real_email,
        phone=real_phone,
    )
    conn = _mock_conn(blind=True, rows=[row])

    entries = await list_for_job(conn, job_id=job_id)
    dumped = entries[0].model_dump_json()

    assert real_name not in dumped
    assert real_email not in dumped
    assert real_phone not in dumped


# ── empty job / get_one branches ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_job_returns_empty_list_not_error() -> None:
    from src.services.shortlist_service import list_for_job

    job_id = uuid4()
    conn = _mock_conn(blind=False, rows=[])

    entries = await list_for_job(conn, job_id=job_id)

    assert entries == []


@pytest.mark.asyncio
async def test_get_one_nonexistent_entry_raises_not_found() -> None:
    from src.services.shortlist_service import get_one

    conn = _mock_conn(blind=False, row=None)
    # blind lookup on a missing entry -> NULL from the join
    conn.fetchval = AsyncMock(return_value=None)

    with pytest.raises(NotFoundError):
        await get_one(conn, uuid4())


@pytest.mark.asyncio
async def test_get_one_non_blind_returns_real_entry() -> None:
    from src.services.shortlist_service import get_one

    entry_id = uuid4()
    row = _entry_row(job_id=uuid4(), entry_id=entry_id, evidence={"requirements": []})
    conn = _mock_conn(blind=False, row=row)

    entry = await get_one(conn, entry_id)

    assert entry.id == entry_id
    assert entry.blinded is False
    assert entry.display_label is None


@pytest.mark.asyncio
async def test_get_one_blind_returns_masked_entry() -> None:
    from src.services.shortlist_service import get_one

    entry_id = uuid4()
    parsed = {
        "candidate": {"name": "Jane Smith"},
        "summary": "",
        "total_years_experience": 0,
        "skills": [],
        "experience": [],
        "education": [],
        "chunks": [],
        "cover_letter_chunks": [],
    }
    evidence = {
        "requirements": [
            {
                "requirement": "Python",
                "status": "met",
                "evidence": "Jane Smith did great work.",
                "evidence_chunk_ids": [],
                "confidence": 0.8,
            }
        ]
    }
    row = _blind_entry_row(
        job_id=uuid4(),
        entry_id=entry_id,
        rank=3,
        evidence=evidence,
        parsed=parsed,
        name="Jane Smith",
    )
    conn = _mock_conn(blind=True, row=row)

    entry = await get_one(conn, entry_id)

    assert entry.blinded is True
    assert entry.display_label == "Candidate C"
    assert entry.evidence is not None
    assert "Jane Smith" not in entry.evidence.requirements[0].evidence
