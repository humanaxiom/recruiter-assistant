"""Pure Taleo combined-export split service, pinned (slice B1, RED).

The splitter logic that lives in ``core/scripts/split_taleo_pdf.py`` moves to
a pure, I/O-free service so the in-app "split upload" flow and the CLI share
ONE implementation. This module pins the PUBLIC API of the two new modules.

``src/schemas/taleo_split.py``::

    class ProposalApplicant(BaseModel):          # extra="ignore"
        candidate_name: str = ""
        resume_pages: list[int] = []             # 1-based, order significant
        cover_letter_pages: list[int] = []

    class SplitProposal(BaseModel):              # extra="ignore"
        applicants: list[ProposalApplicant] = []
        ignored_pages: list[int] = []            # manager-marked: accounted for

    class ProposalDiagnostics(BaseModel):
        missing: list[int]        # 1-based pages in no applicant and not ignored
        duplicated: list[int]     # 1-based pages assigned more than once
        merged: list[int]         # 1-based APPLICANT indexes whose resume pages
                                  #   carry >= 2 distinct emails
        cover_only: list[int]     # applicant indexes with cover pages, no resume
        low_text: list[int]       # applicant indexes whose resume text < min_text
        blocking: bool            # property: missing or duplicated or merged
                                  #   (cover_only / low_text are NOT blocking:
                                  #   they are excluded and disclosed instead)

    class ApplicantSlice(BaseModel):
        index: int                        # 1-based position in the proposal
        resume_filename: str              # "applicant-NNN_resume.pdf"
        cover_filename: str | None        # "applicant-NNN_cover_letter.pdf"
        resume_pdf: bytes
        cover_pdf: bytes | None

``src/services/taleo_split_service.py``::

    TALEO_SEGMENT_MAX_TOKENS: int                       # == REASONING_JSON_MIN_TOKENS
    DEFAULT_MIN_TEXT: int                               # 200

    def parse_page_list(spec: str, page_count: int) -> list[int]
    def extract_page_texts(pdf: bytes) -> list[str]
    def page_accounting(page_count, assigned) -> (missing, duplicated)
    def valid_pages(pages, page_count) -> list[int]
    def page_digest(texts: list[str]) -> str
    def repair_orphan_pages(applicants, page_count, page_emails) -> (list, list[str])
    def validate_proposal(proposal: SplitProposal, page_texts: list[str],
                          *, min_text: int = DEFAULT_MIN_TEXT) -> ProposalDiagnostics
    def heuristic_proposal(page_texts: list[str]) -> SplitProposal
    def build_slices(pdf: bytes, proposal: SplitProposal) -> list[ApplicantSlice]
    def build_pairing_manifest(slices: list[ApplicantSlice]) -> bytes
    async def llm_segment(llm: LLMClient, page_texts: list[str]) -> SplitProposal

``core/scripts/split_taleo_pdf.py`` becomes a CLI wrapper that RE-EXPORTS the
moved names (``page_accounting``, ``repair_orphan_pages``, ``_valid_pages``,
``_page_digest``, ``_Applicant``) as the very same objects.
"""

from __future__ import annotations

import importlib.util
import json
import textwrap
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Any

import fitz  # type: ignore[import-untyped]
import pytest

from src.pipeline.llm import REASONING_JSON_MIN_TOKENS
from src.prompts import load_prompt
from src.schemas.taleo_split import (
    ApplicantSlice,
    ProposalApplicant,
    ProposalDiagnostics,
    SplitProposal,
)
from src.services import taleo_split_service as svc
from src.services.bulk_ingest_service import parse_pairing_manifest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "split_taleo_pdf.py"


def _script_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_split_taleo_pdf_b1", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── helpers ──────────────────────────────────────────────────────────────────

_PAD = " ".join(["Experience in recruiting and operations."] * 8)  # > 200 chars


def _resume_text(email: str, marker: str = "") -> str:
    return f"Applicant {marker}\n{email}\n604-555-0100\nRESUME\n{_PAD}"


