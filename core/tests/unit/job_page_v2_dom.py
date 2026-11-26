"""Shared test support for the S2 (Candidates tab) job-page-v2 test files.

Not a test module (no ``test_`` prefix), so pytest does not collect it. It holds
two things the four S2 files all need:

* a tiny DOM built with ``html.parser`` (never a regex over the page) so a test
  can ask "inside THIS card" or "inside THIS form" instead of grepping a blob;
* fixture builders. The job and the résumé rows are built from the DTO classes
  (``JobOut``, ``ResumeListItem``) and ``model_dump(mode="json")``, so every
  timestamp is a string exactly as in the BFF (HANDOFF section 4 lesson 6). The
  export and split read models have NO DTO class in this repo (the API routes
  return plain dicts), so those two are dicts shaped like the ones the existing
  ``test_frontend_resume_export_job.py`` and ``test_frontend_split_review.py``
  use.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from src.schemas.auth import User
from src.schemas.jobs import JDExtracted, JobOut, Skill
from src.schemas.matching import ScoreBreakdown, ShortlistEntry
from src.schemas.resumes import ResumeListItem, WorkAuthorization
from src.settings import Settings

JOB_ID = UUID("11111111-1111-4111-8111-111111111111")
EXPORT_ID = UUID("33333333-3333-4333-8333-333333333333")
TS = dt.datetime(2026, 9, 3, 8, 30, tzinfo=dt.UTC)
STAMP = "2026-11-20T12:00:00+00:00"

_VOID = frozenset({"input", "br", "img", "hr", "meta", "link", "source", "wbr"})


# ── a minimal DOM ───────────────────────────────────────────────────────────


def _has_attrs(node: Node, want: Mapping[str, str | None]) -> bool:
    for name, value in want.items():
        if value is None:
            if name not in node.attrs:
                return False
        elif node.attrs.get(name) != value:
            return False
    return True


class Node:
    """One element. ``children`` holds ``Node`` and ``str`` (text) in order."""

    def __init__(
        self, tag: str, attrs: Mapping[str, str | None], parent: Node | None = None
    ) -> None:
        self.tag = tag
        self.attrs: dict[str, str | None] = dict(attrs)
        self.parent = parent
        self.children: list[Node | str] = []

    def get(self, name: str) -> str | None:
        return self.attrs.get(name)

    def walk(self) -> Iterator[Node]:
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.walk()

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return " ".join("".join(parts).split())

    def find_all(
        self, tag: str | None = None, attrs: Mapping[str, str | None] | None = None
    ) -> list[Node]:
        """Descendants matching ``tag`` and every attr. A wanted value of None
        means "the attribute is present, whatever its value"."""
        want = dict(attrs or {})
        found: list[Node] = []
        for node in self.walk():
            if tag is not None and node.tag != tag:
                continue
            if _has_attrs(node, want):
                found.append(node)
        return found

    def find(
        self, tag: str | None = None, attrs: Mapping[str, str | None] | None = None
    ) -> Node | None:
        found = self.find_all(tag, attrs)
        return found[0] if found else None

    def within(self, other: Node) -> bool:
        node: Node | None = self
        while node is not None:
            if node is other:
                return True
            node = node.parent
        return False


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {})
        self._stack: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, dict(attrs), self._stack[-1])
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def parse(html: str) -> Node:
    builder = _Builder()
    builder.feed(html)
    builder.close()
    return builder.root


def hidden_inputs(form: Node) -> dict[str, str]:
    out: dict[str, str] = {}
    for inp in form.find_all("input", {"type": "hidden"}):
        name = inp.get("name")
        if name:
            out[name] = inp.get("value") or ""
    return out


def forms_posting_to(root: Node, path_suffix: str) -> list[Node]:
    found: list[Node] = []
    for form in root.find_all("form"):
        action = form.get("action") or ""
        method = (form.get("method") or "").lower()
        if action.endswith(path_suffix) and method == "post":
            found.append(form)
    return found


def tab_root(root: Node, tab: str = "candidates") -> Node | None:
    """The element carrying ``data-job-tab="<tab>"`` (the tab's content)."""
    return root.find(attrs={"data-job-tab": tab})


def card(root: Node, key: str) -> Node | None:
    return root.find(attrs={"data-intake-card": key})


# ── fixtures ────────────────────────────────────────────────────────────────

STATES: dict[str, dict[str, Any]] = {
    "parsing": {"status": "draft", "parsed_at": None, "description_parsed": None},
    "failed": {
        "status": "draft",
        "parsed_at": None,
        "description_parsed": None,
        "failure_reason": "The model returned no usable JSON.",
    },
    "parsed": {"status": "draft"},
    "open": {"status": "open"},
    "closed": {"status": "closed", "closed_at": TS},
    "archived": {"status": "archived", "closed_at": TS},
}


def make_job(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": JOB_ID,
        "title": "Multimedia Specialist",
        "department": "School of Medicine",
        "location": "Burnaby",
        "employment_type": None,
        "seniority": None,
        "min_years": 3,
        "description_raw": "A detailed job description of the role. " * 3,
        "description_parsed": JDExtracted(
            title="x", required_skills=[Skill(name="Video editing")]
        ),
        "status": "open",
        "retention_days": 180,
        "shortlist_top_percent": 100,
        "blind_review": False,
        "failure_reason": None,
        "created_by": "asalah",
        "created_at": TS,
        "updated_at": TS,
        "parsed_at": TS,
        "closed_at": None,
        "source": "manual",
    }
    base.update(over)
    return JobOut(**base).model_dump(mode="json")


def make_resume(index: int = 1, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": UUID(int=0x22222222000000000000000000000000 + index),
        "original_filename": f"applicant-{index:03d}.pdf",
        "status": "parsed",
        "uploaded_at": TS,
        "parsed_at": TS,
        "candidate_name": f"Candidate Number{index:03d}",
    }
    base.update(over)
    return ResumeListItem(**base).model_dump(mode="json")


def make_export(status: str = "ready", export_id: UUID = EXPORT_ID) -> dict[str, Any]:
    return {
        "id": str(export_id),
        "status": status,
        "counts": {"applicants": 3, "anonymized": 2, "not_anonymized": 1},
        "created_at": STAMP,
        "expires_at": STAMP if status == "ready" else None,
    }


def make_split(
    index: int = 1,
    status: str = "proposed",
    filename: str | None = "march-export.pdf",
) -> dict[str, Any]:
    return {
        "id": str(UUID(int=0x44444444000000000000000000000000 + index)),
        "status": status,
        "created_at": STAMP,
        "page_count": 8,
        "method": "llm",
        "source_filename": filename,
    }


@dataclass
class Spies:
    list_resumes: MagicMock
    list_resume_splits: MagicMock
    get_latest_resume_export: MagicMock


def _as_mock(value: Any) -> MagicMock:
    if isinstance(value, BaseException):
        return MagicMock(side_effect=value)
    return MagicMock(return_value=value)


def install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str = "open",
    role: str | None = None,
    flag: bool = True,
    resumes: list[dict[str, Any]] | None = None,
    splits: Any = None,
    export: Any = None,
    job_over: Mapping[str, Any] | None = None,
) -> Spies:
    """Settings, CAS session and stubbed ``api_client`` for the job page.

    ``role`` None means CAS off (a dev session, which is a writer). ``export``
    and ``splits`` may be an Exception instance to make the call raise;
    ``export=None`` means "no export yet" (the api_client raises NotFound).
    """
    settings = Settings(cas_enabled=role is not None, job_page_v2=flag)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    if role is not None:
        status = {
            "authenticated": True,
            "username": "jordan",
            "cas_enabled": True,
            "role": role,
        }
        monkeypatch.setattr(api_client, "get_cas_user", lambda **kw: status)
    job = make_job(**{**STATES[state], **dict(job_over or {})})
    rows = resumes if resumes is not None else [make_resume(1)]
    spies = Spies(
        list_resumes=MagicMock(return_value=rows),
        list_resume_splits=_as_mock(splits if splits is not None else []),
        get_latest_resume_export=_as_mock(
            export if export is not None else api_client.NotFound("none")
        ),
    )
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: job)
    monkeypatch.setattr(api_client, "list_resumes", spies.list_resumes)
    monkeypatch.setattr(api_client, "list_resume_splits", spies.list_resume_splits)
    monkeypatch.setattr(
        api_client, "get_latest_resume_export", spies.get_latest_resume_export
    )
    monkeypatch.setattr(api_client, "list_job_assignees", lambda jid, **kw: [])
    monkeypatch.setattr(api_client, "list_users", lambda **kw: [])
    monkeypatch.setattr(api_client, "list_shortlist", lambda jid, **kw: [])
    return spies


