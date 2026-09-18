"""Per-résumé cover-letter pairing (FU-3 Slice 2) — pure, I/O-free.

Pairs an uploaded batch of files into applicants: each résumé optionally
carries its OWN cover letter, matched by a filename convention
(``<base>_resume`` ↔ ``<base>_cover_letter``) or an explicit manifest (Slice 3
— the ``manifest`` param is plumbed here but only exercised there). A
cover-named file with no matching résumé is DEMOTED to a standalone résumé
(ingested on its own, with a note) rather than dropped — so a stray name never
loses a document, and a plain bulk upload (no cover-named files) behaves
exactly as before.

Ported from hris ``apps/api/src/api/services/bulk_ingest_service.py`` (the
pairing half). DEVIATIONS from the hris source:

* An uploaded file here is a plain ``tuple[str, bytes]`` = ``(filename,
  content)`` (what ``expand_zip_entries`` returns), NOT hris's ``ExpandedFile``
  dataclass — so ``ApplicantFiles.resume``/``.cover_letter`` are tuples.
* The non-fatal ``note`` strings are STATIC English (no filename interpolated),
  a blind/PIPEDA invariant: a pairing warning surfaced to the operator must
  never carry filename-derived candidate PII.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, get_args

from src.errors import AppError
from src.schemas.jobs import EmploymentType, Seniority
from src.schemas.resumes import WorkAuthorization

# An uploaded file as this repo models it: ``(filename, content)``.
UploadedFile = tuple[str, bytes]

# Space / dash / underscore are equivalent separators (bug fix:
# fix/cover-letter-pairing-separators). ``[\s_-]*`` between "cover" and
# "letter"/"note" lets a real-world name like "Jane Smith Cover Letter.pdf"
# match. Two guards, both load-bearing:
#   * the mandatory ``[\s_-]+`` between base and suffix means "discover.pdf"
#     (no separator before "cover") never matches;
#   * ``(?P<base>.*\S)`` requires a non-empty name before that separator, so a
#     leading-separator stem with no name ("_cover_letter", "-cover") stays a
#     résumé rather than pairing on an empty base.
# The ``$`` anchor makes the alternation ORDER irrelevant (only the alternative
# that consumes the exact tail can match at a given base/separator split), so
# "cover letter" is recognized whether or not a bare "cover" precedes it.
_COVER_SUFFIX_RE: Final = re.compile(
    r"^(?P<base>.*\S)[\s_-]+"
    r"(?:cover[\s_-]*letter|coverletter|cover[\s_-]*note|cover)$",
    re.IGNORECASE,
)
_RESUME_SUFFIX_RE: Final = re.compile(
    r"^(?P<base>.*\S)[\s_-]+(?:resume|cv)$", re.IGNORECASE
)


def _norm_base(s: str) -> str:
    """Collapse runs of space/underscore/dash into a single space so a base
    built from any separator convention normalizes to the same pairing key."""
    return re.sub(r"[\s_-]+", " ", s).strip()


# The suffix regexes have two adjacent ambiguous quantifiers (``.*\S`` then
# ``[\s_-]+`` over overlapping classes), so a pathological all-separator stem
# backtracks O(n²). Filenames run on UNTRUSTED input (zip entry names can be
# tens of KB) and pairing runs synchronously inside the async upload route, so
# an uncapped name could block the event loop. Real résumé/cover names are far
# under this cap; an over-length stem skips suffix detection entirely (treated
# as a plain résumé) — ``_norm_base``'s single quantifier stays linear.
_MAX_STEM_LEN: Final = 256


# STATIC English pairing notes — never interpolate a filename (blind invariant).
_DEMOTED_COVER_NOTE: Final = (
    "looked like a cover letter but had no matching résumé; ingested as a résumé"
)
# ADR-017 amendment (2026-09-18): the reversal from "promote" to "disclose".
# When the caller supplies ``is_cover_content`` and it says the orphan's
# ACTUAL text reads as a cover letter, that file is NEVER demoted to a
# résumé — a cover letter must never be ranked as a résumé — it is reported
# in ``PairingResult.unattached`` instead, with this static reason.
_UNATTACHED_COVER_REASON: Final = (
    "a cover letter with no matching résumé — not ingested, since a cover "
    "letter must never be ranked as a résumé"
)
_MANIFEST_MISSING_COVER_NOTE: Final = (
    "a cover letter named in the manifest wasn't in the upload"
)
_MANIFEST_MISSING_RESUME_REASON: Final = (
    "manifest names a résumé that wasn't in the upload"
)

# ``cover_letter_flag`` truthy vocabulary. Anything outside this set (including
# an explicit "No"/"False"/"") forces NO cover for that applicant even when a
# ``cover_letter_file`` is named — the flag is the operator's veto.
_COVER_FLAG_TRUE: Final = frozenset({"true", "1", "yes", "y", "t"})

# Defensive size bound on the untrusted manifest bytes BEFORE ``json.loads`` —
# mirrors the zip-expansion / docx-decompression trust boundaries so a giant
# blob can't exhaust memory in the parser. 1 MiB is far above any real pairing
# manifest (a few hundred applicants of short filenames).
_MAX_MANIFEST_BYTES: Final = 1 * 1024 * 1024


class ManifestError(AppError):
    """The pairing manifest couldn't be parsed or had an invalid shape.

    Subclasses this repo's :class:`src.errors.AppError` (NOT hris's error base),
    so the global FastAPI ``AppError`` handler renders it as a 422 without any
    per-route ``try/except`` — mirrors ``NotFoundError``/``FileRejectedError``.
    """

    code = "bulk.manifest_invalid"
    status = 422


def _decode(blob: bytes) -> str:
    """Decode manifest bytes tolerantly (strip a UTF-8 BOM, fall back to
    latin-1) — ported from hris so a stray byte won't 500 before we can raise a
    typed ``ManifestError``."""
    for codec in ("utf-8-sig", "utf-8"):
        try:
            return blob.decode(codec)
        except UnicodeDecodeError:
            continue
    return blob.decode("latin-1", errors="replace")


def parse_pairing_manifest(blob: bytes) -> dict[str, str | None]:
    """Parse a JSON pairing manifest into ``{resume_key: cover_key | None}``
    (both lowercased basenames via :func:`basename_lower`).

    Shape: ``{"applicants": [{"resume_file": "...", "cover_letter_file": "...",
    "cover_letter_flag": "Yes"}, ...]}``. An applicant without a usable
    ``resume_file`` string is skipped; a falsy ``cover_letter_flag`` (anything
    outside ``_COVER_FLAG_TRUE``) forces no cover even if a file is named.
    Raises :class:`ManifestError` (422) on oversize bytes, non-JSON, a non-dict
    root, or a missing ``applicants`` array.
    """
    if len(blob) > _MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"manifest is {len(blob)} bytes; the cap is {_MAX_MANIFEST_BYTES}"
        )
    try:
        data = json.loads(_decode(blob))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    applicants = data.get("applicants") if isinstance(data, dict) else None
    if not isinstance(applicants, list):
        raise ManifestError("manifest must be an object with an 'applicants' array")

    out: dict[str, str | None] = {}
    for entry in applicants:
        if not isinstance(entry, dict):
            continue
        resume_file = entry.get("resume_file")
        if not isinstance(resume_file, str) or not resume_file.strip():
            continue
        cover_file = entry.get("cover_letter_file")
        flag = entry.get("cover_letter_flag")
        flag_ok = flag is None or str(flag).strip().lower() in _COVER_FLAG_TRUE
        cover_key = (
            basename_lower(cover_file)
            if flag_ok and isinstance(cover_file, str) and cover_file.strip()
            else None
        )
        out[basename_lower(resume_file)] = cover_key
    return out


def basename_lower(filename: str) -> str:
    """The lowercased basename (folder stripped) used as a pairing/manifest
    key, so a manifest path and an uploaded file line up regardless of folder
    or case."""
    return PurePosixPath(filename.replace("\\", "/")).name.lower()


def _classify(filename: str) -> tuple[str, str]:
    """Classify a filename by its stem suffix. Returns ``(role, base)`` where
    ``role`` is ``"cover"`` or ``"resume"`` and ``base`` is the shared key two
    paired files have in common (the stem minus the role suffix). Space, dash,
    and underscore are equivalent separators (case-insensitive) both in
    matching the suffix and in the returned ``base``, so filenames using
    different separator conventions still share the same pairing key. A stem
    with no known suffix is a résumé whose base is the whole (normalized)
    stem."""
    stem = Path(basename_lower(filename)).stem
    if len(stem) > _MAX_STEM_LEN:
        # Pathological/attacker-crafted length — skip the backtracking-prone
        # suffix regexes (see _MAX_STEM_LEN). No real name reaches here.
        return "resume", _norm_base(stem)
    cover_match = _COVER_SUFFIX_RE.match(stem)
    if cover_match:
        return "cover", _norm_base(cover_match.group("base"))
    resume_match = _RESUME_SUFFIX_RE.match(stem)
    if resume_match:
        return "resume", _norm_base(resume_match.group("base"))
    return "resume", _norm_base(stem)


@dataclass(frozen=True)
class ApplicantFiles:
    """One applicant to feed through the résumé upload path: a résumé file and
    an optional cover-letter file. ``note`` carries a non-fatal pairing warning
    for this row (e.g. a cover-named file with no matching résumé that was
    demoted), surfaced to the operator on the result row."""

    resume: UploadedFile
    cover_letter: UploadedFile | None = None
    note: str | None = None


@dataclass(frozen=True)
class PairingResult:
    """Outcome of ``pair_applicants``: the applicants to ingest plus any
    ``rejected`` manifest references (a résumé the manifest named but that
    wasn't in the upload) — surfaced as ``rejected`` rows, never silently
    dropped."""

    pairs: list[ApplicantFiles] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (filename, reason)
    # ADR-017 amendment (2026-09-18): an orphan cover-named file whose ACTUAL
    # content reads as a cover letter (per ``is_cover_content``) — disclosed
    # here rather than promoted to a résumé. Defaults to empty so every
    # existing caller (``is_cover_content`` omitted) is byte-identical to
    # before this field existed.
    # (filename, reason)
    unattached: list[tuple[str, str]] = field(default_factory=list)


def _pair_from_manifest(
    by_name: dict[str, UploadedFile],
    manifest: dict[str, str | None],
    used: set[str],
    result: PairingResult,
) -> None:
    """Apply explicit résumé→cover manifest pairings, marking consumed files in
    ``used``. A named-but-absent résumé is a rejected row; a named-but-absent
    cover is a per-row note — never a silent drop."""
    for resume_key, cover_key in manifest.items():
        resume = by_name.get(resume_key)
        if resume is None:
            result.rejected.append((resume_key, _MANIFEST_MISSING_RESUME_REASON))
            continue
        if resume_key in used:
            continue
        used.add(resume_key)
        cover: UploadedFile | None = None
        note: str | None = None
        if cover_key is not None:
            cover = by_name.get(cover_key)
            if cover is None:
                note = _MANIFEST_MISSING_COVER_NOTE
            else:
                used.add(cover_key)
        result.pairs.append(
            ApplicantFiles(resume=resume, cover_letter=cover, note=note)
        )


def pair_applicants(
    files: list[UploadedFile],
    *,
    manifest: dict[str, str | None] | None = None,
    is_cover_content: Callable[[UploadedFile], bool] | None = None,
) -> PairingResult:
    """Pair each cover letter to its résumé. ``manifest`` (résumé→cover keys)
    takes precedence; everything it doesn't cover falls back to the filename
    convention. Input order of résumés is preserved. Pure — no I/O.

    ``is_cover_content`` is ADDITIVE and OPTIONAL (ADR-017 amendment,
    2026-09-18 — the reversal from "promote" to "disclose"): a caller (the
    upload route) can supply it to check a file's ACTUAL extracted text, not
    just its filename. It is consulted ONLY on a leftover cover-named file
    that has no matching résumé (never on a résumé that pairs cleanly with
    its own cover letter). When it returns True, that file is NEVER demoted
    to a standalone résumé — it goes to ``PairingResult.unattached`` instead
    (filename, static reason) and is absent from every ``ApplicantFiles``. A
    False/None return preserves today's demote-with-note behaviour.
    ``is_cover_content=None`` (the default) is byte-identical to omitting it."""
    by_name = {basename_lower(f[0]): f for f in files}
    used: set[str] = set()
    result = PairingResult()

    # 1. Manifest-driven pairing (explicit wins).
    if manifest:
        _pair_from_manifest(by_name, manifest, used, result)

    # 2. Convention pairing for whatever the manifest didn't consume. Gather
    #    candidate cover letters by base key first, then attach to résumés.
    remaining = [f for f in files if basename_lower(f[0]) not in used]
    covers_by_base: dict[str, list[UploadedFile]] = {}
    for f in remaining:
        role, base = _classify(f[0])
        if role == "cover":
            covers_by_base.setdefault(base, []).append(f)

    for f in remaining:
        role, base = _classify(f[0])
        if role == "cover":
            continue  # attached below (or demoted in step 3)
        cands = covers_by_base.get(base)
        cover = cands.pop(0) if cands else None
        result.pairs.append(ApplicantFiles(resume=f, cover_letter=cover))

    # 3. Leftover cover-named files (no matching résumé, or a duplicate cover
    #    for an already-paired résumé) → demote to a standalone résumé with a
    #    note, so nothing is silently lost.
    for cands in covers_by_base.values():
        for f in cands:
            if is_cover_content is not None and is_cover_content(f):
                result.unattached.append((f[0], _UNATTACHED_COVER_REASON))
            else:
                result.pairs.append(ApplicantFiles(resume=f, note=_DEMOTED_COVER_NOTE))

    return result


# ── bulk-JD CSV manifest (FU-3 Slice 4) ──────────────────────────────────
#
# A bulk-JD upload (many .txt/.json/.pdf/.docx, or a .zip of them) creates ONE
# draft job per file. An OPTIONAL sidecar CSV manifest maps a filename to job
# metadata (title/department/…) so the recruiter can pin fields up front instead
# of editing each created job afterwards. Ported from hris
# ``apps/api/src/api/services/bulk_ingest_service.py`` (``JobManifestRow`` /
# ``title_from_filename`` / ``parse_csv_manifest``), retargeted to THIS repo's
# ``src.schemas.jobs.EmploymentType``/``Seniority`` for enum validation and
# reusing this module's ``_decode`` / ``_MAX_MANIFEST_BYTES`` trust boundary.


@dataclass(frozen=True)
class JobManifestRow:
    """One CSV-manifest row: optional job metadata keyed by filename.

    A present ``title`` pins the created job's title (the filename-stem fallback
    is used only when it is absent). Every field is optional so a manifest with
    just a ``filename`` column is valid.
    """

    title: str | None = None
    department: str | None = None
    location: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    min_years: int | None = None
    retention_days: int | None = None
    blind_review: bool | None = None


def title_from_filename(filename: str) -> str:
    """Derive a job title from a filename stem.

    Replaces ``-``/``_``/whitespace runs with single spaces; PRESERVES the
    recruiter's casing (so ``Senior_DevOps_Engineer`` stays mixed-case rather
    than being title-cased into ``Senior Devops Engineer``). Clamped to
    JobCreate's 2-200 char bounds, with a safe fallback for a too-short stem.
    """
    # Strip folder(s) first (keeping case), then the extension.
    stem = Path(PurePosixPath(filename.replace("\\", "/")).name).stem
    cleaned = re.sub(r"[\s_-]+", " ", stem).strip()
    if len(cleaned) < 2:
        return "Untitled job"
    return cleaned[:200]


def _int_cell(value: str | None, field_name: str, line_no: int) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ManifestError(
            f"row {line_no}: {field_name} must be an integer, got {value!r}",
            row=line_no,
        ) from exc


def _bool_cell(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in _COVER_FLAG_TRUE


def parse_csv_manifest(blob: bytes) -> dict[str, JobManifestRow]:
    """Parse an optional bulk-JD CSV manifest into ``{basename_lower: row}``.

    Required header column: ``filename``. Optional: ``title``, ``department``,
    ``location``, ``employment_type``, ``seniority``, ``min_years``,
    ``retention_days``, ``blind_review``. Header names are matched
    case/space-insensitively; keys are the lowercased basename (folder stripped)
    so a manifest path and an uploaded file line up regardless of folder or case.
    Raises :class:`ManifestError` (422) on oversize bytes, an empty file, a
    missing ``filename`` column, or an invalid enum/integer cell value.
    """
    if len(blob) > _MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"manifest is {len(blob)} bytes; the cap is {_MAX_MANIFEST_BYTES}"
        )
    reader = csv.DictReader(io.StringIO(_decode(blob)))
    if reader.fieldnames is None:
        raise ManifestError("the manifest is empty")

    headers = {(h or "").strip().lower(): (h or "") for h in reader.fieldnames}
    if "filename" not in headers:
        raise ManifestError("manifest is missing a required 'filename' column")

    def cell(row: dict[str, str], key: str) -> str | None:
        if key not in headers:
            return None
        raw = row.get(headers[key])
        if raw is None:
            return None
        return raw.strip() or None

    employment_values = set(get_args(EmploymentType))
    seniority_values = set(get_args(Seniority))

    out: dict[str, JobManifestRow] = {}
    for line_no, row in enumerate(reader, start=2):  # row 1 is the header
        filename = cell(row, "filename")
        if filename is None:
            continue  # blank line / no filename → skip

        employment_type = cell(row, "employment_type")
        if employment_type is not None and employment_type not in employment_values:
            raise ManifestError(
                f"row {line_no}: invalid employment_type {employment_type!r}",
                row=line_no,
            )
        seniority = cell(row, "seniority")
        if seniority is not None and seniority not in seniority_values:
            raise ManifestError(
                f"row {line_no}: invalid seniority {seniority!r}", row=line_no
            )

        out[basename_lower(filename)] = JobManifestRow(
            title=cell(row, "title"),
            department=cell(row, "department"),
            location=cell(row, "location"),
            employment_type=employment_type,
            seniority=seniority,
            min_years=_int_cell(cell(row, "min_years"), "min_years", line_no),
            retention_days=_int_cell(
                cell(row, "retention_days"), "retention_days", line_no
            ),
            blind_review=_bool_cell(cell(row, "blind_review")),
        )
    return out


# ── Taleo candidate-roster CSV (Sponsor Requirements PR2 · S3/I1) ────────
#
# The real Taleo "All Candidates" export (arrived 2026-09-09; see
# ``docs/SPONSOR_REQUIREMENTS_PLAN.md`` ~line 402) has NO attachment-filename
# column — ``Resume`` is blank on all 315 real data rows — so there is nothing
# to key an upload-pairing dict by. ``parse_candidate_csv`` therefore returns
# an ordered ``list[CandidateRosterRow]`` (line-numbered), not a dict: the
# fixture also has one person submitted twice with CONFLICTING work-
# authorization declarations, and collapsing on any key would silently hide
# that conflict from the recruiter who needs to see it.
_HEADER_NORM_RE: Final = re.compile(r"[\s_]+")


def _normalize_header(h: str) -> str:
    """Collapse whitespace/underscore runs to a single space before
    strip+lower, so ``"Work Authorization"``, ``"work_authorization"`` and
    ``"  Work  Authorization  "`` all key to the same column."""
    return _HEADER_NORM_RE.sub(" ", h or "").strip().lower()


# The single place a raw Taleo work-authorization string is spelled. Two
# choices here are screening decisions, not formatting ones, so they get
# recorded rather than left to be "corrected" by a future reader:
#
# 1. ``"Work Permit" -> "eligible"`` is the sponsor's explicit, dated answer
#    (2026-09-09) — not this codebase's guess — and it is not a minor case: it
#    decides 123 of the 315 real rows. If that answer ever changes, it changes
#    here, in one place, with a new sponsor decision to cite.
# 2. Every key below is a declaration Taleo actually emits; a string that
#    ISN'T one of these keys is handled by the caller as ``"unknown"``, never
#    ``"not_eligible"``. Silently banding a real candidate last because the
#    parser met a string it didn't recognise would be an adverse decision on a
#    protected ground (BC Human Rights Code) that nobody actually made — the
#    raw cell survives in ``work_authorization_source`` so a recruiter can be
#    told the tool didn't understand it, instead of a state nobody chose being
#    recorded silently. All 315 real rows carry one of these four recognised
#    declarations (180 / 123 / 7 / 5 respectively, zero blanks) — the
#    "unknown" branch is only exercised by the synthetic tests/vendor fixture,
#    so a clean run against a real export is not evidence that branch works.
WORK_AUTHORIZATION_MAP: Final[dict[str, WorkAuthorization]] = {
    "no restrictions": "eligible",
    "work permit": "eligible",  # sponsor-confirmed 2026-09-09; 123/315 real rows
    "study permit": "not_eligible",
    "not eligible to work in canada": "not_eligible",
}


@dataclass(frozen=True)
class CandidateRosterRow:
    """One row of a parsed Taleo candidate-roster CSV, line-numbered so
    duplicates/conflicts survive rather than being collapsed by a dict key."""

    line_no: int
    name: str | None
    email: str | None
    sfu_id: str | None
    # The Literal, not ``str``: the three states are the whole point of this
    # field, and typing it loosely would let a typo'd fourth state ("not-
    # eligible") type-check here and only fail later at the DDL's CHECK
    # constraint — or reach a path that has no constraint at all. mypy is the
    # enforcement; this repo's characteristic defect is an invariant stated in
    # prose with nothing checking it.
    work_authorization: WorkAuthorization
    work_authorization_source: str | None
    # ``bool | None``, mirroring ``work_authorization``'s absent/declared
    # distinction: ``None`` means the "APSA Internal"/"CUPE Internal" column
    # was not present in this export at all; ``False`` means the column WAS
    # present and this row declares the candidate is not internal; ``True``
    # means internal. Collapsing "absent" into ``False`` (as slice 1
    # originally did) would make a `False` write indistinguishable from "no
    # opinion", and since Taleo exports are snapshots re-uploaded as
    # applicants trickle in, a wrongly-set ``True`` would become impossible to
    # ever clear by re-uploading a corrected roster —
    # ``candidate_roster_service.reconcile_candidate_roster`` writes internal
    # status only for rows where the value is not ``None``, and a declared
    # ``False`` DOES clear a previously-set ``True``.
    internal_apsa: bool | None
    internal_cupe: bool | None
    submission_date: str | None


def parse_candidate_csv(blob: bytes) -> list[CandidateRosterRow]:
    """Parse a Taleo "All Candidates" export into an ordered list of rows.

    At least one of ``name``/``email`` columns must be present (matched
    case/space/underscore-insensitively, like ``parse_csv_manifest``'s
    ``headers`` dict); a row with neither a name nor an email is a blank/
    placeholder line and is skipped, not an error. ``line_no`` is the real CSV
    line (header is line 1), preserved even when a preceding row was skipped.
    Raises :class:`ManifestError` (422) on oversize bytes or an empty file, or
    when neither a name nor an email column is present at all.
    """
    if len(blob) > _MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"manifest is {len(blob)} bytes; the cap is {_MAX_MANIFEST_BYTES}"
        )
    reader = csv.DictReader(io.StringIO(_decode(blob)))
    if reader.fieldnames is None:
        raise ManifestError("the manifest is empty")

    headers = {_normalize_header(h): (h or "") for h in reader.fieldnames}
    if "name" not in headers and "email" not in headers:
        raise ManifestError("manifest has neither a 'name' nor an 'email' column")

    def cell(row: dict[str, str], key: str) -> str | None:
        if key not in headers:
            return None
        raw = row.get(headers[key])
        if raw is None:
            return None
        return raw.strip() or None

    def raw_cell(row: dict[str, str], key: str) -> str | None:
        # UNLIKE cell(): returns the value verbatim (no strip), because the
        # work-authorization mapping test pins that ``work_authorization_source``
        # carries the exact raw declaration Taleo sent, whitespace and all.
        if key not in headers:
            return None
        return row.get(headers[key])

    def internal_flag(row: dict[str, str], key: str) -> bool | None:
        # ``None`` when the column is absent from the export altogether
        # (checked once against ``headers``, not per-cell) — a present-but-
        # blank cell is a declared ``False``, not an absence.
        if key not in headers:
            return None
        return (cell(row, key) or "").lower() == "i"

    rows: list[CandidateRosterRow] = []
    for line_no, row in enumerate(reader, start=2):  # row 1 is the header
        name = cell(row, "name")
        email = cell(row, "email")
        if name is None and email is None:
            continue  # blank/placeholder row, not an error

        wa_raw = raw_cell(row, "work authorization")
        work_authorization: WorkAuthorization
        work_authorization_source: str | None
        if wa_raw is None or not wa_raw.strip():
            work_authorization = "unknown"
            work_authorization_source = None
        else:
            work_authorization_source = wa_raw
            # ``.get(..., "unknown")`` is the load-bearing default: an
            # unrecognised declaration must never fall through to
            # ``not_eligible``. See WORK_AUTHORIZATION_MAP above.
            work_authorization = WORK_AUTHORIZATION_MAP.get(
                wa_raw.strip().lower(), "unknown"
            )

        rows.append(
            CandidateRosterRow(
                line_no=line_no,
                name=name,
                email=email,
                sfu_id=cell(row, "sfu id"),
                work_authorization=work_authorization,
                work_authorization_source=work_authorization_source,
                internal_apsa=internal_flag(row, "apsa internal"),
                internal_cupe=internal_flag(row, "cupe internal"),
                submission_date=cell(row, "submission date"),
            )
        )
    return rows


__all__ = [
    "ApplicantFiles",
    "CandidateRosterRow",
    "JobManifestRow",
    "ManifestError",
    "PairingResult",
    "UploadedFile",
    "WORK_AUTHORIZATION_MAP",
    "basename_lower",
    "pair_applicants",
    "parse_candidate_csv",
    "parse_csv_manifest",
    "parse_pairing_manifest",
    "title_from_filename",
]
