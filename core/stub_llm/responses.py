"""Canned, schema-valid responses for the offline LLM stub.

``respond(system_prompt, user_prompt)`` routes on the DISTINGUISHING first
line of the real system templates under ``src/prompts/templates/*.system.j2``
(pinned against the templates on disk by ``tests/unit/test_llm_stub_responses.py``,
never a paraphrase of them). An unrecognised system prompt raises
``ValueError`` — silently guessing would corrupt a fixture in a way nothing
downstream could tell from a real, badly-calibrated model.

Every synthetic candidate identity is derived from a hash of the user
prompt: deterministic per fixture résumé, and never a real name.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from typing import Any

# A vocabulary shared between the JD and résumé-skills stubs, so a stubbed
# job and a stubbed résumé always overlap on at least a couple of skills —
# exactly what the ranking pipeline needs to produce a non-trivial score.
_VOCAB: list[str] = [
    "Python",
    "SQL",
    "Kubernetes",
    "AWS",
    "Docker",
    "PostgreSQL",
    "Project Management",
    "Communication",
]


def _digest(user_prompt: str) -> str:
    """First 4 hex chars of the user prompt's sha256 — deterministic per
    fixture, never a real name."""
    return hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()[:4]


def _jd_extract(user_prompt: str) -> dict[str, Any]:
    return {
        "title": "Stub Position",
        "required_skills": [{"name": name} for name in _VOCAB[:5]],
        "nice_to_have_skills": [{"name": name} for name in _VOCAB[5:]],
        "min_years_experience": 2,
        "department": "Stub Department",
    }


def _resume_core(user_prompt: str) -> dict[str, Any]:
    digest = _digest(user_prompt)
    return {
        "candidate": {
            "name": f"Candidate {digest} Synthetic",
            "email": f"{digest}@example.invalid",
        },
        "summary": "Synthetic stub résumé summary.",
        "total_years_experience": 5,
        "experience": [
            {
                "company": "Stub Co",
                "title": "Stub Engineer",
                "start": "2019",
                "end": "2024",
                "is_current": False,
                "bullets": [],
            }
        ],
        "education": [],
    }


def _resume_skills(user_prompt: str) -> dict[str, Any]:
    digest = _digest(user_prompt)
    return {
        "skills": [
            {"name": _VOCAB[0], "years": 5},
            {"name": _VOCAB[1], "years": 3},
            {"name": _VOCAB[2]},
            {"name": f"Stub Skill {digest}"},
        ]
    }


def _cover_letter(user_prompt: str) -> dict[str, Any]:
    return {
        "raw_text": user_prompt[:2000],
        "themes": ["enthusiasm"],
        "key_claims": [],
    }


def _manager_prompt(user_prompt: str) -> dict[str, Any]:
    return {
        "must_have_skills": [{"name": _VOCAB[0]}],
        "nice_to_have_skills": [{"name": _VOCAB[1]}],
        "other_requirements": [],
    }


_CHUNK_RE = re.compile(
    r"--- chunk (?P<id>\S+) \(section: [^)]*\) ---\n"
    r"(?P<text>.*?)(?:\n\n|\n--- end)",
    re.DOTALL,
)
_REQUIREMENT_RE = re.compile(r"^\d+\.\s+(.+)$", re.MULTILINE)


def _all_chunks(user_prompt: str) -> list[tuple[str, str]]:
    """Every ``--- chunk <id> (section: ...) ---`` block in the prompt, résumé
    (``c_NNN``) and cover-letter (``cl_NNN``) alike, in document order."""
    return [
        (m.group("id"), m.group("text").strip())
        for m in _CHUNK_RE.finditer(user_prompt)
    ]


def _resume_chunks(user_prompt: str) -> list[tuple[str, str]]:
    return [
        (cid, text)
        for cid, text in _all_chunks(user_prompt)
        if not cid.startswith("cl_")
    ]


def _cover_letter_chunks(user_prompt: str) -> list[tuple[str, str]]:
    return [
        (cid, text) for cid, text in _all_chunks(user_prompt) if cid.startswith("cl_")
    ]


def _shortlist_evidence(user_prompt: str) -> dict[str, Any]:
    chunks = _resume_chunks(user_prompt)
    requirements_section = user_prompt.split("Requirements:", 1)
    req_texts: list[str] = []
    if len(requirements_section) > 1:
        candidate_pool = requirements_section[1].split("Candidate resume chunks", 1)[0]
        req_texts = [
            m.group(1).strip() for m in _REQUIREMENT_RE.finditer(candidate_pool)
        ]
    if not req_texts:
        req_texts = ["stub requirement"]
    if not chunks:
        return {
            "requirements": [
                {
                    "requirement": text,
                    "status": "missing",
                    "evidence": "",
                    "evidence_chunk_ids": [],
                    "confidence": 0.0,
                }
                for text in req_texts
            ],
            "overall_summary": "Stub evaluation: no résumé chunks provided.",
            "cover_letter_presence": False,
            "cover_letter_evidence": [],
            "overall_motivation": "",
        }
    chunk_id, chunk_text = chunks[0]
    quote = chunk_text[:40]
    return {
        "requirements": [
            {
                "requirement": text,
                "status": "met",
                "evidence": quote,
                "evidence_chunk_ids": [chunk_id],
                "confidence": 0.9,
            }
            for text in req_texts
        ],
        "overall_summary": "Stub evaluation: candidate meets the stubbed requirements.",
        "cover_letter_presence": False,
        "cover_letter_evidence": [],
        "overall_motivation": "",
    }


def _shortlist_evidence_v2(user_prompt: str) -> dict[str, Any]:
    """v2 adds a COVER LETTER assessment (Feature 1) on top of v1's
    requirement evidence. The orchestrator loads v2 whenever a candidate has
    cover-letter chunks (``orchestrator.py``'s ``_stage3_per_candidate``), so
    an unrouted v2 template 500s every stub-mode candidate that uploaded a
    cover letter — the whole shortlist then times out fail-closed."""
    base = _shortlist_evidence(user_prompt)
    cover_chunks = _cover_letter_chunks(user_prompt)
    if not cover_chunks:
        return base
    cl_id, cl_text = cover_chunks[0]
    return {
        **base,
        "cover_letter_presence": True,
        "cover_letter_evidence": [
            {
                "theme": "motivation",
                "evidence": cl_text[:40],
                "evidence_chunk_ids": [cl_id],
                "confidence": 0.8,
            }
        ],
        "overall_motivation": (
            "Stub evaluation: cover letter indicates genuine motivation."
        ),
    }


# The distinguishing first line of each real system template — read verbatim
# from disk by the pinning test, spelled here as the literal it routes on.
_JD_FIRST_LINE = (
    "You extract structured fields from a job description. Return ONLY valid"
)
_RESUME_CORE_FIRST_LINE = (
    "You extract structured fields from a resume. Return ONLY valid JSON"
)
_RESUME_SKILLS_FIRST_LINE = (
    "You extract the distinct technical and professional skills mentioned in"
)
_COVER_LETTER_FIRST_LINE = (
    "You extract structured fields from a candidate's COVER LETTER. Return ONLY"
)
_MANAGER_PROMPT_FIRST_LINE = (
    "You extract structured requirements from a HIRING MANAGER's free-text note"
)
_EVIDENCE_FIRST_LINE = (
    "You evaluate a candidate against a list of job requirements. Return"
)
_EVIDENCE_V2_FIRST_LINE = (
    "You evaluate a candidate against a list of job requirements AND assess their"
)

_ROUTES: dict[str, Callable[[str], dict[str, Any]]] = {
    _JD_FIRST_LINE: _jd_extract,
    _RESUME_CORE_FIRST_LINE: _resume_core,
    _RESUME_SKILLS_FIRST_LINE: _resume_skills,
    _COVER_LETTER_FIRST_LINE: _cover_letter,
    _MANAGER_PROMPT_FIRST_LINE: _manager_prompt,
    _EVIDENCE_FIRST_LINE: _shortlist_evidence,
    _EVIDENCE_V2_FIRST_LINE: _shortlist_evidence_v2,
}


def respond(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    """Route ``system_prompt`` to its canned, schema-valid JSON response.

    Raises ``ValueError`` for an unrecognised system prompt — never guesses.
    """
    first_line = system_prompt.splitlines()[0].strip() if system_prompt else ""
    handler = _ROUTES.get(first_line)
    if handler is None:
        raise ValueError(
            f"unrecognised/unknown system prompt (first line: {first_line!r})"
        )
    return handler(user_prompt)


def embed(texts: list[str], *, dim: int = 768) -> list[list[float]]:
    """Deterministic, unit-norm pseudo-embeddings — same text -> same
    vector, different text -> a different vector, no network call."""
    out: list[list[float]] = []
    for text in texts:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        values: list[float] = []
        counter = 0
        while len(values) < dim:
            block = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
            for i in range(0, len(block), 2):
                if len(values) >= dim:
                    break
                raw = int.from_bytes(block[i : i + 2], "big")
                values.append((raw / 65535.0) * 2 - 1)
            counter += 1
        norm = sum(v * v for v in values) ** 0.5
        if norm == 0:
            values[0] = 1.0
            norm = 1.0
        out.append([v / norm for v in values])
    return out