def _cover_text(email: str, marker: str = "") -> str:
    return (
        f"{email}\nDear Hiring Manager,\nI am writing to apply {marker}.\n"
        f"{_PAD}\nSincerely,\nApplicant"
    )


def _make_pdf(pages: list[str]) -> bytes:
    # Wrap long lines: insert_text never wraps, so a 327-char _PAD line ran off
    # the 612pt page and get_text clipped it to ~100 chars, which made healthy
    # applicants look LOW TEXT.
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        lines = [w for ln in text.splitlines() for w in (textwrap.wrap(ln, 80) or [""])]
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + i * 14), line, fontsize=9)
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _pdf_page_count(data: bytes) -> int:
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        return int(doc.page_count)
    finally:
        doc.close()


def _pdf_texts(data: bytes) -> list[str]:
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        return [page.get_text("text") or "" for page in doc]
    finally:
        doc.close()


def _app(
    resume: list[int], cover: list[int] | None = None, name: str = ""
) -> ProposalApplicant:
    return ProposalApplicant(
        candidate_name=name, resume_pages=resume, cover_letter_pages=cover or []
    )


# ── parse_page_list (grammar and bounds) ─────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("1-2,5", [1, 2, 5]),
        ("3", [3]),
        ("1-3", [1, 2, 3]),
        (" 1 - 2 , 5 ", [1, 2, 5]),
        ("5,1-2", [5, 1, 2]),  # order honoured, like script parse_ranges :139
        ("2,2", [2, 2]),  # kept: validate_proposal is what flags duplicates
        ("", []),
        ("1,,2", [1, 2]),
    ],
)
def test_parse_page_list_grammar(spec: str, expected: list[int]) -> None:
    assert svc.parse_page_list(spec, 6) == expected


@pytest.mark.parametrize(
    "spec", ["0", "7", "1-7", "0-2", "-1", "3-1", "a", "1-b", "1;2"]
)
def test_parse_page_list_rejects_malformed_or_out_of_bounds(spec: str) -> None:
    with pytest.raises(ValueError):
        svc.parse_page_list(spec, 6)


# ── moved pure helpers keep their behaviour (script :167, :371, :326) ────────


def test_page_accounting_missing_and_duplicated() -> None:
    assert svc.page_accounting(6, [[1, 2], [2, 3], [6]]) == ([4, 5], [2])


def test_valid_pages_drops_out_of_range_and_duplicates_keeping_order() -> None:
    assert svc.valid_pages([3, 1, 3, 0, 9, 2], 4) == [3, 1, 2]


def test_page_digest_flags_contact_and_cover_and_truncates() -> None:
    texts = [
        _resume_text("pat@example.com"),
        _cover_text("pat@example.com"),
        "x" * 2000,
    ]
    lines = svc.page_digest(texts).split("\n")
    assert lines[0].startswith("[page 1] [CONTACT]:")
    assert lines[1].startswith("[page 2] [CONTACT/COVER]:")
    assert lines[2].startswith("[page 3]: ")
    assert len(lines[2]) <= len("[page 3]: ") + 500


def test_extract_page_texts_one_entry_per_page() -> None:
    pdf = _make_pdf([_resume_text("a@example.com", "ONE"), "second page TWO"])
    texts = svc.extract_page_texts(pdf)
    assert len(texts) == 2
    assert "ONE" in texts[0]
    assert "TWO" in texts[1]


# ── validate_proposal ────────────────────────────────────────────────────────


def _clean_texts() -> list[str]:
    return [
        _resume_text("a@example.com", "A1"),
        _resume_text("a@example.com", "A2"),
        _cover_text("a@example.com", "A"),
        _resume_text("b@example.com", "B1"),
    ]


