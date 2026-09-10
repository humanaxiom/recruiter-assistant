"""Reconcile a Taleo candidate-roster export against this job's résumés
(Sponsor Requirements PR2 slice 2).

``reconcile_candidate_roster`` is the ONLY caller of
``resume_service.set_work_authorization``/``set_internal_status`` on this
path — the roster CSV never writes those columns directly.

## Matching order

1. **Email hash first** — ``pii.email_hash`` (pure, no decryption) against
   ``resumes.candidate_email_hash``, scoped to ``job_id``.
2. **Name fallback, only for CSV rows that did not match by hash** — against
   ``pii.decrypt``-ed ``candidate_name``, normalised by splitting on any
   non-letter character, lower-casing, dropping empties, and comparing as an
   ORDER-INVARIANT token set (the CSV writes "Last, First"; a parsed résumé
   yields free-form order).

Ambiguous name matches (either direction — one CSV row matching >= 2
résumés, or one résumé matching >= 2 CSV rows) are reported, never resolved
arbitrarily. Two-or-more CSV rows resolving to the SAME résumé with
disagreeing declarations for a field are refused and reported as a conflict
— the grouping happens BEFORE any write, since the write functions are
single-value guarded UPDATEs with no concept of a conflict.

``internal_apsa``/``internal_cupe`` are ``bool | None`` on
``CandidateRosterRow`` (``None`` == the column was absent from THIS export).
A résumé group where every contributing row is ``None`` for a field gets NO
write attempt for that field at all — the field is left exactly as it was.
This is what stops a roster with no APSA/CUPE columns from silently clearing
a flag a PREVIOUS roster legitimately set; see
``bulk_ingest_service.CandidateRosterRow`` for the full reasoning.

No PII, anywhere: neither the report nor the single
``audit_service.record_audit(action="reconcile_candidate_roster", ...)``
call's ``details`` blob ever carries a decrypted name or email — counts and
line numbers/ids only.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from src.services import DbConn, audit_service, resume_service
from src.services import pii as pii_service
from src.services.bulk_ingest_service import CandidateRosterRow
from src.services.pii import email_hash

_JOB_RESUMES_SQL = """
    SELECT id, candidate_email_hash, candidate_name,
           work_authorization, internal_apsa, internal_cupe
      FROM resumes
     WHERE job_id = $1
