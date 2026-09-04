#!/usr/bin/env python3
"""Standalone Taleo combined-PDF splitter — interim tool until Feature 2.

Splits ONE combined applicant-export PDF (the raw Taleo download, many
applicants concatenated) into per-applicant PDFs you can then bulk-upload
(multi-file or a ``.zip``) through the existing résumé uploader.

This is a LOCAL dev/ops utility. It is deliberately NOT wired into the app:
it touches no database and writes only to the output directory you pass. Its
one network call is to the configured local LLM host (Ollama on the tailnet);
``--heuristic`` and ``--ranges`` make none at all.

**Do not run it on this host** — there is no usable Python here, and PyMuPDF
(``fitz``) only exists in the worker image. Use the wrappers, which mount your
input and output directories into a throwaway worker container:

    scripts\\split-taleo.ps1 C:\\path\\to\\taleo_export.pdf -Output C:\\path\\to\\split
    scripts/split-taleo.sh  ./taleo_export.pdf --output ./split

Directly, if you would rather see the moving parts (``./core`` is bind-mounted
at ``/app``, so this file is already inside the container at
``/app/scripts/`` — no ``-v`` needed for the code, only for your data):

    docker compose run --rm \\
        -v "$PWD/in:/in" -v "$PWD/out:/out" \\
        worker python scripts/split_taleo_pdf.py /in/taleo_export.pdf \\
        --output /out --zip

A real Taleo export is real candidate PII. Keep it outside the repo, or under
the gitignored ``fixtures/`` — never commit one, and never pass a path that
lands the output inside a tracked directory.

Three modes (LLM is the default — pure heuristics mis-split section headings
like "Professional Summary" as new applicants):

* **LLM segmentation (default)** — sends a compact per-page digest to the
  LOCAL LLM on the configured host (Ollama on the Tailnet; no cloud egress),
  which emits a manifest (applicant → résumé pages + cover-letter pages +
  name); the deterministic slicer then executes it, writing a clean
  ``NNN_name_resume.pdf`` (+ ``NNN_name_cover_letter.pdf``) per applicant.
  Defaults to ``gpt-oss:20b`` (instruct, JSON-reliable; NOT a reasoning model
  like deepseek-r1, which breaks strict JSON — see docs/ops/llm-models.md);
  override with ``--model``. Always eyeball the printed proposal.
* **``--heuristic``** — the old pure-heuristic boundaries (contact block +
  salutation veto). Offline, no LLM; noisier. One PDF per applicant.
* **``--ranges "1-2;3-5;6,7"``** — deterministic manual split. 1-based pages;
  applicants separated by ``;``; pages/ranges within an applicant by ``,``.
  Order is honoured, so ``"21,19-20"`` puts page 21 (a cover page) first.

The split is loss-free page copying (no OCR): a scanned/image-only export
still produces unreadable PDFs — the tool flags low-text segments so you know
the app will struggle to parse them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import zipfile
from pathlib import Path

import fitz  # type: ignore[import-untyped]
import httpx
from pydantic import BaseModel, ConfigDict, Field

from src.pipeline.llm import LLMClient
from src.settings import get_settings as get_pipeline_settings

# Email or a 10+ digit phone run in the page header → a fresh applicant's
# résumé usually opens with one. Cover letters carry contact info too, hence
# the salutation veto below.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?:\+?\d[\s().-]?){10,}")
_HEADER_CHARS = 400

# Clean cover-letter signal in the validated sample (fired on every cover
# page, zero on résumé pages). Used to veto a cover page from starting a new
# applicant when it sits inside someone's packet.
_COVER = re.compile(
    r"\b(dear\b|to whom it may concern|i am writing|hiring manager|"
    r"sincerely|kind regards|best regards|yours (?:truly|sincerely))",
    re.IGNORECASE,
)


def _has_contact(text: str) -> bool:
    head = text[:_HEADER_CHARS]
    return bool(_EMAIL.search(head) or _PHONE.search(head))


def _is_cover(text: str) -> bool:
    return bool(_COVER.search(text))


def _guess_name(text: str) -> str | None:
    """Best-effort: the first short line of capitalised words that isn't an
    email/phone. Display + filename hint only — the app re-parses the real
    name from the résumé content, so a wrong guess is harmless."""
    for raw in text.splitlines():
        line = raw.strip()
        if not (2 <= len(line.split()) <= 4):
            continue
        if any(ch.isdigit() for ch in line) or "@" in line:
            continue
        if (
            line.replace(" ", "").replace("-", "").replace(".", "").isalpha()
            and line[:1].isupper()
        ):
            return line
    return None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def detect_segments(texts: list[str]) -> list[list[tuple[int, int]]]:
    """Group page indices (0-based) into one (start, end) range per applicant.

    Applicant 1 starts at page 0; every later page that carries a fresh
    contact block and is not a cover letter starts a new applicant. Cover
    letters and continuation pages fold into the current applicant."""
    if not texts:
        return []
    starts = [0]
    for i in range(1, len(texts)):
        if _has_contact(texts[i]) and not _is_cover(texts[i]):
            starts.append(i)
    segments: list[list[tuple[int, int]]] = []
    for idx, start in enumerate(starts):
        end = (starts[idx + 1] - 1) if idx + 1 < len(starts) else len(texts) - 1
        segments.append([(start, end)])
    return segments


def parse_ranges(spec: str, page_count: int) -> list[list[tuple[int, int]]]:
    """Parse ``"1-2;3-5;6,7"`` (1-based) into 0-based per-applicant ranges,
    preserving order. Raises ValueError on malformed or out-of-bounds input."""
    applicants: list[list[tuple[int, int]]] = []
    for raw_chunk in spec.split(";"):
        chunk = raw_chunk.strip()
        if not chunk:
            continue
        parts: list[tuple[int, int]] = []
        for raw_token in chunk.split(","):
            token = raw_token.strip()
            if not token:
                continue
            if "-" in token:
                lo_s, hi_s = token.split("-", 1)
                lo, hi = int(lo_s), int(hi_s)
            else:
                lo = hi = int(token)
            if not (1 <= lo <= page_count and 1 <= hi <= page_count):
                raise ValueError(f"page out of range 1..{page_count}: '{token}'")
            parts.append((lo - 1, hi - 1))
        if parts:
            applicants.append(parts)
    if not applicants:
        raise ValueError("no applicants parsed from --ranges")
    return applicants


def _write_applicant(
    doc: fitz.Document, parts: list[tuple[int, int]], out_path: Path
) -> int:
    """Write one applicant's pages (in the given order) to out_path. Returns
    the page count written."""
    out = fitz.open()
    pages = 0
    try:
        for lo, hi in parts:
            out.insert_pdf(doc, from_page=lo, to_page=hi)
            pages += hi - lo + 1
        out.save(out_path)
    finally:
        out.close()
    return pages


def _emit_applicants(
    doc: fitz.Document,
    texts: list[str],
    applicants: list[list[tuple[int, int]]],
    out_dir: Path,
    min_text: int,
) -> list[Path]:
    """Write each applicant's PDF and print a one-line proposal row. Returns
    the written paths (in applicant order)."""
    written: list[Path] = []
    for i, parts in enumerate(applicants, start=1):
        seg_text = "".join("".join(texts[lo : hi + 1]) for lo, hi in parts)
        name = _guess_name(seg_text)
        cover = any(_is_cover(texts[p]) for lo, hi in parts for p in range(lo, hi + 1))
        stem = f"applicant_{i:02d}" + (f"_{_slug(name)}" if name else "")
        out_path = out_dir / f"{stem}.pdf"
        n_pages = _write_applicant(doc, parts, out_path)
        written.append(out_path)

        label = ", ".join(
            f"{lo + 1}-{hi + 1}" if lo != hi else f"{lo + 1}" for lo, hi in parts
        )
        flags: list[str] = []
        if cover:
            flags.append("cover-letter pages")
        if len(seg_text.strip()) < min_text:
            flags.append("LOW TEXT — likely scanned, app can't OCR")
        flag_str = f"  [{'; '.join(flags)}]" if flags else ""
        print(
            f"  {i:2d}. pages {label:<12} -> {out_path.name}"
            f"  ({n_pages}p, name: {name or '?'}){flag_str}"
        )
    return written


# ---------------- LLM segmentation (default) ----------------


class _Applicant(BaseModel):
    """One applicant in the LLM-emitted manifest. Pages are 1-based."""

    model_config = ConfigDict(extra="ignore")

    candidate_name: str = ""
    resume_pages: list[int] = Field(default_factory=list)
    cover_letter_pages: list[int] = Field(default_factory=list)


class _TaleoManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    applicants: list[_Applicant] = Field(default_factory=list)


_DIGEST_HEAD = 500

_SEG_SYSTEM = (
    "You are a precise document-segmentation tool. You receive a page-by-page "
    "digest of ONE PDF that concatenates MANY job applicants' documents, in "
    "order. Each applicant has exactly one résumé (one or more consecutive "
    "pages) and OPTIONALLY one cover letter (one or more pages, usually "
    "adjacent to that applicant's résumé). Section headings INSIDE a résumé "
    "(e.g. 'Professional Summary', 'Core Competencies', 'Work Experience', "
    "'Registration') are NOT new applicants — do not split on them. A new "
    "applicant typically begins with a fresh contact block (a different "
    "person's name + email/phone), flagged CONTACT. Cover-letter pages are "
    "flagged COVER. Assign EVERY page (1..N) to exactly one applicant and "
    "classify it as résumé or cover letter. Return STRICT JSON only — no prose."
)

_SEG_USER = (
    "Segment this export. Return JSON of the form:\n"
    '{"applicants":[{"candidate_name":"Full Name",'
    '"resume_pages":[1,2],"cover_letter_pages":[3]}, ...]}\n'
    "Pages are 1-based. Every page must appear exactly once across all "
    "applicants. Omit cover_letter_pages (or use []) when an applicant has no "
    "cover letter.\n\nPER-PAGE DIGEST:\n"
)


def _page_digest(texts: list[str]) -> str:
    """Compact per-page summary for the LLM: page no + flags + first chars."""
    lines: list[str] = []
    for i, t in enumerate(texts, start=1):
        head = " ".join(t.split())[:_DIGEST_HEAD]
        flags = []
        if _has_contact(t):
            flags.append("CONTACT")
        if _is_cover(t):
            flags.append("COVER")
        tag = f" [{'/'.join(flags)}]" if flags else ""
        lines.append(f"[page {i}]{tag}: {head}")
    return "\n".join(lines)


async def _llm_segment(texts: list[str], *, model: str) -> _TaleoManifest:
    """Ask the local LLM to segment the export into a per-applicant manifest.
    Uses the configured host (no cloud egress); the model is overridable."""
    pipe = get_pipeline_settings()
    http = httpx.AsyncClient(timeout=pipe.llm_timeout_s)
    llm = LLMClient(
        pipe.llm_base_url,
        model,
        pipe.llm_model_embedding,
        timeout_s=pipe.llm_timeout_s,
        max_retries=pipe.llm_max_retries,
        breaker_threshold=pipe.llm_breaker_threshold,
        breaker_cooldown_s=pipe.llm_breaker_cooldown_s,
        debug_llm=pipe.debug_llm,
        native_chat=pipe.llm_ollama_native,
        http_client=http,
    )
    try:
        return await llm.chat_json(
            [
                {"role": "system", "content": _SEG_SYSTEM},
                {"role": "user", "content": _SEG_USER + _page_digest(texts)},
            ],
            _TaleoManifest,
            max_tokens=4096,
        )
    finally:
        await http.aclose()


def _valid_pages(pages: list[int], page_count: int) -> list[int]:
    """Keep in-range pages, preserve order, drop duplicates."""
    seen: set[int] = set()
    out: list[int] = []
    for p in pages:
        if 1 <= p <= page_count and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _write_pages(doc: fitz.Document, pages: list[int], out_path: Path) -> int:
    """Write the given 1-based pages (in order) to out_path."""
    out = fitz.open()
    try:
        for p in pages:
            out.insert_pdf(doc, from_page=p - 1, to_page=p - 1)
        out.save(out_path)
    finally:
        out.close()
    return len(pages)


def _emit_from_manifest(
    doc: fitz.Document,
    texts: list[str],
    manifest: _TaleoManifest,
    out_dir: Path,
    min_text: int,
) -> list[tuple[str, Path | None, Path | None]]:
    """Write a résumé PDF (+ optional cover-letter PDF) per applicant from the
    LLM manifest. Returns one (candidate_name, resume_path, cover_path) row per
    applicant (paths None when absent) — used to build the pairing manifest."""
    page_count = len(texts)
    emitted: list[tuple[str, Path | None, Path | None]] = []
    for i, a in enumerate(manifest.applicants, start=1):
        r_pages = _valid_pages(a.resume_pages, page_count)
        c_pages = _valid_pages(a.cover_letter_pages, page_count)
        if not r_pages and not c_pages:
            continue
        slug = _slug(a.candidate_name) if a.candidate_name else ""
        base = f"{i:03d}" + (f"_{slug}" if slug else "")

        flags: list[str] = []
        resume_path: Path | None = None
        if r_pages:
            resume_path = out_dir / f"{base}_resume.pdf"
            _write_pages(doc, r_pages, resume_path)
            r_text = "".join(texts[p - 1] for p in r_pages)
            if len(r_text.strip()) < min_text:
                flags.append("LOW TEXT — likely scanned")
        cover_path: Path | None = None
        if c_pages:
            cover_path = out_dir / f"{base}_cover_letter.pdf"
            _write_pages(doc, c_pages, cover_path)
        emitted.append((a.candidate_name, resume_path, cover_path))

        r_lbl = ",".join(map(str, r_pages)) or "—"
        c_lbl = ",".join(map(str, c_pages)) or "—"
        flag_str = f"  [{'; '.join(flags)}]" if flags else ""
        print(
            f"  {i:2d}. {a.candidate_name or '?':<28} "
            f"résumé p[{r_lbl}]  cover p[{c_lbl}]{flag_str}"
        )
    return emitted


def _write_pairing_manifest(
    emitted: list[tuple[str, Path | None, Path | None]], out_dir: Path
) -> Path:
    """Write a CodeX-shaped ``manifest.json`` pairing each résumé to its cover
    letter. The in-app paired uploader (Feature 2) consumes this directly, so
    the operator can upload the whole output dir (or the zip) and have cover
    letters attach to the right applicant without relying on filenames."""
    applicants = [
        {
            "candidate_name": name,
            "resume_file": resume_path.name,
            "cover_letter_file": cover_path.name if cover_path else None,
            "cover_letter_flag": "Yes" if cover_path else "No",
        }
        for name, resume_path, cover_path in emitted
        if resume_path is not None
    ]
    path = out_dir / "manifest.json"
    path.write_text(json.dumps({"applicants": applicants}, indent=2), encoding="utf-8")
    return path


def _run_llm_mode(
    doc: fitz.Document,
    texts: list[str],
    in_path: Path,
    out_dir: Path,
    *,
    model: str,
    min_text: int,
    do_zip: bool,
) -> int:
    print(f"\n{in_path.name}: {len(texts)} pages → segmenting with {model}…")
    try:
        manifest = asyncio.run(_llm_segment(texts, model=model))
    except Exception as exc:
        print(f"error: LLM segmentation failed ({exc}).")
        print("       Retry, or fall back to --heuristic / --ranges.")
        return 1
    print(f"output: {out_dir}\n")
    emitted = _emit_from_manifest(doc, texts, manifest, out_dir, min_text)
    resumes = [r for _, r, _ in emitted if r is not None]
    covers = [c for _, _, c in emitted if c is not None]
    manifest_path = _write_pairing_manifest(emitted, out_dir)
    if do_zip and resumes:
        zip_path = out_dir / "applicants.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in [*resumes, *covers, manifest_path]:
                zf.write(p, arcname=p.name)
        print(
            f"\nzipped {len(resumes)} résumé(s) + {len(covers)} "
            f"cover letter(s) + manifest -> {zip_path}"
        )
    print(
        f"\n{len(resumes)} résumé(s) + {len(covers)} cover letter(s). Review "
        "them, then bulk-upload applicants.zip (or the whole output dir) on "
        "the job's Resumes section: the cover letters pair to their applicant "
        "automatically via manifest.json (Feature 2).\n"
    )
    return 0


def _run_deterministic_mode(
    doc: fitz.Document,
    texts: list[str],
    in_path: Path,
    out_dir: Path,
    *,
    ranges: str | None,
    heuristic: bool,
    min_text: int,
    do_zip: bool,
) -> int:
    page_count = len(texts)
    if ranges:
        try:
            applicants = parse_ranges(ranges, page_count)
            mode = "manual (--ranges)"
        except ValueError as exc:
            print(f"error: bad --ranges: {exc}")
            return 2
    else:
        applicants = detect_segments(texts)
        mode = "heuristic"

    print(
        f"\n{in_path.name}: {page_count} pages → "
        f"{len(applicants)} applicant(s) [{mode}]"
    )
    print(f"output: {out_dir}\n")
    written = _emit_applicants(doc, texts, applicants, out_dir, min_text)

    if do_zip:
        zip_path = out_dir / "applicants.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in written:
                zf.write(p, arcname=p.name)
        print(f"\nzipped {len(written)} file(s) -> {zip_path}")

    if heuristic and len(applicants) <= 1 and page_count > 2:
        print(
            "\nNOTE: the heuristic found only one applicant on a multi-page "
            "PDF — drop --heuristic to use the LLM pass, or use --ranges "
            "after eyeballing the pages."
        )

    print(
        "\nNext: review the PDFs, then bulk-upload them (or applicants.zip) on the "
        "job's Resumes section. Each file is ingested as one candidate.\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Split a combined Taleo applicant-export PDF into per-applicant PDFs."
        ),
    )
    ap.add_argument("input", type=Path, help="combined PDF (e.g. /in/export.pdf)")
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output directory for the per-applicant PDFs (default: <input>_split)",
    )
    ap.add_argument(
        "--ranges",
        default=None,
        help='manual split, 1-based, e.g. "1-2;3-5;6,7" (applicants ; , pages). '
        "Overrides auto-detect; honours reorder.",
    )
    ap.add_argument(
        "--zip",
        action="store_true",
        help="also write applicants.zip in the output dir (ready for bulk upload)",
    )
    ap.add_argument(
        "--min-text",
        type=int,
        default=200,
        help=(
            "warn when an applicant has fewer than N extracted chars "
            "(likely scanned; default 200)"
        ),
    )
    ap.add_argument(
        "--heuristic",
        action="store_true",
        help=(
            "use the offline contact-block heuristic instead of the LLM "
            "segmentation pass"
        ),
    )
    ap.add_argument(
        "--model",
        default="gpt-oss:20b",
        help="local LLM for segmentation (default gpt-oss:20b; NOT a reasoning model)",
    )
    args = ap.parse_args(argv)

    in_path: Path = args.input
    if not in_path.is_file():
        print(f"error: no such file: {in_path}")
        return 2

    doc = fitz.open(in_path)
    try:
        if doc.needs_pass:
            print("error: PDF is password-protected — decrypt it first, then re-run.")
            return 2
        texts: list[str] = [page.get_text("text") or "" for page in doc]
        out_dir: Path = args.output or in_path.with_name(in_path.stem + "_split")
        out_dir.mkdir(parents=True, exist_ok=True)

        if not args.ranges and not args.heuristic:
            return _run_llm_mode(
                doc,
                texts,
                in_path,
                out_dir,
                model=args.model,
                min_text=args.min_text,
                do_zip=args.zip,
            )
        return _run_deterministic_mode(
            doc,
            texts,
            in_path,
            out_dir,
            ranges=args.ranges,
            heuristic=args.heuristic,
            min_text=args.min_text,
            do_zip=args.zip,
        )
    finally:
        doc.close()


if __name__ == "__main__":
    raise SystemExit(main())