def test_validate_clean_proposal_has_no_findings() -> None:
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1, 2], [3]), _app([4])]), _clean_texts()
    )
    assert isinstance(diag, ProposalDiagnostics)
    assert diag.missing == []
    assert diag.duplicated == []
    assert diag.merged == []
    assert diag.cover_only == []
    assert diag.low_text == []
    assert diag.blocking is False


def test_validate_flags_missing_pages_and_blocks() -> None:
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1, 2]), _app([4])]), _clean_texts()
    )
    assert diag.missing == [3]
    assert diag.blocking is True


def test_validate_flags_duplicated_pages_and_blocks() -> None:
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1, 2], [3]), _app([3, 4])]), _clean_texts()
    )
    assert diag.duplicated == [3]
    assert diag.missing == []
    assert diag.blocking is True


def test_validate_flags_duplicate_between_resume_and_cover_of_same_applicant() -> None:
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1, 2, 3], [3]), _app([4])]), _clean_texts()
    )
    assert diag.duplicated == [3]


def test_validate_flags_merged_applicant_with_two_distinct_emails() -> None:
    texts = [
        _resume_text("a@example.com"),
        _resume_text("b@example.com"),
        _resume_text("c@example.com"),
    ]
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1, 2]), _app([3])]), texts
    )
    assert diag.merged == [1]
    assert diag.blocking is True


def test_validate_same_email_different_case_is_not_merged() -> None:
    texts = [_resume_text("Pat@Example.com"), _resume_text("pat@example.com")]
    diag = svc.validate_proposal(SplitProposal(applicants=[_app([1, 2])]), texts)
    assert diag.merged == []


def test_validate_cover_page_emails_never_count_toward_merged() -> None:
    texts = [
        _resume_text("a@example.com"),
        _cover_text("someone-else@example.com"),
    ]
    diag = svc.validate_proposal(SplitProposal(applicants=[_app([1], [2])]), texts)
    assert diag.merged == []
    assert diag.blocking is False


def test_validate_merged_only_looks_at_first_600_chars_of_each_page() -> None:
    far = "filler " * 200  # pushes the second email past char 600
    texts = [
        _resume_text("a@example.com"),
        f"Applicant\n{far}\nlate@example.com",
    ]
    diag = svc.validate_proposal(SplitProposal(applicants=[_app([1, 2])]), texts)
    assert diag.merged == []


def test_validate_flags_cover_only_applicant_not_blocking() -> None:
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1, 2]), _app([], [3]), _app([4])]),
        _clean_texts(),
    )
    assert diag.cover_only == [2]
    assert diag.blocking is False  # decision 1: excluded and disclosed


def test_validate_flags_low_text_resume_not_blocking() -> None:
    texts = _clean_texts()
    texts[3] = "b@example.com\nscan"
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1, 2], [3]), _app([4])]), texts
    )
    assert diag.low_text == [2]
    assert diag.blocking is False


def test_validate_low_text_threshold_is_configurable() -> None:
    texts = ["x" * 50]
    assert (
        svc.validate_proposal(
            SplitProposal(applicants=[_app([1])]), texts, min_text=10
        ).low_text
        == []
    )
    assert svc.validate_proposal(
        SplitProposal(applicants=[_app([1])]), texts, min_text=51
    ).low_text == [1]


def test_validate_default_low_text_threshold_is_200_chars() -> None:
    assert svc.DEFAULT_MIN_TEXT == 200
    just_under = ["x" * 199]
    at = ["x" * 200]
    assert svc.validate_proposal(
        SplitProposal(applicants=[_app([1])]), just_under
    ).low_text == [1]
    assert (
        svc.validate_proposal(SplitProposal(applicants=[_app([1])]), at).low_text == []
    )


def test_validate_ignored_pages_count_as_accounted_for() -> None:
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1, 2]), _app([4])], ignored_pages=[3]),
        _clean_texts(),
    )
    assert diag.missing == []
    assert diag.blocking is False


def test_validate_ignored_pages_do_not_hide_other_missing_pages() -> None:
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1])], ignored_pages=[3]), _clean_texts()
    )
    assert diag.missing == [2, 4]