"""

# Split on any non-letter character (mirrors the module docstring's pinned
# normalisation) — digits, punctuation, and whitespace are all separators.
# ASCII-only BY DESIGN, because the two sides are folded to ASCII first.
_NON_LETTER_RE = re.compile(r"[^A-Za-z]+")


def _normalize_name(name: str) -> frozenset[str]:
    """Lower-case, accent-fold, and split a name into an order-invariant token
    set.

    The accent fold is load-bearing and comes from the real data, not from
    caution. **The two sides of this comparison are encoded differently.**
    Taleo ASCII-folds its export — zero of the 315 rows in the sponsor's real
    roster carry a non-ASCII character — while the résumé side is parsed out
    of the candidate's own PDF and keeps its diacritics. The delivered bundle
    contains exactly that pair: the CSV row reads `an ASCII-folded surname` and the
    résumé reads ``a surname carrying an acute accent``.

    Without the fold, ``[^A-Za-z]+`` treats ``í`` as a SEPARATOR and shatters
    ``that surname`` into ``{d, az}``, so the two spellings of one surname could never
    produce a common token — a mismatch caused entirely by which side of the
    integration a name happened to arrive from. NFKD then dropping combining
    marks maps both spellings onto ``ferran``.

    This only ever makes two names MORE likely to be judged equal, so it
    cannot introduce a false match that strict equality would have refused.
    """
    folded = unicodedata.normalize("NFKD", name.lower())
    stripped = "".join(c for c in folded if not unicodedata.combining(c))
    return frozenset(t for t in _NON_LETTER_RE.split(stripped) if t)


class AmbiguousNameMatch(BaseModel):
    csv_line_nos: list[int]
    resume_ids: list[UUID]


class ConflictingField(BaseModel):
    resume_id: UUID
    field: Literal["work_authorization", "internal_apsa", "internal_cupe"]
    csv_line_nos: list[int]


class RosterReconciliationReport(BaseModel):
    """A response summary, NOT persisted — the same shape ADR-017 already
    uses for bulk ingest (one audit event, no new table)."""

    matched: int
    work_authorization_changed: int
    work_authorization_unchanged: int
    internal_apsa_changed: int
    internal_apsa_unchanged: int
    internal_cupe_changed: int
    internal_cupe_unchanged: int
    unmatched_csv_rows: list[int]
    unmatched_resumes: list[UUID]
    ambiguous_name_matches: list[AmbiguousNameMatch]
    conflicting: list[ConflictingField]
    unrecognised_work_authorization_source: list[int]


async def reconcile_candidate_roster(
    conn: DbConn,
    job_id: UUID,
    rows: list[CandidateRosterRow],
    *,
    actor_kind: str,
    actor_user_id: UUID | None,
    actor_service: str | None,
) -> RosterReconciliationReport:
    resume_records = await conn.fetch(_JOB_RESUMES_SQL, job_id)
    resumes = [dict(r) for r in resume_records]
    resumes_by_id: dict[UUID, dict[str, Any]] = {r["id"]: r for r in resumes}

    by_email_hash: dict[str, UUID] = {
        r["candidate_email_hash"]: r["id"]
        for r in resumes
        if r["candidate_email_hash"] is not None
    }

    # Decrypt every résumé name up front — cheap relative to the network
    # round trip already paid for `conn.fetch`, and it lets the matching
    # logic below stay synchronous.
    resume_name_tokens: dict[UUID, frozenset[str]] = {}
    for r in resumes:
        if r["candidate_name"] is not None:
            plain = await pii_service.decrypt(conn, r["candidate_name"])
            if plain:
                tokens = _normalize_name(plain)
                if tokens:
                    resume_name_tokens[r["id"]] = tokens

    row_by_line: dict[int, CandidateRosterRow] = {row.line_no: row for row in rows}

    # ── step 1: email-hash match ─────────────────────────────────────────
    resolved: dict[int, UUID] = {}
    hash_matched_resume_ids: set[UUID] = set()
    for row in rows:
        h = email_hash(row.email) if row.email else None
        if h is not None and h in by_email_hash:
            resume_id = by_email_hash[h]
            resolved[row.line_no] = resume_id
            hash_matched_resume_ids.add(resume_id)

    # ── step 2: normalised-name fallback, only for what step 1 missed ────
    remaining_lines = [ln for ln in row_by_line if ln not in resolved]
    remaining_resume_tokens = {
        rid: tok
        for rid, tok in resume_name_tokens.items()
        if rid not in hash_matched_resume_ids
    }

    row_to_resumes: dict[int, set[UUID]] = {}
    for ln in remaining_lines:
        row = row_by_line[ln]
        if not row.name:
            continue
        tokens = _normalize_name(row.name)
        if not tokens:
            continue
        matches = {rid for rid, tok in remaining_resume_tokens.items() if tok == tokens}
        if matches:
            row_to_resumes[ln] = matches

    resume_to_rows: dict[UUID, set[int]] = defaultdict(set)
    for ln, rids in row_to_resumes.items():
        for rid in rids:
            resume_to_rows[rid].add(ln)

    ambiguous_line_nos = {ln for ln, rids in row_to_resumes.items() if len(rids) >= 2}

    resume_side_ambiguous: dict[UUID, set[int]] = {}
    for rid, lns in resume_to_rows.items():
        filtered = {ln for ln in lns if ln not in ambiguous_line_nos}
        if len(filtered) >= 2:
            resume_side_ambiguous[rid] = filtered

    ambiguous_name_matches: list[AmbiguousNameMatch] = []
    for ln in sorted(ambiguous_line_nos):
        ambiguous_name_matches.append(
            AmbiguousNameMatch(
                csv_line_nos=[ln],
                resume_ids=sorted(row_to_resumes[ln], key=str),
            )
        )
    for rid in sorted(resume_side_ambiguous, key=str):
        ambiguous_name_matches.append(
            AmbiguousNameMatch(
                csv_line_nos=sorted(resume_side_ambiguous[rid]),
                resume_ids=[rid],
            )
        )

    for ln, rids in row_to_resumes.items():
        if ln in ambiguous_line_nos:
            continue
        (rid,) = rids
        if ln in resume_side_ambiguous.get(rid, set()):
            continue
        resolved[ln] = rid

    ambiguous_all_line_nos = {
        ln for m in ambiguous_name_matches for ln in m.csv_line_nos
    }
    ambiguous_all_resume_ids = {
        rid for m in ambiguous_name_matches for rid in m.resume_ids
    }

    # ── unrecognised work_authorization_source (independent of matching) ─
    unrecognised_work_authorization_source = sorted(
        row.line_no
        for row in rows
        if row.work_authorization == "unknown"
        and row.work_authorization_source is not None
    )

    # ── group matched rows by resolved résumé; grouping happens BEFORE any
    #    write, so a conflict is refused rather than last-written ──────────
    groups: dict[UUID, list[CandidateRosterRow]] = defaultdict(list)
    for ln, rid in resolved.items():
        groups[rid].append(row_by_line[ln])

    conflicting: list[ConflictingField] = []
    wa_changed = wa_unchanged = 0
    apsa_changed = apsa_unchanged = 0
    cupe_changed = cupe_unchanged = 0

    for resume_id, group_rows in groups.items():
        existing = resumes_by_id[resume_id]

        # -- work_authorization --
        non_unknown_wa = {
            r.work_authorization
            for r in group_rows
            if r.work_authorization != "unknown"
        }
        if len(non_unknown_wa) > 1:
            conflicting.append(
                ConflictingField(
                    resume_id=resume_id,
                    field="work_authorization",
                    csv_line_nos=sorted(
                        r.line_no
                        for r in group_rows
                        if r.work_authorization != "unknown"
                    ),
                )
            )
        else:
            resolved_wa = next(iter(non_unknown_wa)) if non_unknown_wa else "unknown"
            if resolved_wa != "unknown":
                await resume_service.set_work_authorization(
                    conn,
                    resume_id,
                    status=resolved_wa,
                    note=None,
                    actor_kind=actor_kind,
                    actor_user_id=actor_user_id,
                    actor_service=actor_service,
                )
                if resolved_wa != existing["work_authorization"]:
                    wa_changed += 1
                else:
                    wa_unchanged += 1
            else:
                wa_unchanged += 1

        # -- internal_apsa / internal_cupe (one guarded call writes both) --
        non_none_apsa = {
            r.internal_apsa for r in group_rows if r.internal_apsa is not None
        }
        non_none_cupe = {
            r.internal_cupe for r in group_rows if r.internal_cupe is not None
        }
        apsa_conflict = len(non_none_apsa) > 1
        cupe_conflict = len(non_none_cupe) > 1
        if apsa_conflict:
            conflicting.append(
                ConflictingField(
                    resume_id=resume_id,
                    field="internal_apsa",
                    csv_line_nos=sorted(
                        r.line_no for r in group_rows if r.internal_apsa is not None
                    ),
                )
            )
        if cupe_conflict:
            conflicting.append(
                ConflictingField(
                    resume_id=resume_id,
                    field="internal_cupe",
                    csv_line_nos=sorted(
                        r.line_no for r in group_rows if r.internal_cupe is not None
                    ),
                )
            )
        if not apsa_conflict and not cupe_conflict:
            resolved_apsa = next(iter(non_none_apsa)) if non_none_apsa else None
            resolved_cupe = next(iter(non_none_cupe)) if non_none_cupe else None
            # Both None means the roster carries NO signal at all for either
            # field this run (either the columns were absent, or every
            # contributing row's cell was absent) — never even attempt the
            # write, so a previously-set flag survives untouched.
            if resolved_apsa is not None or resolved_cupe is not None:
                final_apsa = (
                    resolved_apsa
                    if resolved_apsa is not None
                    else existing["internal_apsa"]
                )
                final_cupe = (
                    resolved_cupe
                    if resolved_cupe is not None
                    else existing["internal_cupe"]
                )
                await resume_service.set_internal_status(
                    conn,
                    resume_id,
                    internal_apsa=final_apsa,
                    internal_cupe=final_cupe,
                    actor_kind=actor_kind,
                    actor_user_id=actor_user_id,
                    actor_service=actor_service,
                )
                if final_apsa != existing["internal_apsa"]:
                    apsa_changed += 1
                else:
                    apsa_unchanged += 1
                if final_cupe != existing["internal_cupe"]:
                    cupe_changed += 1
                else:
                    cupe_unchanged += 1

    matched_resume_ids_final = hash_matched_resume_ids | set(resolved.values())
    unmatched_csv_rows = sorted(
        ln
        for ln in row_by_line
        if ln not in resolved and ln not in ambiguous_all_line_nos
    )
    unmatched_resumes = sorted(
        (
            rid
            for rid in resumes_by_id
            if rid not in matched_resume_ids_final
            and rid not in ambiguous_all_resume_ids
        ),
        key=str,
    )

    report = RosterReconciliationReport(
        matched=len(resolved),
        work_authorization_changed=wa_changed,
        work_authorization_unchanged=wa_unchanged,
        internal_apsa_changed=apsa_changed,
        internal_apsa_unchanged=apsa_unchanged,
        internal_cupe_changed=cupe_changed,
        internal_cupe_unchanged=cupe_unchanged,
        unmatched_csv_rows=unmatched_csv_rows,
        unmatched_resumes=unmatched_resumes,
        ambiguous_name_matches=ambiguous_name_matches,
        conflicting=conflicting,
        unrecognised_work_authorization_source=unrecognised_work_authorization_source,
    )

    await audit_service.record_audit(
        conn,
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        actor_service=actor_service,
        action="reconcile_candidate_roster",
        subject_type="job",
        subject_id=job_id,
        details={
            "matched": report.matched,
            "work_authorization_changed": wa_changed,
            "work_authorization_unchanged": wa_unchanged,
            "internal_apsa_changed": apsa_changed,
            "internal_apsa_unchanged": apsa_unchanged,
            "internal_cupe_changed": cupe_changed,
            "internal_cupe_unchanged": cupe_unchanged,
            "unmatched_csv_rows": unmatched_csv_rows,
            "unmatched_resumes": [str(rid) for rid in unmatched_resumes],
            "ambiguous_name_matches": len(ambiguous_name_matches),
            "conflicting": len(conflicting),
            "unrecognised_work_authorization_source": (
                unrecognised_work_authorization_source
            ),
        },
    )

    return report


__all__ = [
    "AmbiguousNameMatch",
    "ConflictingField",
    "RosterReconciliationReport",
    "reconcile_candidate_roster",
]
