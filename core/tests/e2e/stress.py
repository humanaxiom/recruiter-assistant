"""Stress / functional-load harness — drives the REAL running product over
HTTP, exactly like ``tests/smoke``, but with N concurrent virtual users each
running the FULL recruiter workflow: extract a JD, create a job, upload
résumés (one with a cover letter), build + upload a candidate roster, declare
a work authorization, generate a shortlist, withdraw + reinstate a candidate,
export the shortlist, download a document, and read the audit log.

Run with:

    python -m tests.e2e.stress

Every knob is an environment variable (this repo's config-via-settings rule
does not apply to a throwaway ops script that lives outside ``src/``, but the
SHAPE — one place, defaulted, documented — is kept anyway):

    USERS                   number of concurrent virtual users (default 1)
    RESUMES_PER_USER        résumés uploaded per user, capped at 30 (default 3)
    FRONTEND                base URL of the Flask BFF (default http://frontend:5000)
    FIXTURES                fixtures directory (default /repo/fixtures)
    JD_PARSE_TIMEOUT        seconds (default 300, matches tests/smoke/conftest.py)
    RESUME_PARSE_TIMEOUT    seconds (default 900, matches tests/smoke/conftest.py)
    RANK_TIMEOUT            seconds (default 900, matches tests/smoke/conftest.py)
    REPORT_DIR              where report.json/report.md are written
                            (default /repo/report)

Precondition failures (frontend redirects to CAS, fixtures missing) FAIL
loudly — this harness never silently skips or reports a false green.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from tests.e2e.driver import (
    CSRF_HEADER,
    count_parsed_pills,
    form_for,
    hidden_value,
    is_parsing,
    page_token,
    withdraw_ids,
)
from tests.e2e.report import render_markdown, summarise
from tests.e2e.roster import RosterSpec, build_roster_csv

_MAX_RESUMES_PER_USER = 30

FRONTEND = os.environ.get("FRONTEND", "http://frontend:5000")
FIXTURES = Path(os.environ.get("FIXTURES", "/repo/fixtures"))
REPORT_DIR = Path(os.environ.get("REPORT_DIR", "/repo/report"))

USERS = int(os.environ.get("USERS", "1"))
RESUMES_PER_USER = int(os.environ.get("RESUMES_PER_USER", "3"))

JD_PARSE_TIMEOUT = int(os.environ.get("JD_PARSE_TIMEOUT", "300"))
RESUME_PARSE_TIMEOUT = int(os.environ.get("RESUME_PARSE_TIMEOUT", "900"))
RANK_TIMEOUT = int(os.environ.get("RANK_TIMEOUT", "900"))

_WITHDRAW_REASON = "stress: exercising the audited withdraw/reinstate path"


class StepError(RuntimeError):
    """A step failed (bad status code or a broken assertion about the page)."""


class StepTimeoutError(RuntimeError):
    """A step never reached the state it was polling for."""


class Recorder:
    """Per-run latency/error/timeout bookkeeping, shared across virtual users."""

    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.errors: dict[str, int] = defaultdict(int)
        self.timeouts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def record(self, step: str, elapsed_s: float) -> None:
        async with self._lock:
            self.samples[step].append(elapsed_s * 1000.0)

    async def error(self, step: str) -> None:
        async with self._lock:
            self.errors[step] += 1

    async def timeout(self, step: str) -> None:
        async with self._lock:
            self.timeouts[step] += 1


async def _timed(recorder: Recorder, step: str, coro: Any) -> Any:
    start = time.monotonic()
    try:
        result = await coro
    except StepTimeoutError:
        await recorder.timeout(step)
        raise
    except Exception:
        await recorder.error(step)
        raise
    else:
        await recorder.record(step, time.monotonic() - start)
        return result


async def _wait_async(
    what: str,
    probe: Callable[[], Awaitable[str]],
    timeout_s: int,
    *,
    poll_s: float = 3.0,
) -> str:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = await probe()
        if last:
            return last
        await asyncio.sleep(poll_s)
    raise StepTimeoutError(
        f"timed out after {timeout_s}s waiting for {what}; last={last!r}"
    )


def _pick_jd(user_index: int) -> Path:
    jds = sorted(FIXTURES.glob("JDs/*.docx"))
    if not jds:
        raise StepError(f"no JD fixtures found under {FIXTURES / 'JDs'}")
    return jds[user_index % len(jds)]


def _pick_resumes(user_index: int, k: int) -> list[Path]:
    """``k`` résumés for this user, always including exactly one résumé that
    has a matching ``*_cover_letter.pdf`` sibling."""
    resumes = sorted(FIXTURES.glob("resumes/*_resume.pdf"))
    covers = {
        p.stem.rsplit("_cover_letter", 1)[0]
        for p in FIXTURES.glob("resumes/*_cover_letter.pdf")
    }
    paired = [r for r in resumes if r.stem.rsplit("_resume", 1)[0] in covers]
    unpaired = [r for r in resumes if r.stem.rsplit("_resume", 1)[0] not in covers]
    if not paired:
        raise StepError("no résumé/cover-letter fixture pair found")
    pair = paired[user_index % len(paired)]
    rest = (unpaired + [p for p in paired if p != pair])[: max(0, k - 1)]
    chosen = [pair] + rest
    if len(chosen) < k:
        raise StepError(f"not enough résumé fixtures to satisfy RESUMES_PER_USER={k}")
    return chosen[:k]


def _cover_letter_for(resume: Path) -> Path | None:
    stem = resume.stem.rsplit("_resume", 1)[0]
    candidates = list(resume.parent.glob(f"{stem}_cover_letter.*"))
    return candidates[0] if candidates else None


async def _page_token(client: httpx.AsyncClient, path: str = "/") -> str:
    resp = await client.get(path)
    token = page_token(resp.text)
    if not token:
        raise StepError(f"no page CSRF token on {path}")
    return token


async def _check_preconditions(client: httpx.AsyncClient) -> None:
    try:
        root = await client.get("/")
    except httpx.HTTPError as exc:
        raise StepError(f"cannot reach {FRONTEND} ({exc})") from exc
    if (
        root.status_code in (301, 302)
        and "cas" in root.headers.get("location", "").lower()
    ):
        raise StepError(
            "CAS is ENABLED on this stack; the stress harness needs CAS off"
        )
    if root.status_code != 200:
        raise StepError(f"frontend returned {root.status_code} for /")
    if not await asyncio.to_thread(FIXTURES.is_dir):
        raise StepError(f"fixtures directory {FIXTURES} not found")


async def _run_user(user_index: int, recorder: Recorder) -> None:
    label = f"U{user_index}"

    # Fixture bytes are read ONCE, up front, off the event loop
    # (asyncio.to_thread) and OUTSIDE every `_timed` block below — reading a
    # multi-MB PDF from disk is not part of what "jd_extract"/"resume_upload"
    # latency is measuring, and doing it inline was skewing both.
    jd = _pick_jd(user_index)
    jd_bytes = await asyncio.to_thread(jd.read_bytes)

    k = min(RESUMES_PER_USER, _MAX_RESUMES_PER_USER)
    resumes = _pick_resumes(user_index, k)
    cover = _cover_letter_for(resumes[0])
    resume_bytes = [await asyncio.to_thread(r.read_bytes) for r in resumes]
    cover_bytes = await asyncio.to_thread(cover.read_bytes) if cover else None

    async with httpx.AsyncClient(
        base_url=FRONTEND,
        timeout=120.0,
        follow_redirects=False,
        headers={"Origin": FRONTEND, "Referer": FRONTEND + "/"},
    ) as client:
        token = await _timed(recorder, "page_token", _page_token(client))

        async def _extract() -> str:
            resp = await client.post(
                "/jobs/jd-extract",
                files={
                    "file": (
                        jd.name,
                        jd_bytes,
                        "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document",
                    )
                },
                headers={CSRF_HEADER: token},
            )
            if resp.status_code != 200:
                raise StepError(f"jd-extract -> {resp.status_code}: {resp.text[:200]}")
            return resp.text

        description = await _timed(recorder, "jd_extract", _extract())

        async def _create_job() -> str:
            resp = await client.post(
                "/jobs",
                data={
                    "title": f"Stress {label} Position",
                    "department": "Stress",
                    "description_raw": description,
                    "additional_requirements": "",
                    "shortlist_top_percent": "100",
                    "csrf_token": token,
                },
                headers={CSRF_HEADER: token},
            )
            if resp.status_code != 302:
                raise StepError(f"create job -> {resp.status_code}: {resp.text[:200]}")
            return resp.headers["location"].rstrip("/").rsplit("/", 1)[-1]

        job_id = await _timed(recorder, "create_job", _create_job())

        async def _jd_parsed() -> str:
            body = (await client.get(f"/jobs/{job_id}/parse-status")).text
            return "ok" if not is_parsing(body) else ""

        await _timed(
            recorder,
            "jd_parse_wait",
            _wait_async("JD parse", _jd_parsed, JD_PARSE_TIMEOUT),
        )

        open_token = await _page_token(client, f"/jobs/{job_id}")

        async def _open() -> None:
            resp = await client.post(
                f"/jobs/{job_id}/status",
                data={"to": "open", "csrf_token": open_token},
                headers={CSRF_HEADER: open_token},
            )
            if resp.status_code not in (200, 302):
                raise StepError(f"open job -> {resp.status_code}: {resp.text[:200]}")

        await _timed(recorder, "job_open", _open())

        upload_token = await _page_token(client, f"/jobs/{job_id}")

        async def _upload() -> None:
            files = [
                ("files", (r.name, b, "application/pdf"))
                for r, b in zip(resumes, resume_bytes, strict=True)
            ]
            data = {"consent_acknowledged": "true", "csrf_token": upload_token}
            if cover is not None and cover_bytes is not None:
                files.append(
                    ("cover_letter_file", (cover.name, cover_bytes, "application/pdf"))
                )
            resp = await client.post(
                f"/jobs/{job_id}/resumes",
                data=data,
                files=files,
                headers={CSRF_HEADER: upload_token},
                timeout=180.0,
            )
            if resp.status_code not in (200, 302):
                raise StepError(
                    f"upload resumes -> {resp.status_code}: {resp.text[:200]}"
                )

        await _timed(recorder, "resume_upload", _upload())

        async def _resumes_parsed() -> str:
            body = (await client.get(f"/jobs/{job_id}/resumes-table")).text
            return "ok" if count_parsed_pills(body) >= k else ""

        await _timed(
            recorder,
            "resume_parse_wait",
            _wait_async("résumés to parse", _resumes_parsed, RESUME_PARSE_TIMEOUT),
        )

        candidate_names = _extract_candidate_names(
            (await client.get(f"/jobs/{job_id}/resumes-table")).text
        )

        async def _roster() -> None:
            specs = [
                RosterSpec(
                    name=name,
                    email=f"{i}.{label.lower()}@example.invalid",
                    work_authorization_text="No restrictions",
                    apsa=False,
                    cupe=False,
                )
                for i, name in enumerate(candidate_names)
            ]
            if not specs:
                return
            csv_text = build_roster_csv(specs)
            roster_token = await _page_token(client, f"/jobs/{job_id}")
            resp = await client.post(
                f"/jobs/{job_id}/candidate-roster",
                data={"csrf_token": roster_token},
                files={"file": ("roster.csv", csv_text.encode("utf-8"), "text/csv")},
                headers={CSRF_HEADER: roster_token},
            )
            if resp.status_code not in (200, 302):
                # Status code + body LENGTH only — never the body. The
                # roster CSV this posts carries real candidate names, and an
                # error path that echoes response text risks echoing one
                # back into a log a recruiter never asked to be in.
                raise StepError(
                    f"candidate-roster -> {resp.status_code} "
                    f"(body {len(resp.content)} bytes)"
                )

        await _timed(recorder, "candidate_roster", _roster())

        async def _generate_shortlist() -> None:
            sl_token = await _page_token(client, f"/jobs/{job_id}/shortlist")
            resp = await client.post(
                f"/jobs/{job_id}/shortlist", headers={CSRF_HEADER: sl_token}
            )
            if resp.status_code not in (200, 202, 302):
                raise StepError(f"generate shortlist -> {resp.status_code}")

        await _timed(recorder, "shortlist_generate", _generate_shortlist())

        async def _shortlist_ranked() -> str:
            body = (await client.get(f"/jobs/{job_id}/shortlist")).text
            return body if "/withdraw" in body else ""

        shortlist_html = await _timed(
            recorder,
            "rank_wait",
            _wait_async("shortlist ranking", _shortlist_ranked, RANK_TIMEOUT),
        )

        ids = withdraw_ids(shortlist_html)
        if not ids:
            raise StepError("no ranked candidates on the shortlist")
        if any(len(cid) == 32 and "-" not in cid for cid in ids):
            raise StepError("raw hex candidate id leaked into the shortlist chip")

        async def _work_auth() -> None:
            sl_token = page_token(shortlist_html)
            if not sl_token:
                raise StepError("no page token on the shortlist page")
            resp = await client.post(
                f"/jobs/{job_id}/shortlist/{ids[0]}/work-authorization",
                data={"status": "eligible"},
                headers={CSRF_HEADER: sl_token},
            )
            if resp.status_code not in (200, 302):
                raise StepError(f"work-authorization -> {resp.status_code}")

        await _timed(recorder, "work_authorization", _work_auth())

        withdraw_id = ids[-1]

        async def _withdraw() -> None:
            body = (await client.get(f"/jobs/{job_id}/shortlist")).text
            form = form_for(body, f"/resumes/{withdraw_id}/withdraw")
            if form is None:
                raise StepError("no withdraw form for the chosen candidate")
            wd_token = hidden_value(form, "csrf_token")
            if not wd_token:
                raise StepError("no csrf_token in the withdraw form")
            resp = await client.post(
                f"/resumes/{withdraw_id}/withdraw",
                data={
                    "csrf_token": wd_token,
                    "context": "shortlist",
                    "job_id": job_id,
                    "reason": _WITHDRAW_REASON,
                },
            )
            if resp.status_code != 302:
                raise StepError(f"withdraw -> {resp.status_code}")

        await _timed(recorder, "withdraw", _withdraw())

        async def _reinstate() -> None:
            body = (await client.get(f"/resumes/{withdraw_id}")).text
            form = form_for(body, f"/resumes/{withdraw_id}/reinstate")
            if form is None:
                raise StepError("no reinstate form on the résumé page")
            re_token = hidden_value(form, "csrf_token")
            if not re_token:
                raise StepError("no csrf_token in the reinstate form")
            resp = await client.post(
                f"/resumes/{withdraw_id}/reinstate",
                data={"csrf_token": re_token},
            )
            if resp.status_code != 302:
                raise StepError(f"reinstate -> {resp.status_code}")

        await _timed(recorder, "reinstate", _reinstate())

        async def _export() -> None:
            resp = await client.get(f"/jobs/{job_id}/shortlist/export?format=csv")
            if resp.status_code != 200:
                raise StepError(f"shortlist export -> {resp.status_code}")

        await _timed(recorder, "shortlist_export", _export())

        active_id = ids[0]

        async def _document() -> None:
            body = (await client.get(f"/resumes/{active_id}")).text
            form = form_for(body, f"/resumes/{active_id}/document")
            if form is None:
                raise StepError("no document-download form on the résumé page")
            doc_token = hidden_value(form, "csrf_token")
            if not doc_token:
                raise StepError("no csrf_token in the document form")
            resp = await client.post(
                f"/resumes/{active_id}/document",
                data={"csrf_token": doc_token, "kind": "resume"},
            )
            if resp.status_code != 200:
                raise StepError(f"document download -> {resp.status_code}")

        await _timed(recorder, "resume_document", _document())

        async def _audit() -> None:
            resp = await client.get("/audit?action=withdraw_resume")
            if resp.status_code != 200:
                raise StepError(f"audit -> {resp.status_code}")

        await _timed(recorder, "audit", _audit())


_NAME_CELL_RE = re.compile(r"<td>([^<]+)</td>\s*<td>")


def _extract_candidate_names(resumes_table_html: str) -> list[str]:
    """Names rendered in the résumés table (blind review off): one ``<td>``
    per candidate row, before the file-name column."""
    return [
        m.strip()
        for m in _NAME_CELL_RE.findall(resumes_table_html)
        if m.strip() and m.strip() != "—"
    ]


def _user_run_errors(results: list[Any]) -> dict[str, int]:
    """How many virtual users raised outside any ``_timed`` step, keyed for
    ``summarise()``'s ``errors`` dict.

    Pure and pinned by a unit test (``tests/unit/test_stress_user_errors.py``)
    precisely because the bug this closes is invisible at the network layer:
    ``asyncio.gather(..., return_exceptions=True)`` on its own only PRINTS a
    failed user, it does not fail the run — a user that dies before its
    first ``_timed`` call (a missing page token, a bad fixture pick) left
    ``summarise()`` seeing only the steps that DID complete, which reported a
    clean PASS and exit 0 for a run that never finished at all.
    """
    n = sum(1 for r in results if isinstance(r, BaseException))
    return {"user_run": n} if n else {}


async def _main() -> int:
    if RESUMES_PER_USER > _MAX_RESUMES_PER_USER:
        print(
            f"RESUMES_PER_USER={RESUMES_PER_USER} exceeds the cap of "
            f"{_MAX_RESUMES_PER_USER} (CSRF one-shot-token budget per card — "
            "HANDOFF lesson 8). Lower it or split the run across more users.",
            file=sys.stderr,
        )
        return 1

    recorder = Recorder()

    async with httpx.AsyncClient(base_url=FRONTEND, timeout=30.0) as probe:
        await _check_preconditions(probe)

    tasks = [_run_user(i, recorder) for i in range(USERS)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            print(f"U{i} failed: {result!r}", file=sys.stderr)

    errors = dict(recorder.errors)
    for key, count in _user_run_errors(results).items():
        errors[key] = errors.get(key, 0) + count

    summary = summarise(
        dict(recorder.samples),
        errors=errors,
        timeouts=dict(recorder.timeouts),
    )
    await asyncio.to_thread(REPORT_DIR.mkdir, parents=True, exist_ok=True)
    report_json = json.dumps(summary, indent=2)
    report_md = render_markdown(summary)
    await asyncio.to_thread(
        (REPORT_DIR / "report.json").write_text, report_json, encoding="utf-8"
    )
    await asyncio.to_thread(
        (REPORT_DIR / "report.md").write_text, report_md, encoding="utf-8"
    )
    print(report_md)

    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