def test_validate_ignored_page_also_assigned_is_still_duplicated() -> None:
    diag = svc.validate_proposal(
        SplitProposal(applicants=[_app([1, 2], [3]), _app([4])], ignored_pages=[3]),
        _clean_texts(),
    )
    assert diag.duplicated == [3]
    assert diag.blocking is True


def test_validate_empty_proposal_reports_every_page_missing() -> None:
    diag = svc.validate_proposal(SplitProposal(applicants=[]), _clean_texts())
    assert diag.missing == [1, 2, 3, 4]
    assert diag.blocking is True


# ── heuristic_proposal: cover pages go to the cover list (DTO-bug regression) ─


def _combined_pdf_texts() -> tuple[bytes, list[str]]:
    pages = [
        _resume_text("a@example.com", "A1"),  # 1 résumé A
        _cover_text("a@example.com", "A"),  # 2 cover A
        _resume_text("b@example.com", "B1"),  # 3 résumé B
        f"B continuation page\n{_PAD}",  # 4 résumé B (continuation)
    ]
    pdf = _make_pdf(pages)
    return pdf, svc.extract_page_texts(pdf)


def test_heuristic_proposal_classifies_cover_pages_into_cover_list() -> None:
    _pdf, texts = _combined_pdf_texts()
    proposal = svc.heuristic_proposal(texts)
    assert [a.resume_pages for a in proposal.applicants] == [[1], [3, 4]]
    assert [a.cover_letter_pages for a in proposal.applicants] == [[2], []]


def test_heuristic_proposal_never_folds_a_cover_page_into_resume_pages() -> None:
    _pdf, texts = _combined_pdf_texts()
    proposal = svc.heuristic_proposal(texts)
    for a in proposal.applicants:
        assert not set(a.resume_pages) & set(a.cover_letter_pages)
        assert 2 not in a.resume_pages


def test_heuristic_proposal_accounts_for_every_page_exactly_once() -> None:
    _pdf, texts = _combined_pdf_texts()
    proposal = svc.heuristic_proposal(texts)
    diag = svc.validate_proposal(proposal, texts)
    assert diag.missing == []
    assert diag.duplicated == []
    assert diag.merged == []
    assert diag.blocking is False


def test_heuristic_proposal_empty_input_is_empty() -> None:
    assert svc.heuristic_proposal([]).applicants == []


def test_heuristic_proposal_does_not_invent_names() -> None:
    _pdf, texts = _combined_pdf_texts()
    # Decision 6: guessed names are never persisted / surfaced as identity.
    assert all(a.candidate_name == "" for a in svc.heuristic_proposal(texts).applicants)


# ── build_slices: page counts and order ──────────────────────────────────────


def _marker_pdf() -> bytes:
    return _make_pdf([f"MARKER-{n}\nbody" for n in range(1, 7)])


def test_build_slices_page_counts_and_order() -> None:
    pdf = _marker_pdf()
    proposal = SplitProposal(
        applicants=[_app([1, 2], [3], name="Pat Example"), _app([5, 4])]
    )
    slices = svc.build_slices(pdf, proposal)

    assert [s.index for s in slices] == [1, 2]
    assert all(isinstance(s, ApplicantSlice) for s in slices)
    assert _pdf_page_count(slices[0].resume_pdf) == 2
    assert slices[0].cover_pdf is not None
    assert _pdf_page_count(slices[0].cover_pdf) == 1
    assert slices[1].cover_pdf is None
    # Listed order is honoured: pages 5 then 4.
    texts = _pdf_texts(slices[1].resume_pdf)
    assert "MARKER-5" in texts[0]
    assert "MARKER-4" in texts[1]
    assert "MARKER-3" in _pdf_texts(slices[0].cover_pdf)[0]


