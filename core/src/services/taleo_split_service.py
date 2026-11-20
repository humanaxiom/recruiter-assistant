"""Pure Taleo combined-export split logic, shared by the app and the CLI.

No database, no network, no filesystem: everything here is a function of PDF
bytes / per-page text / a proposal. ``scripts/split_taleo_pdf.py`` re-exports
the moved helpers so the CLI and the in-app split flow run ONE implementation.

Invariants enforced by :func:`validate_proposal` (recomputed server-side from
the stored PDF, never trusted from the model): every page is accounted for
exactly once, and no applicant's résumé pages carry two distinct emails.
Names are never put into emitted filenames or the pairing manifest.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection

import fitz  # type: ignore[import-untyped]

from src.pipeline.llm import REASONING_JSON_MIN_TOKENS, LLMClient
from src.prompts import load_prompt
from src.schemas.taleo_split import (
    ApplicantSlice,
    ProposalApplicant,
    ProposalDiagnostics,
    SplitProposal,
)

# UNMEASURED until docs/model-profiles has taleo_segment_v1 — shipped at the
# reasoning-JSON floor; a committed model-check profile entry blocks hand-over.
TALEO_SEGMENT_MAX_TOKENS = REASONING_JSON_MIN_TOKENS

DEFAULT_MIN_TEXT = 200

# Email or a 10+ digit phone run in the page header -> a fresh applicant's
# resume usually opens with one. Cover letters carry contact info too, hence
# the salutation veto in ``is_cover``.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+?\d[\s().-]?){10,}")
_HEADER_CHARS = 400
_MERGED_HEAD = 600
_DIGEST_HEAD = 500

_COVER = re.compile(
    r"\b(dear\b|to whom it may concern|i am writing|hiring manager|"
    r"sincerely|kind regards|best regards|yours (?:truly|sincerely))",
    re.IGNORECASE,
)


def has_contact(text: str) -> bool:
    head = text[:_HEADER_CHARS]
    return bool(EMAIL_RE.search(head) or _PHONE.search(head))


def is_cover(text: str) -> bool:
    return bool(_COVER.search(text))


def parse_page_list(spec: str, page_count: int) -> list[int]:
    """Parse ``"1-2,5"`` (1-based) into pages, preserving order and repeats.

    Raises ValueError on malformed or out-of-bounds input."""
    pages: list[int] = []
    for raw_token in spec.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(token)
        if not (1 <= lo <= page_count and 1 <= hi <= page_count) or lo > hi:
            raise ValueError(f"invalid page range 1..{page_count}: '{token}'")
        pages.extend(range(lo, hi + 1))
    return pages


def extract_page_texts(pdf: bytes) -> list[str]:
    """Full extracted text of each page, one entry per page."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        return [page.get_text("text") or "" for page in doc]
    finally:
        doc.close()


def page_accounting(
    page_count: int, assigned: list[list[int]]
) -> tuple[list[int], list[int]]:
    """Reconcile 1-based pages actually written per applicant against
    ``1..page_count``. Returns ``(missing, duplicated)``, both sorted
    ascending: ``missing`` are pages written to no applicant at all;
    ``duplicated`` are pages written more than once, whether to the same
    applicant twice (e.g. résumé + cover letter) or to two different ones."""
    counts: dict[int, int] = {}
    for pages in assigned:
        for p in pages:
            counts[p] = counts.get(p, 0) + 1
    missing = [p for p in range(1, page_count + 1) if p not in counts]
    duplicated = sorted(p for p, n in counts.items() if n > 1)
    return missing, duplicated


def valid_pages(pages: list[int], page_count: int) -> list[int]:
    """Keep in-range pages, preserve order, drop duplicates."""
    seen: set[int] = set()
    out: list[int] = []
    for p in pages:
        if 1 <= p <= page_count and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def page_digest(texts: list[str]) -> str:
    """Compact per-page summary for the LLM: page no + flags + first chars."""
    lines: list[str] = []
    for i, t in enumerate(texts, start=1):
        head = " ".join(t.split())[:_DIGEST_HEAD]
        flags = []
        if has_contact(t):
            flags.append("CONTACT")
        if is_cover(t):
            flags.append("COVER")
        tag = f" [{'/'.join(flags)}]" if flags else ""
        lines.append(f"[page {i}]{tag}: {head}")
    return "\n".join(lines)


