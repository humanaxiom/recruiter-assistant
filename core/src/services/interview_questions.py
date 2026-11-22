"""Interview-question generator -- the PURE layer (slice S1).

Turns the requirements a candidate's evidence does NOT cover into interview
questions. No DB, no settings, no network: the worker (S2) feeds this module
the blind-redacted ``ShortlistEntry`` DTO and persists what comes back.

The rules, each of which exists because the obvious implementation is a harm
on a hiring screen:

* **The evidence cliff (ADR-040).** ``evidence_evaluated is not True`` is a
  typed refusal: a candidate ranked past ``evidence_k`` was never examined, and
  "not examined" must never be presented as "missing".
* **Cite a gap or be dropped.** Every question must name a supplied
  requirement index, and any quote it claims to probe must be a substring of
  the quote we supplied.
* **No identifiers in, none out.** The prompt only takes ``Gap`` objects
  (requirement text, status, and the quote of PARTIAL rows), and
  ``filter_questions`` drops any question that echoes name, email or phone.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from src.pipeline.llm.client import REASONING_JSON_MIN_TOKENS, LLMClient
from src.prompts import load_prompt
from src.schemas.matching import CleanText, EvidenceStatus, ShortlistEntry
from src.services.redaction import _EMAIL_RE, _MIN_PHONE_DIGITS, _PHONE_CAND_RE

PROMPT_VERSION = "interview_questions_v1"

MAX_QUESTIONS_TOTAL = 12
MAX_QUESTIONS_PER_GAP = 3
MAX_QUESTION_CHARS = 400
# Raw-list bound applied BEFORE per-item validation (the DoS bound, as in
# ``EvidenceObjectIngest``). Generous so that the drop rules still see the
# items they are meant to drop.
_MAX_RAW_ITEMS = MAX_QUESTIONS_TOTAL * 4

_MIN_NAME_TOKEN_CHARS = 2
_MIN_SHORT_PHONE_DIGITS = 7


class RefusalReason(enum.StrEnum):
    NOT_EVALUATED = "not_evaluated"
    NO_REQUIREMENTS = "no_requirements"
    NO_GAPS = "no_gaps"


_REFUSAL_MESSAGES = {
    RefusalReason.NOT_EVALUATED: "this candidate's evidence was not evaluated",
    RefusalReason.NO_REQUIREMENTS: "the job has no requirements to ask about",
    RefusalReason.NO_GAPS: "every requirement is evidenced in the resume",
}


class InterviewRefusal(Exception):  # noqa: N818 -- name is pinned by the tests/plan
    """A typed, PII-free refusal to generate. ``reason`` is a fixed set."""

    def __init__(self, reason: RefusalReason) -> None:
        super().__init__(_REFUSAL_MESSAGES[reason])
        self.reason = reason


@dataclass(frozen=True)
class Gap:
    """One requirement not evidenced in the résumé.

    ``index`` is the position in ``entry.evidence.requirements``. ``quote`` is
    set for ``partial`` rows only.
    """

    index: int
    requirement: str
    status: EvidenceStatus
    quote: str | None = None


def select_gaps(entry: ShortlistEntry) -> list[Gap]:
    """The missing/partial requirements of ``entry``, in order.

    Raises ``InterviewRefusal`` for the evidence cliff, an empty requirement
    list, or when nothing is a gap -- in that precedence.
    """
    if entry.evidence_evaluated is not True:
        raise InterviewRefusal(RefusalReason.NOT_EVALUATED)
    rows = entry.evidence.requirements if entry.evidence is not None else []
    if not rows:
        raise InterviewRefusal(RefusalReason.NO_REQUIREMENTS)
    gaps: list[Gap] = []
    for index, row in enumerate(rows):
        if row.status == "met":
            continue
        quote = row.evidence if row.status == "partial" and row.evidence else None
        gaps.append(
            Gap(
                index=index,
                requirement=row.requirement,
                status=row.status,
                quote=quote,
            )
        )
    if not gaps:
        raise InterviewRefusal(RefusalReason.NO_GAPS)
    return gaps


def gaps_hash(gaps: Sequence[Gap]) -> str:
    """sha256 over (index, requirement, status) of each gap, order-sensitive.

    The quote is excluded: it is a display detail, and re-wording a quote must
    not mark stored questions stale. JSON framing keeps boundary shifts from
    colliding.
    """
    payload = json.dumps(
        [[g.index, g.requirement, g.status] for g in gaps],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class InterviewQuestionItem(BaseModel):
    """One generated question."""

    model_config = ConfigDict(extra="ignore")

    requirement_index: int
    question: CleanText
    probes_quote: CleanText | None = None


class InterviewQuestionsIngest(BaseModel):
    """LLM-output schema -- the ``chat_json`` target. Caps SCRUB, never raise,
    so one excess or over-long item cannot cost the whole payload."""

    model_config = ConfigDict(extra="ignore")

    questions: list[InterviewQuestionItem]

    @model_validator(mode="before")
    @classmethod
    def _bound_list_before_item_validation(cls, data: Any) -> Any:
        if isinstance(data, dict):
            value = data.get("questions")
            if isinstance(value, list) and len(value) > _MAX_RAW_ITEMS:
                data = {**data, "questions": value[:_MAX_RAW_ITEMS]}
        return data

    @model_validator(mode="after")
    def _enforce_caps(self) -> InterviewQuestionsIngest:
        kept: list[InterviewQuestionItem] = []
        per_gap: dict[int, int] = {}
        for item in self.questions:
            text = item.question.strip()
            # DROP an overlong question, never truncate it: a cut-off
            # question reads as a different question.
            if not text or len(text) > MAX_QUESTION_CHARS:
                continue
            seen = per_gap.get(item.requirement_index, 0)
            if seen >= MAX_QUESTIONS_PER_GAP:
                continue
            per_gap[item.requirement_index] = seen + 1
            item.question = text
            kept.append(item)
            if len(kept) >= MAX_QUESTIONS_TOTAL:
                break
        self.questions = kept
        return self


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


REDACTED = "[redacted]"

# Per-character fold: NFKC, then NFKD with combining marks dropped, then
# casefold. Done per character so every folded character maps back to the
# ORIGINAL index it came from (a combining mark folds to nothing and is
# swallowed into the match that precedes it).
_FOLD_CACHE: dict[str, str] = {}


def _fold_char(ch: str) -> str:
    cached = _FOLD_CACHE.get(ch)
    if cached is None:
        decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", ch))
        cached = "".join(c for c in decomposed if not unicodedata.combining(c))
        cached = cached.casefold()
        _FOLD_CACHE[ch] = cached
    return cached


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    folded: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        piece = _fold_char(ch)
        folded.append(piece)
        index.extend([i] * len(piece))
    return "".join(folded), index


def _fold(text: str) -> str:
    return _fold_with_map(text)[0]


def _name_pattern(name: str | None) -> re.Pattern[str] | None:
    """Whole-token matcher over the FOLDED text, so ``Jose Nunez`` and a
    composed or decomposed ``José Núñez`` all meet. Match against ``_fold``."""
    if not name:
        return None
    tokens = [
        t for t in re.findall(r"\w+", _fold(name)) if len(t) >= _MIN_NAME_TOKEN_CHARS
    ]
    if not tokens:
        return None
    alternation = "|".join(re.escape(t) for t in sorted(set(tokens)))
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


def _redact_names(text: str, name_re: re.Pattern[str]) -> str:
    folded, index = _fold_with_map(text)
    spans: list[tuple[int, int]] = []
    for m in name_re.finditer(folded):
        start = index[m.start()]
        end = index[m.end() - 1] + 1
        # swallow combining marks that follow the last matched character
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        spans.append((start, end))
    for start, end in reversed(spans):
        text = text[:start] + REDACTED + text[end:]
    return text


_PHONE_LOOSE_SEP = r"[\s().\-]*"


def _phone_key(phone: str | None) -> str:
    """The stored phone's digits that identify it: its last 10 digits when it
    has at least 10 (a country code never decides a match), all of them when it
    has 7-9, ``""`` when too short to match safely."""
    digits = re.sub(r"\D", "", phone) if phone else ""
    if len(digits) >= _MIN_PHONE_DIGITS:
        return digits[-_MIN_PHONE_DIGITS:]
    return digits if len(digits) >= _MIN_SHORT_PHONE_DIGITS else ""


def _email_key(email: str | None) -> str | None:
    return email.strip().casefold() if email and email.strip() else None


def scrub_identifiers(
    text: str, *, name: str | None, email: str | None, phone: str | None
) -> str:
    """``text`` with the candidate's identifiers -- and ANY email-shaped or
    phone-shaped string -- replaced by ``[redacted]``. ``filter_questions``
    uses the SAME rules (it drops what this would change).

    Email first (it contains name tokens), then phone, then name -- so no
    fragment of an address survives a name substitution. The email and phone
    patterns are the blind redactor's own.
    """
    email_n = _email_key(email)
    if email_n is not None:
        text = re.sub(re.escape(email_n), REDACTED, text, flags=re.IGNORECASE)
        # The blind redactor (``get_one``) can already have replaced the local
        # part, leaving "[name redacted]@example.test": the full address no
        # longer matches, so redact the residual domain too.
        domain = email_n.rpartition("@")[2]
        if "." in domain:
            text = re.sub(re.escape(domain), REDACTED, text, flags=re.IGNORECASE)
    text = _EMAIL_RE.sub(REDACTED, text)

    key = _phone_key(phone)

    def _phone_sub(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group())
        if len(digits) >= _MIN_PHONE_DIGITS or (key and key in digits):
            return REDACTED
        return match.group()

    text = _PHONE_CAND_RE.sub(_phone_sub, text)
    if key and len(key) < _MIN_PHONE_DIGITS:
        # a 7-9 digit number is below the candidate pattern's reach when it is
        # written without separators; match its own digits directly
        loose = _PHONE_LOOSE_SEP.join(re.escape(d) for d in key)
        text = re.sub(rf"(?<!\d){loose}(?!\d)", REDACTED, text)
    name_re = _name_pattern(name)
    if name_re is not None:
        text = _redact_names(text, name_re)
    return text


def redact_gaps(
    gaps: Sequence[Gap], *, name: str | None, email: str | None, phone: str | None
) -> list[Gap]:
    """``gaps`` with identifiers scrubbed from each partial quote. A non-blind
    job's evidence quote can still carry the candidate's own name or contact
    details; this runs BEFORE the prompt is built and the same scrubbed gaps
    are what ``filter_questions`` checks ``probes_quote`` against."""
    return [
        Gap(
            index=g.index,
            requirement=scrub_identifiers(
                g.requirement, name=name, email=email, phone=phone
            ),
            status=g.status,
            quote=(
                scrub_identifiers(g.quote, name=name, email=email, phone=phone)
                if g.quote is not None
                else None
            ),
        )
        for g in gaps
    ]


def filter_questions(
    ingest: InterviewQuestionsIngest,
    gaps: Sequence[Gap],
    *,
    name: str | None,
    email: str | None,
    phone: str | None,
) -> tuple[list[InterviewQuestionItem], int]:
    """Keep the questions that cite a supplied gap, probe only a supplied quote
    and echo no candidate identifier. Returns ``(kept, dropped_count)``."""
    by_index = {g.index: g for g in gaps}

    def leaks(text: str) -> bool:
        return scrub_identifiers(text, name=name, email=email, phone=phone) != text

    kept: list[InterviewQuestionItem] = []
    dropped = 0
    for item in ingest.questions:
        gap = by_index.get(item.requirement_index)
        if gap is None:
            dropped += 1
            continue
        quote = item.probes_quote
        if quote is not None and quote.strip():
            if gap.quote is None or _norm(quote) not in _norm(gap.quote):
                dropped += 1
                continue
        if leaks(item.question) or (quote is not None and leaks(quote)):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def build_messages(gaps: Sequence[Gap]) -> list[dict[str, str]]:
    """The chat messages. Takes ONLY gaps, so nothing else can leak in."""
    return load_prompt(PROMPT_VERSION, gaps=gaps).messages


async def generate(client: LLMClient, gaps: Sequence[Gap]) -> InterviewQuestionsIngest:
    """One LLM call. The S2 worker owns retries, filtering and persistence."""
    return await client.chat_json(
        build_messages(gaps),
        InterviewQuestionsIngest,
        max_tokens=REASONING_JSON_MIN_TOKENS,
    )