def test_build_slices_filenames_are_numbered_and_name_free() -> None:
    proposal = SplitProposal(
        applicants=[_app([1, 2], [3], name="Pat Example"), _app([4])]
    )
    slices = svc.build_slices(_marker_pdf(), proposal)
    assert slices[0].resume_filename == "applicant-001_resume.pdf"
    assert slices[0].cover_filename == "applicant-001_cover_letter.pdf"
    assert slices[1].resume_filename == "applicant-002_resume.pdf"
    assert slices[1].cover_filename is None
    for s in slices:
        blob = f"{s.resume_filename} {s.cover_filename}".lower()
        assert "pat" not in blob
        assert "example" not in blob


def test_build_slices_skips_cover_only_applicant_but_keeps_numbering() -> None:
    proposal = SplitProposal(
        applicants=[_app([1, 2], [3]), _app([], [6]), _app([5, 4])]
    )
    slices = svc.build_slices(_marker_pdf(), proposal)
    assert [s.index for s in slices] == [1, 3]
    assert slices[1].resume_filename == "applicant-003_resume.pdf"
    # The excluded cover-only page is in no emitted PDF (never ingested).
    emitted = "".join(
        "".join(_pdf_texts(p))
        for s in slices
        for p in (s.resume_pdf, s.cover_pdf)
        if p is not None
    )
    assert "MARKER-6" not in emitted


def test_build_slices_empty_proposal_is_empty() -> None:
    assert svc.build_slices(_marker_pdf(), SplitProposal(applicants=[])) == []


# ── build_pairing_manifest round-trips through parse_pairing_manifest ────────


def test_pairing_manifest_round_trips_through_parse_pairing_manifest() -> None:
    proposal = SplitProposal(
        applicants=[_app([1, 2], [3], name="Pat Example"), _app([4])]
    )
    slices = svc.build_slices(_marker_pdf(), proposal)
    blob = svc.build_pairing_manifest(slices)

    assert isinstance(blob, bytes)
    assert parse_pairing_manifest(blob) == {
        "applicant-001_resume.pdf": "applicant-001_cover_letter.pdf",
        "applicant-002_resume.pdf": None,
    }


def test_pairing_manifest_contains_no_candidate_names() -> None:
    proposal = SplitProposal(applicants=[_app([1, 2], [3], name="Pat Example")])
    blob = svc.build_pairing_manifest(svc.build_slices(_marker_pdf(), proposal))
    assert b"Pat" not in blob
    assert b"Example" not in blob
    assert json.loads(blob)["applicants"]  # valid JSON with the expected root


def test_pairing_manifest_of_no_slices_still_parses() -> None:
    assert parse_pairing_manifest(svc.build_pairing_manifest([])) == {}


# ── llm_segment with an injected fake LLM client ─────────────────────────────


class _FakeLLM:
    def __init__(self, result: object | None = None, exc: Exception | None = None):
        self._result = result
        self._exc = exc
        self.calls: list[dict[str, Any]] = []

    async def chat_json(
        self, messages: list[dict[str, str]], schema: type, **kwargs: Any
    ) -> object:
        self.calls.append({"messages": messages, "schema": schema, **kwargs})
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.mark.asyncio
async def test_llm_segment_returns_the_models_proposal() -> None:
    proposal = SplitProposal(applicants=[_app([1, 2], [3]), _app([4])])
    llm = _FakeLLM(result=proposal)
    out = await svc.llm_segment(llm, _clean_texts())  # type: ignore[arg-type]
    assert out == proposal
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_llm_segment_renders_prompt_taleo_segment_v1_with_digest() -> None:
    texts = _clean_texts()
    llm = _FakeLLM(result=SplitProposal())
    await svc.llm_segment(llm, texts)  # type: ignore[arg-type]

    rendered = load_prompt("taleo_segment_v1", digest=svc.page_digest(texts))
    messages = llm.calls[0]["messages"]
    assert messages == [
        {"role": "system", "content": rendered.system},
        {"role": "user", "content": rendered.user},
    ]
    assert "[page 1]" in messages[1]["content"]
    assert "[page 4]" in messages[1]["content"]