# ── S3: Settings reads and user rows ────────────────────────────────────────


def make_user(
    index: int = 1,
    *,
    cas_username: str | None = None,
    display_name: str | None = None,
    role: str = "hiring_manager",
) -> dict[str, Any]:
    """One ``users`` row as the API returns it (the ``User`` DTO, JSON mode)."""
    return User(
        id=UUID(int=0x77777777000000000000000000000000 + index),
        cas_username=cas_username or f"manager{index}",
        display_name=display_name,
        email=None,
        role=role,
        active=True,
        created_at=TS,
        last_seen_at=TS,
    ).model_dump(mode="json")


@dataclass
class ReadSpies:
    """The reads ``install`` stubs with plain lambdas, as spies."""

    list_job_assignees: MagicMock
    list_users: MagicMock
    list_shortlist: MagicMock

    def all(self) -> list[MagicMock]:
        return [self.list_job_assignees, self.list_users, self.list_shortlist]


def stub_settings_reads(
    monkeypatch: pytest.MonkeyPatch,
    *,
    assignees: Any = None,
    users: Any = None,
) -> ReadSpies:
    """Replace the Settings-tab reads with spies. Call AFTER ``install``.

    ``assignees`` / ``users`` are a list of user rows (default: empty) or an
    Exception instance to make the call raise (the handler maps a backend
    failure on ``list_job_assignees`` to ``None``, which the page words as
    "could not be loaded").
    """
    spies = ReadSpies(
        list_job_assignees=_as_mock(assignees if assignees is not None else []),
        list_users=_as_mock(users if users is not None else []),
        list_shortlist=MagicMock(return_value=[]),
    )
    monkeypatch.setattr(api_client, "list_job_assignees", spies.list_job_assignees)
    monkeypatch.setattr(api_client, "list_users", spies.list_users)
    monkeypatch.setattr(api_client, "list_shortlist", spies.list_shortlist)
    return spies