def repair_orphan_pages(
    manifest_applicants: list[ProposalApplicant],
    page_count: int,
    page_emails: Callable[[int], set[str]],
) -> tuple[list[ProposalApplicant], list[str]]:
    """Repair pages the LLM manifest left assigned to no applicant, when a
    single distinct email ties the page unambiguously to a NEIGHBOUR
    applicant's résumé (2026-09-19 finding: page 30 of a real 43-page export
    carried the SAME email as page 29, the last résumé page of the preceding
    applicant, mis-assigned by the model).

    For each page in ``1..page_count`` assigned to no applicant (neither
    résumé nor cover-letter pages, across every applicant): look up its
    distinct emails via ``page_emails``. If there is exactly one, and it
    equals the email on the immediately PRECEDING applicant's last résumé
    page, append the orphan page to that applicant's résumé pages. Failing
    that, if it equals the email on the immediately FOLLOWING applicant's
    first résumé page, prepend it there instead. Otherwise the page is left
    untouched (still orphaned; the caller's existing page-accounting check
    still fires on it).

    Only résumé pages are ever grown — an applicant with no résumé pages
    (cover-letter-only) is never a repair target, so a page is never folded
    into a cover-letter page set.

    Cover-letter pages are out of scope: ``page_emails`` is expected to
    return an empty set for a page whose text reads as a cover letter (the
    caller's real implementation checks ``is_cover`` before extracting
    emails), which this function then treats identically to "no email" —
    it has no page text of its own to make that call.

    Returns the repaired applicant list (a deep copy; the input is never
    mutated) and the human-readable report lines to print, one per repair.
    """
    assigned: set[int] = set()
    for a in manifest_applicants:
        assigned.update(a.resume_pages)
        assigned.update(a.cover_letter_pages)
    orphans = sorted(p for p in range(1, page_count + 1) if p not in assigned)

    repaired = [a.model_copy(deep=True) for a in manifest_applicants]
    report: list[str] = []

    def _bounds(a: ProposalApplicant) -> tuple[int, int] | None:
        pages = a.resume_pages + a.cover_letter_pages
        return (min(pages), max(pages)) if pages else None

    for p in orphans:
        emails = page_emails(p)
        if len(emails) != 1:
            continue
        (email,) = tuple(emails)

        prev_idx: int | None = None
        prev_max = -1
        next_idx: int | None = None
        next_min = page_count + 1
        for idx, a in enumerate(repaired):
            b = _bounds(a)
            if b is None:
                continue
            if b[1] < p and b[1] > prev_max:
                prev_max = b[1]
                prev_idx = idx
            if b[0] > p and b[0] < next_min:
                next_min = b[0]
                next_idx = idx

        attached = False
        if prev_idx is not None:
            a = repaired[prev_idx]
            if a.resume_pages:
                last_resume_page = max(a.resume_pages)
                if email in page_emails(last_resume_page):
                    a.resume_pages = sorted([*a.resume_pages, p])
                    report.append(
                        f"REPAIRED: page {p} attached to applicant "
                        f"{prev_idx + 1} — same applicant email as page "
                        f"{last_resume_page}"
                    )
                    attached = True

        if not attached and next_idx is not None:
            a = repaired[next_idx]
            if a.resume_pages:
                first_resume_page = min(a.resume_pages)
                if email in page_emails(first_resume_page):
                    a.resume_pages = sorted([p, *a.resume_pages])
                    report.append(
                        f"REPAIRED: page {p} attached to applicant "
                        f"{next_idx + 1} — same applicant email as page "
                        f"{first_resume_page}"
                    )

    return repaired, report


def orphan_page_emails(texts: list[str]) -> Callable[[int], set[str]]:
    """The ``page_emails`` callable :func:`repair_orphan_pages` needs. A page
    that reads as a cover letter returns no emails (out of scope for repair)."""

    def page_emails(p: int) -> set[str]:
        text = texts[p - 1]
        if is_cover(text):
            return set()
        return {m.lower() for m in EMAIL_RE.findall(text[:_MERGED_HEAD])}

    return page_emails


def validate_proposal(
    proposal: SplitProposal,
    page_texts: list[str],
    *,
    min_text: int = DEFAULT_MIN_TEXT,
) -> ProposalDiagnostics:
    """Recompute every finding from the per-page text. ``ignored_pages`` count
    as accounted for (but are still duplicates if also assigned). Only résumé
    pages feed the merged check, and only the first 600 chars of each."""
    page_count = len(page_texts)
    assigned = [[*a.resume_pages, *a.cover_letter_pages] for a in proposal.applicants]
    assigned.append(list(proposal.ignored_pages))
    missing, duplicated = page_accounting(page_count, assigned)

    merged: list[int] = []
    cover_only: list[int] = []
    low_text: list[int] = []
    for idx, a in enumerate(proposal.applicants, start=1):
        r_pages = valid_pages(a.resume_pages, page_count)
        if not r_pages:
            if valid_pages(a.cover_letter_pages, page_count):
                cover_only.append(idx)
            continue
        emails: set[str] = set()
        for p in r_pages:
            for m in EMAIL_RE.findall(page_texts[p - 1][:_MERGED_HEAD]):
                emails.add(m.lower())
        if len(emails) >= 2:
            merged.append(idx)
        if len("".join(page_texts[p - 1] for p in r_pages).strip()) < min_text:
            low_text.append(idx)

    return ProposalDiagnostics(
        missing=missing,
        duplicated=duplicated,
        merged=merged,
        cover_only=cover_only,
        low_text=low_text,
    )