@pytest.mark.asyncio
async def test_llm_segment_validates_against_split_proposal_schema() -> None:
    llm = _FakeLLM(result=SplitProposal())
    await svc.llm_segment(llm, _clean_texts())  # type: ignore[arg-type]
    assert llm.calls[0]["schema"] is SplitProposal


@pytest.mark.asyncio
async def test_llm_segment_uses_the_segment_token_budget() -> None:
    # Decision 7: shipped UNMEASURED at the reasoning-JSON floor (8192).
    assert svc.TALEO_SEGMENT_MAX_TOKENS == REASONING_JSON_MIN_TOKENS
    llm = _FakeLLM(result=SplitProposal())
    await svc.llm_segment(llm, _clean_texts())  # type: ignore[arg-type]
    assert llm.calls[0]["max_tokens"] == svc.TALEO_SEGMENT_MAX_TOKENS


@pytest.mark.asyncio
async def test_llm_segment_propagates_llm_failure_for_caller_fallback() -> None:
    # Decision 3: the CALLER (worker) falls back to the heuristic; the pure
    # service must not swallow the error.
    llm = _FakeLLM(exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await svc.llm_segment(llm, _clean_texts())  # type: ignore[arg-type]


def test_prompt_taleo_segment_v1_requires_a_digest_variable() -> None:
    import jinja2

    with pytest.raises(jinja2.UndefinedError):
        load_prompt("taleo_segment_v1")


def test_prompt_taleo_segment_v1_embeds_the_digest_in_the_user_message() -> None:
    rendered = load_prompt("taleo_segment_v1", digest="DIGEST-SENTINEL-123")
    assert "DIGEST-SENTINEL-123" in rendered.user
    assert "DIGEST-SENTINEL-123" not in rendered.system
    assert "JSON" in rendered.system


# ── repair_orphan_pages lives in the service and still repairs ───────────────


def test_repair_orphan_pages_in_service_attaches_by_shared_email() -> None:
    applicants = [_app([1, 2, 10]), _app([12, 13])]

    def page_emails(p: int) -> set[str]:
        return {10: {"pat@example.com"}, 11: {"pat@example.com"}}.get(p, set())

    repaired, report = svc.repair_orphan_pages(applicants, 13, page_emails)
    assert repaired[0].resume_pages == [1, 2, 10, 11]
    assert len(report) == 1
    assert applicants[0].resume_pages == [1, 2, 10]  # input not mutated


# ── meta-test: the script's names ARE the service's objects ──────────────────


def test_script_reexports_are_the_service_objects() -> None:
    script = _script_module()
    assert script.page_accounting is svc.page_accounting
    assert script.repair_orphan_pages is svc.repair_orphan_pages
    assert script._valid_pages is svc.valid_pages
    assert script._page_digest is svc.page_digest
    assert script._Applicant is ProposalApplicant


def test_script_still_exposes_cli_only_names() -> None:
    script = _script_module()
    for name in ("main", "parse_ranges", "merged_applicant_files", "_zip_outputs"):
        assert callable(getattr(script, name)), name


# ── Slice C1 review fixes (M1, M3) ───────────────────────────────────────────
# M1: llm_segment must strip model-supplied ``ignored_pages`` (manager-only,
# decision 2) and every ``candidate_name`` (guessed names never persisted,
# decision 6) before returning.
# M3: build_slices takes ``exclude: Collection[int]`` (1-based applicant
# indexes) that are skipped while numbering is preserved (decision 1: accept
# passes cover_only | low_text).


@pytest.mark.asyncio
async def test_llm_segment_clears_model_supplied_ignored_pages_m1() -> None:
    llm = _FakeLLM(
        result=SplitProposal(
            applicants=[_app([1, 2], [3]), _app([4])], ignored_pages=[2]
        )
    )
    out = await svc.llm_segment(llm, _clean_texts())  # type: ignore[arg-type]
    assert out.ignored_pages == []


@pytest.mark.asyncio
async def test_llm_segment_clears_every_candidate_name_m1() -> None:
    llm = _FakeLLM(
        result=SplitProposal(
            applicants=[_app([1, 2], [3], name="Pat Example"), _app([4], name="Lee")]
        )
    )
    out = await svc.llm_segment(llm, _clean_texts())  # type: ignore[arg-type]
    assert [a.candidate_name for a in out.applicants] == ["", ""]


@pytest.mark.asyncio
async def test_llm_segment_keeps_the_page_assignments_when_stripping_m1() -> None:
    llm = _FakeLLM(
        result=SplitProposal(
            applicants=[_app([1, 2], [3], name="Pat"), _app([4])], ignored_pages=[9]
        )
    )
    out = await svc.llm_segment(llm, _clean_texts())  # type: ignore[arg-type]
    assert [a.resume_pages for a in out.applicants] == [[1, 2], [4]]
    assert [a.cover_letter_pages for a in out.applicants] == [[3], []]


@pytest.mark.asyncio
async def test_llm_segment_strip_means_a_dropped_applicant_cannot_hide_m1() -> None:
    # A model that "ignores" page 4 to hide an applicant: after the strip the
    # page is MISSING again, so validate_proposal blocks accept.
    texts = _clean_texts()
    llm = _FakeLLM(
        result=SplitProposal(applicants=[_app([1, 2], [3])], ignored_pages=[4])
    )
    out = await svc.llm_segment(llm, texts)  # type: ignore[arg-type]
    assert svc.validate_proposal(out, texts).missing == [4]


def test_build_slices_exclude_skips_the_index_but_keeps_numbering_m3() -> None:
    proposal = SplitProposal(applicants=[_app([1]), _app([2, 3]), _app([4, 5])])
    slices = svc.build_slices(_marker_pdf(), proposal, exclude={2})
    assert [s.index for s in slices] == [1, 3]
    assert slices[1].resume_filename == "applicant-003_resume.pdf"
    emitted = "".join("".join(_pdf_texts(s.resume_pdf)) for s in slices)
    assert "MARKER-2" not in emitted and "MARKER-3" not in emitted


def test_build_slices_exclude_default_is_unchanged_m3() -> None:
    proposal = SplitProposal(applicants=[_app([1]), _app([2, 3])])
    pdf = _marker_pdf()
    assert [s.index for s in svc.build_slices(pdf, proposal)] == [1, 2]
    assert [s.index for s in svc.build_slices(pdf, proposal, exclude=())] == [1, 2]


@pytest.mark.parametrize("kind", [set, frozenset, list, tuple])
def test_build_slices_exclude_accepts_any_collection_m3(kind: type) -> None:
    proposal = SplitProposal(applicants=[_app([1]), _app([2]), _app([3])])
    slices = svc.build_slices(_marker_pdf(), proposal, exclude=kind([1, 3]))
    assert [s.index for s in slices] == [2]


def test_build_slices_exclude_covers_low_text_union_cover_only_m3() -> None:
    texts = [_resume_text("a@example.com"), "x", _resume_text("c@example.com")]
    proposal = SplitProposal(
        applicants=[_app([1]), _app([2]), _app([3]), _app([], [4])]
    )
    pdf = _make_pdf([*texts, "cover only page"])
    diag = svc.validate_proposal(proposal, _pdf_texts(pdf))
    skip = set(diag.cover_only) | set(diag.low_text)
    assert skip == {2, 4}
    assert [s.index for s in svc.build_slices(pdf, proposal, exclude=skip)] == [1, 3]


def test_build_slices_exclude_of_unknown_index_is_ignored_m3() -> None:
    proposal = SplitProposal(applicants=[_app([1]), _app([2])])
    slices = svc.build_slices(_marker_pdf(), proposal, exclude={99})
    assert [s.index for s in slices] == [1, 2]
