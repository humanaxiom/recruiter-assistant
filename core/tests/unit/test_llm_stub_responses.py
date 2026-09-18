"""RED — pins the contract for ``core/stub_llm/responses.py``, the offline
fake OpenAI-compatible model the stress/e2e stack points ``LLM_BASE_URL`` at
instead of the real tailnet Ollama peer.

Not written here (Tester scope: tests only, never implementation) —
``stub_llm/responses.py`` itself, which does not exist yet. Every test below
fails at COLLECTION with ``ModuleNotFoundError`` until it does.

Routing contract: ``respond(system_prompt, user_prompt)`` looks at the
DISTINGUISHING first line of the real system templates under
``src/prompts/templates/*.system.j2`` (read from disk here, so this pins
against what the app ACTUALLY sends, not a paraphrase of it) and returns the
matching schema's JSON. An unrecognised system prompt raises ``ValueError`` —
silently guessing would corrupt a fixture in a way nothing downstream could
tell from a real, badly-calibrated model.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from src.pipeline.matching.stages import verify_evidence
from src.schemas.jobs import JDExtracted, ManagerRequirements
from src.schemas.matching import EvidenceObjectIngest
from src.schemas.resumes import CoverLetterParsed, ResumeCore, ResumeSkillDetails
from stub_llm.responses import embed, respond

_TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "prompts" / "templates"


def _system(name: str) -> str:
    return (_TEMPLATES / name).read_text(encoding="utf-8")


JD_SYSTEM = _system("jd_extract_v2.system.j2")
RESUME_CORE_SYSTEM = _system("resume_core_v1.system.j2")
RESUME_SKILLS_SYSTEM = _system("resume_skills_v2.system.j2")
COVER_LETTER_SYSTEM = _system("cover_letter_v1.system.j2")
MANAGER_PROMPT_SYSTEM = _system("manager_prompt_v1.system.j2")
EVIDENCE_SYSTEM = _system("shortlist_evidence_v1.system.j2")
EVIDENCE_V2_SYSTEM = _system("shortlist_evidence_v2.system.j2")

_SYNTHETIC_NAME_RE = re.compile(r"^Candidate [0-9a-f]{4} Synthetic$")
_SYNTHETIC_EMAIL_RE = re.compile(r"^[0-9a-f]{4}@example\.invalid$")


def _digest(user_prompt: str) -> str:
    """The contract this stub is pinned to: the first 4 hex chars of the
    user prompt's sha256, so a synthetic candidate is deterministic per
    fixture résumé without ever containing a real name."""
    return hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()[:4]


# ── jd_extract ───────────────────────────────────────────────────────────


def test_jd_extract_response_validates_against_the_real_schema() -> None:
    out = respond(JD_SYSTEM, "Job title: Senior Data Engineer. Requires Python.")
    jd = JDExtracted.model_validate(out)
    assert len(jd.required_skills) >= 5


def test_jd_extract_required_skills_have_names() -> None:
    out = respond(JD_SYSTEM, "any JD text")
    jd = JDExtracted.model_validate(out)
    assert all(s.name for s in jd.required_skills)


# ── resume_core ──────────────────────────────────────────────────────────


def test_resume_core_response_validates_against_the_real_schema() -> None:
    out = respond(RESUME_CORE_SYSTEM, "résumé fixture text for candidate 1")
    core = ResumeCore.model_validate(out)
    assert core.candidate.name is not None
    assert core.candidate.email is not None


def test_resume_core_synthetic_name_is_deterministic_and_contains_no_real_name() -> (
    None
):
    prompt = "résumé fixture text for candidate 1"
    out1 = ResumeCore.model_validate(respond(RESUME_CORE_SYSTEM, prompt))
    out2 = ResumeCore.model_validate(respond(RESUME_CORE_SYSTEM, prompt))
    assert out1.candidate.name == out2.candidate.name
    assert out1.candidate.email == out2.candidate.email
    assert out1.candidate.name is not None
    assert _SYNTHETIC_NAME_RE.match(out1.candidate.name)
    assert out1.candidate.email is not None
    assert _SYNTHETIC_EMAIL_RE.match(out1.candidate.email)


def test_resume_core_synthetic_name_derives_from_the_user_prompt_hash() -> None:
    prompt = "résumé fixture text for candidate 2"
    out = ResumeCore.model_validate(respond(RESUME_CORE_SYSTEM, prompt))
    digest = _digest(prompt)
    assert out.candidate.name == f"Candidate {digest} Synthetic"
    assert out.candidate.email == f"{digest}@example.invalid"


def test_resume_core_different_prompts_yield_different_synthetic_candidates() -> None:
    a = ResumeCore.model_validate(
        respond(RESUME_CORE_SYSTEM, "résumé fixture text for candidate A")
    )
    b = ResumeCore.model_validate(
        respond(RESUME_CORE_SYSTEM, "résumé fixture text for candidate B")
    )
    assert a.candidate.name != b.candidate.name


# ── resume_skills ────────────────────────────────────────────────────────


def test_resume_skills_response_validates_against_the_real_schema() -> None:
    out = respond(RESUME_SKILLS_SYSTEM, "résumé fixture text for candidate 1")
    skills = ResumeSkillDetails.model_validate(out)
    assert len(skills.skills) >= 2


def test_resume_skills_overlap_the_jd_vocabulary() -> None:
    jd = JDExtracted.model_validate(respond(JD_SYSTEM, "any JD text"))
    jd_names = {s.name for s in jd.required_skills}

    resume_skills = ResumeSkillDetails.model_validate(
        respond(RESUME_SKILLS_SYSTEM, "résumé fixture text for candidate 1")
    )
    resume_names = {s.name for s in resume_skills.skills}

    assert len(resume_names & jd_names) >= 2


# ── cover_letter ─────────────────────────────────────────────────────────


def test_cover_letter_response_validates_against_the_real_schema() -> None:
    out = respond(COVER_LETTER_SYSTEM, "Dear hiring committee, I am excited...")
    parsed = CoverLetterParsed.model_validate(out)
    assert parsed.raw_text or parsed.themes


# ── manager_prompt ───────────────────────────────────────────────────────


def test_manager_prompt_response_validates_against_the_real_schema() -> None:
    out = respond(MANAGER_PROMPT_SYSTEM, "Looking for someone with Terraform.")
    parsed = ManagerRequirements.model_validate(out)
    assert isinstance(parsed.must_have_skills, list)


# ── shortlist_evidence ───────────────────────────────────────────────────


_CHUNKS: list[dict[str, str]] = [
    {
        "id": "c_001",
        "section": "experience",
        "text": (
            "Led migration of the payments platform to Kubernetes, cutting "
            "deployment time from 45 minutes to under 5 across 12 services."
        ),
    },
    {
        "id": "c_002",
        "section": "skills",
        "text": "Proficient in Python, PostgreSQL, and FastAPI for backend systems.",
    },
]


def _evidence_user_prompt() -> str:
    lines = [
        "Evaluate the candidate against these 1 requirements.",
        "",
        "Job title: Senior Platform Engineer",
        "",
        "Requirements:",
        "1. 5+ years of Kubernetes experience",
        "",
        "Candidate resume chunks (cite by id):",
        "",
    ]
    for chunk in _CHUNKS:
        lines.append(f"--- chunk {chunk['id']} (section: {chunk['section']}) ---")
        lines.append(chunk["text"])
        lines.append("")
    lines.append("--- end ---")
    lines.append("")
    lines.append("Return the JSON.")
    return "\n".join(lines)


def test_shortlist_evidence_response_validates_against_the_real_schema() -> None:
    out = respond(EVIDENCE_SYSTEM, _evidence_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    assert len(evidence.requirements) >= 1


def test_shortlist_evidence_cites_real_chunk_ids_present_in_the_prompt() -> None:
    out = respond(EVIDENCE_SYSTEM, _evidence_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    real_ids = {c["id"] for c in _CHUNKS}
    for req in evidence.requirements:
        assert set(req.evidence_chunk_ids) <= real_ids
        assert req.evidence_chunk_ids, "must cite at least one real chunk"


def test_shortlist_evidence_quotes_a_verbatim_prefix_of_the_cited_chunk() -> None:
    chunks_by_id = {c["id"]: c["text"] for c in _CHUNKS}
    out = respond(EVIDENCE_SYSTEM, _evidence_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    for req in evidence.requirements:
        assert req.status == "met"
        cited_text = chunks_by_id[req.evidence_chunk_ids[0]]
        assert cited_text.lower().startswith(req.evidence.lower()[:20])


def test_shortlist_evidence_survives_verify_evidence_unscrubbed() -> None:
    """The whole point of quoting a real verbatim prefix: ``verify_evidence``
    (the anti-fabrication guard every real shortlist run passes through)
    must NOT strip or downgrade the stub's own evidence."""
    chunks_by_id = {c["id"]: c["text"] for c in _CHUNKS}
    out = respond(EVIDENCE_SYSTEM, _evidence_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    verified = verify_evidence(evidence, chunks_by_id)
    for req in verified.requirements:
        assert req.status == "met"
        assert req.evidence_chunk_ids
        assert req.evidence != ""


# ── shortlist_evidence_v2 (cover letter) ───────────────────────────────────
#
# The orchestrator loads v2, not v1, whenever a candidate has cover-letter
# chunks (``orchestrator.py``'s ``_stage3_per_candidate``: ``has_cover =
# bool(cl_chunks)``). An unrouted v2 template raises ``ValueError`` -> the
# LLM call 500s -> the fail-closed orchestrator withholds the WHOLE
# shortlist, not just this one candidate's cover-letter section — so every
# stub-mode run against a job with a cover-letter upload times out. Pinned
# here next to v1 so the two can never drift apart again.

_COVER_CHUNKS: list[dict[str, str]] = [
    {
        "id": "cl_001",
        "section": "cover_letter",
        "text": (
            "I have followed your team's platform work for two years and am "
            "genuinely excited to bring my Kubernetes experience to it."
        ),
    },
]


def _evidence_v2_user_prompt() -> str:
    lines = [
        "Evaluate the candidate against these 1 requirements, then assess "
        "their cover letter.",
        "",
        "Job title: Senior Platform Engineer",
        "",
        "Requirements:",
        "1. 5+ years of Kubernetes experience",
        "",
        "Candidate resume chunks (cite by c_ id for requirements):",
        "",
    ]
    for chunk in _CHUNKS:
        lines.append(f"--- chunk {chunk['id']} (section: {chunk['section']}) ---")
        lines.append(chunk["text"])
        lines.append("")
    lines.append("--- end resume ---")
    lines.append("")
    lines.append(
        "Cover letter chunks (cite by cl_ id, for cover_letter_evidence ONLY):"
    )
    lines.append("")
    for chunk in _COVER_CHUNKS:
        lines.append(f"--- chunk {chunk['id']} (section: {chunk['section']}) ---")
        lines.append(chunk["text"])
        lines.append("")
    lines.append("--- end cover letter ---")
    lines.append("")
    lines.append("Return the JSON.")
    return "\n".join(lines)


def test_shortlist_evidence_v2_response_validates_against_the_real_schema() -> None:
    out = respond(EVIDENCE_V2_SYSTEM, _evidence_v2_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    assert len(evidence.requirements) >= 1


def test_shortlist_evidence_v2_cites_real_resume_chunk_ids() -> None:
    out = respond(EVIDENCE_V2_SYSTEM, _evidence_v2_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    real_ids = {c["id"] for c in _CHUNKS}
    for req in evidence.requirements:
        assert set(req.evidence_chunk_ids) <= real_ids
        assert req.evidence_chunk_ids, "must cite at least one real résumé chunk"


def test_shortlist_evidence_v2_reports_cover_letter_presence() -> None:
    out = respond(EVIDENCE_V2_SYSTEM, _evidence_v2_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    assert evidence.cover_letter_presence is True
    assert evidence.cover_letter_evidence


def test_shortlist_evidence_v2_cover_letter_cites_only_cl_ids() -> None:
    out = respond(EVIDENCE_V2_SYSTEM, _evidence_v2_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    real_cl_ids = {c["id"] for c in _COVER_CHUNKS}
    for cl_ev in evidence.cover_letter_evidence:
        assert cl_ev.evidence_chunk_ids, "must cite at least one real cl_ chunk"
        assert set(cl_ev.evidence_chunk_ids) <= real_cl_ids


def test_shortlist_evidence_v2_cover_letter_quotes_a_verbatim_prefix() -> None:
    cl_by_id = {c["id"]: c["text"] for c in _COVER_CHUNKS}
    out = respond(EVIDENCE_V2_SYSTEM, _evidence_v2_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    for cl_ev in evidence.cover_letter_evidence:
        cited_text = cl_by_id[cl_ev.evidence_chunk_ids[0]]
        assert cited_text.lower().startswith(cl_ev.evidence.lower()[:20])


def test_shortlist_evidence_v2_without_cover_chunks_behaves_like_v1() -> None:
    """A v2 system prompt with no cover-letter chunks in the user prompt
    (shouldn't happen in practice — the orchestrator only loads v2 when
    ``cl_chunks`` is non-empty — but the stub must not crash on it)."""
    out = respond(EVIDENCE_V2_SYSTEM, _evidence_user_prompt())
    evidence = EvidenceObjectIngest.model_validate(out)
    assert len(evidence.requirements) >= 1
    assert evidence.cover_letter_presence is False


# ── unknown system prompt ────────────────────────────────────────────────


def test_unknown_system_prompt_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown|unrecognised|unrecognized"):
        respond("You do something this stub has never heard of.", "irrelevant")


# ── embed ────────────────────────────────────────────────────────────────


def test_embed_returns_one_vector_per_text_at_768_dimensions() -> None:
    vectors = embed(["alpha resume text", "beta resume text", "gamma"])
    assert len(vectors) == 3
    assert all(len(v) == 768 for v in vectors)


def test_embed_vectors_are_unit_norm() -> None:
    vectors = embed(["some résumé chunk text"])
    norm = sum(x * x for x in vectors[0]) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_embed_is_deterministic_for_equal_input() -> None:
    a = embed(["same text"])[0]
    b = embed(["same text"])[0]
    assert a == b


def test_embed_differs_for_different_input() -> None:
    a = embed(["text one"])[0]
    b = embed(["text two"])[0]
    assert a != b


def test_embed_returns_empty_list_for_no_texts() -> None:
    assert embed([]) == []


def test_embed_respects_a_custom_dim() -> None:
    vectors = embed(["x"], dim=16)
    assert len(vectors[0]) == 16
    norm = sum(x * x for x in vectors[0]) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_embed_type_signature_accepts_any_return_dict() -> None:
    # respond() must return a plain JSON-serialisable dict, not a pydantic
    # model — the stub HTTP server (Flask/FastAPI, built by the coder) will
    # `json.dumps` it straight into an OpenAI-compatible response body.
    out = respond(JD_SYSTEM, "any JD text")
    assert isinstance(out, dict)
    out2: dict[str, Any] = out
    assert out2 is not None