# ── S4: the Shortlist teaser ────────────────────────────────────────────────


def make_shortlist_entry(
    rank: int = 1,
    *,
    score: float | None = None,
    display_label: str | None = None,
    blinded: bool = False,
    resume_index: int | None = None,
    work_authorization: WorkAuthorization = "unknown",
) -> dict[str, Any]:
    """One ``/jobs/{id}/shortlist`` row (the ``ShortlistEntry`` DTO, JSON mode).

    ``score`` defaults to a value that is distinct for every rank, so a test can
    tell one row's score label from another's.
    """
    index = resume_index if resume_index is not None else rank
    final = score if score is not None else max(0.0, 0.99 - rank * 0.01)
    return ShortlistEntry(
        id=UUID(int=0x55555555000000000000000000000000 + rank),
        job_id=JOB_ID,
        resume_id=UUID(int=0x22222222000000000000000000000000 + index),
        rank=rank,
        score_final=final,
        score_breakdown=ScoreBreakdown(
            skill=0.5,
            experience=0.5,
            education=0.5,
            seniority=0.5,
            vector=0.5,
            structured=0.5,
        ),
        evidence=None,
        generated_at=TS,
        blinded=blinded,
        display_label=display_label or f"Candidate {rank}",
        work_authorization=work_authorization,
    ).model_dump(mode="json")


def stub_shortlist(monkeypatch: pytest.MonkeyPatch, entries: Any) -> MagicMock:
    """Replace ``list_shortlist`` with a spy. Call AFTER ``install``.

    ``entries`` is a list of rows or an Exception instance to make it raise.
    """
    spy = _as_mock(entries)
    monkeypatch.setattr(api_client, "list_shortlist", spy)
    return spy