def heuristic_proposal(page_texts: list[str]) -> SplitProposal:
    """Offline contact-block heuristic. A page starts a new applicant when it
    carries a fresh contact block and is not a cover letter. Cover pages are
    classified into ``cover_letter_pages`` — NEVER folded into the résumé.
    No names are guessed."""
    applicants: list[ProposalApplicant] = []
    current: ProposalApplicant | None = None
    for i, text in enumerate(page_texts, start=1):
        cover = is_cover(text)
        if current is None or (has_contact(text) and not cover):
            current = ProposalApplicant()
            applicants.append(current)
        if cover:
            current.cover_letter_pages.append(i)
        else:
            current.resume_pages.append(i)
    return SplitProposal(applicants=applicants)


def _pages_to_pdf(doc: fitz.Document, pages: list[int]) -> bytes:
    out = fitz.open()
    try:
        for p in pages:
            out.insert_pdf(doc, from_page=p - 1, to_page=p - 1)
        return bytes(out.tobytes())
    finally:
        out.close()


def build_slices(
    pdf: bytes, proposal: SplitProposal, exclude: Collection[int] = ()
) -> list[ApplicantSlice]:
    """Emit one résumé PDF (+ optional cover PDF) per applicant with a résumé.

    Cover-only applicants are skipped (excluded, never ingested) but keep their
    place in the numbering. ``exclude`` is a set of 1-based applicant indexes
    (accept passes cover_only | low_text) skipped the same way.

    Filenames are name-free: ``applicant-NNN_*``."""
    doc = fitz.open(stream=pdf, filetype="pdf")
    try:
        page_count = int(doc.page_count)
        slices: list[ApplicantSlice] = []
        for idx, a in enumerate(proposal.applicants, start=1):
            if idx in exclude:
                continue
            r_pages = valid_pages(a.resume_pages, page_count)
            if not r_pages:
                continue
            c_pages = valid_pages(a.cover_letter_pages, page_count)
            slices.append(
                ApplicantSlice(
                    index=idx,
                    resume_filename=f"applicant-{idx:03d}_resume.pdf",
                    cover_filename=(
                        f"applicant-{idx:03d}_cover_letter.pdf" if c_pages else None
                    ),
                    resume_pdf=_pages_to_pdf(doc, r_pages),
                    cover_pdf=_pages_to_pdf(doc, c_pages) if c_pages else None,
                )
            )
        return slices
    finally:
        doc.close()


def build_pairing_manifest(slices: list[ApplicantSlice]) -> bytes:
    """A pairing manifest (résumé -> cover) consumable by
    ``parse_pairing_manifest``. Carries filenames only — no candidate names."""
    applicants = [
        {
            "resume_file": s.resume_filename,
            "cover_letter_file": s.cover_filename,
            "cover_letter_flag": "Yes" if s.cover_filename else "No",
        }
        for s in slices
    ]
    return json.dumps({"applicants": applicants}, indent=2).encode("utf-8")


async def llm_segment(llm: LLMClient, page_texts: list[str]) -> SplitProposal:
    """Ask the local LLM to segment the export. Failures propagate: the caller
    (worker) owns the fall back to :func:`heuristic_proposal`."""
    rendered = load_prompt("taleo_segment_v1", digest=page_digest(page_texts))
    proposal = await llm.chat_json(
        rendered.messages, SplitProposal, max_tokens=TALEO_SEGMENT_MAX_TOKENS
    )
    # The model proposes page assignments ONLY. ``ignored_pages`` is a
    # manager-only escape hatch (a model could otherwise silently drop an
    # applicant), and guessed names are never persisted or shown.
    return SplitProposal(
        applicants=[
            ProposalApplicant(
                candidate_name="",
                resume_pages=list(a.resume_pages),
                cover_letter_pages=list(a.cover_letter_pages),
            )
            for a in proposal.applicants
        ],
        ignored_pages=[],
    )
