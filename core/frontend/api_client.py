"""Thin sync HTTP client wrapping the FastAPI backend for the Flask viewer.

One function per consumed backend route (Phase 6's ``jobs``/``resumes``/
``shortlist`` routers). Every function accepts an optional keyword-only
``client: httpx.Client | None = None`` so callers (and tests) can inject an
``httpx.MockTransport``-backed client without any real network — when omitted,
:func:`build_client` constructs one from ``src.settings.Settings``.

**Redaction-boundary contract (mirrors the backend exactly):**

* ``list_shortlist``/``get_shortlist_entry`` take NO ``reveal`` parameter at
  all — shortlist reads are unconditionally blind, matching
  ``shortlist_service.list_for_job``/``get_one`` taking no such kwarg either.
* ``get_resume``'s ``reveal`` defaults to ``False`` — callers must opt in
  explicitly.
* ``get_match_results`` has no redaction concept (ADR-012 §4 — the backend
  applies none here either).

Errors are mapped to a small typed hierarchy so the Flask route layer can
handle them without inspecting raw ``httpx`` exceptions: a backend 404 raises
``NotFound``; a backend 5xx or an ``httpx.ConnectError`` raises
``BackendUnavailable``. Both subclass ``BackendError``.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

import httpx

from src.settings import get_settings

ExportFormat = Literal["csv", "evidence-csv", "json"]

# Explicit timeout so the client never relies on httpx's implicit default:
# 5s to establish a connection, 30s overall for connect/read/write/pool.
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


class BackendError(Exception):
    """Base class for all typed backend-communication failures."""


class NotFound(BackendError):  # noqa: N818 — name is a fixed part of the
    # Phase 7 viewer contract (test_frontend_api_client.py imports
    # `api_client.NotFound` verbatim); it subclasses `BackendError` (which
    # already carries the "Error" suffix) so intentionally omits its own.
    """The backend responded 404 — the requested resource does not exist."""


class BackendUnavailable(BackendError):  # noqa: N818 — same rationale as
    # `NotFound` above: the name is pinned by the test contract.
    """The backend responded 5xx, or the connection itself failed."""


class BadRequest(BackendError):  # noqa: N818 — matches the `NotFound` /
    # `BackendUnavailable` naming convention (subclasses `BackendError`, which
    # already carries the "Error" suffix).
    """The backend rejected the request with a 4xx (e.g. 422 validation).

    Carries the backend ``status_code`` and its parsed ``detail`` body so the
    Flask route layer can re-render the offending form with a friendly message
    and the recruiter's inputs intact (no data loss).
    """

    def __init__(self, message: str, *, status_code: int, detail: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class Conflict(BadRequest):  # noqa: N818 — same naming convention as the
    # other typed errors (subclasses `BadRequest`/`BackendError`, which already
    # carry the "Error" suffix).
    """The backend responded 409 — an illegal state transition (e.g. an
    illegal job-status edge). A specialisation of :class:`BadRequest` so the
    route can surface it as a friendly message while still catching it as a
    generic 4xx if it wants to."""


def build_client() -> httpx.Client:
    """Build an ``httpx.Client`` bound to ``settings.api_base_url``.

    Attaches an ``X-API-Key`` header IFF ``settings.api_key`` is non-empty,
    mirroring ``src.api.deps.require_api_key``'s empty-disables-auth
    semantics. Must not crash on a non-ASCII key (client-side mirror of
    ADR-012 §1's SEC-1 fix) — httpx's ``Headers`` defaults to ASCII-only
    encoding for ``str`` values and raises ``UnicodeEncodeError`` on a
    non-ASCII one, so the header value is explicitly UTF-8-encoded here
    (mirroring ``require_api_key`` comparing UTF-8 bytes server-side).
    """
    settings = get_settings()
    headers: dict[str, str] = {}
    if settings.api_key:
        headers["X-API-Key"] = settings.api_key
    return httpx.Client(
        base_url=settings.api_base_url,
        headers=httpx.Headers(headers, encoding="utf-8"),
        timeout=_HTTP_TIMEOUT,
    )


def _client_or_default(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    """Return ``(client, owns_it)`` — ``owns_it`` tells the caller whether to
    close the client after use (only when we built it ourselves)."""
    if client is not None:
        return client, False
    return build_client(), True


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 404:
        raise NotFound(f"backend 404: {response.request.url}")
    if response.status_code >= 500:
        raise BackendUnavailable(
            f"backend {response.status_code}: {response.request.url}"
        )
    if response.status_code >= 400:
        detail: Any
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        message = f"backend {response.status_code}: {response.request.url}"
        if response.status_code == 409:
            raise Conflict(message, status_code=response.status_code, detail=detail)
        raise BadRequest(message, status_code=response.status_code, detail=detail)


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: Any | None = None,
    data: dict[str, Any] | None = None,
    files: Any | None = None,
    client: httpx.Client | None = None,
) -> httpx.Response:
    active, owns_it = _client_or_default(client)
    try:
        try:
            response = active.request(
                method, path, params=params, json=json, data=data, files=files
            )
        except httpx.ConnectError as exc:
            raise BackendUnavailable(f"connection failed: {exc}") from exc
        _raise_for_status(response)
        return response
    finally:
        if owns_it:
            active.close()


def list_jobs(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    client: httpx.Client | None = None,
) -> Any:
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status is not None:
        params["status"] = status
    response = _request("GET", "/jobs", params=params, client=client)
    return response.json()


def create_job(payload: dict[str, Any], *, client: httpx.Client | None = None) -> Any:
    """POST /jobs (JSON ``JobCreate``). A backend 422 surfaces as
    ``BadRequest`` so the route can re-render the form with inputs intact."""
    response = _request("POST", "/jobs", json=payload, client=client)
    return response.json()


def extract_jd(
    filename: str,
    content: bytes,
    content_type: str,
    *,
    client: httpx.Client | None = None,
) -> Any:
    """POST /jobs/jd-extract (multipart ``file=``) → ``{filename,text,chars}``."""
    files = {"file": (filename, content, content_type)}
    response = _request("POST", "/jobs/jd-extract", files=files, client=client)
    return response.json()


def get_job(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/jobs/{job_id}", client=client)
    return response.json()


def transition_status(
    job_id: UUID, to: str, *, client: httpx.Client | None = None
) -> Any:
    """PATCH /jobs/{id}/status (JSON ``{to}``). A backend 409 (illegal
    transition) surfaces as ``Conflict`` so the route can show a friendly
    message."""
    response = _request(
        "PATCH", f"/jobs/{job_id}/status", json={"to": to}, client=client
    )
    return response.json()


def patch_job(
    job_id: UUID, payload: dict[str, Any], *, client: httpx.Client | None = None
) -> Any:
    """PATCH /jobs/{id} (partial JSON, e.g. ``{"blind_review": false}``)."""
    response = _request("PATCH", f"/jobs/{job_id}", json=payload, client=client)
    return response.json()


def list_resumes(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/jobs/{job_id}/resumes", client=client)
    return response.json()


def upload_resumes(
    job_id: UUID,
    files: list[tuple[str, bytes, str]],
    *,
    consent_acknowledged: bool,
    cover_letter_text: str | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """POST /jobs/{id}/resumes (multipart).

    ``files`` is a list of ``(filename, content, content_type)`` tuples,
    forwarded as repeated ``files=`` parts (a ``.zip`` is expanded *server*-side
    — we only forward the raw bytes, never expand it here). ``consent_acknowledged``
    is sent as the string ``"true"``/``"false"`` form field the backend expects
    (it accepts iff ``.strip().lower() == "true"``). An optional
    ``cover_letter_text`` form field is included only when provided. A backend
    4xx surfaces as ``BadRequest`` so the route can re-render with a message."""
    multipart = [
        ("files", (filename, content, ctype)) for filename, content, ctype in files
    ]
    form: dict[str, Any] = {
        "consent_acknowledged": "true" if consent_acknowledged else "false"
    }
    if cover_letter_text is not None:
        form["cover_letter_text"] = cover_letter_text
    response = _request(
        "POST",
        f"/jobs/{job_id}/resumes",
        data=form,
        files=multipart,
        client=client,
    )
    return response.json()


def generate_shortlist(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    """POST /jobs/{id}/shortlist — enqueues the ranking job. Returns the
    enqueue ack ``{job_id, status: "enqueued"}`` (results appear
    asynchronously; the caller polls ``list_shortlist`` for them)."""
    response = _request("POST", f"/jobs/{job_id}/shortlist", client=client)
    return response.json()


def list_shortlist(job_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/jobs/{job_id}/shortlist", client=client)
    return response.json()


def get_shortlist_entry(entry_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/shortlist/{entry_id}", client=client)
    return response.json()


def get_resume(
    resume_id: UUID,
    *,
    reveal: bool = False,
    client: httpx.Client | None = None,
) -> Any:
    response = _request(
        "GET", f"/resumes/{resume_id}", params={"reveal": reveal}, client=client
    )
    return response.json()


def reveal_resume(
    resume_id: UUID,
    *,
    context: str | None = None,
    client: httpx.Client | None = None,
) -> Any:
    """AUDITED de-anonymization. ``POST /resumes/{id}/reveal`` records a reveal
    audit row server-side and returns the UN-blinded ``ResumeOut``. This is the
    ONLY path the viewer reveals through — ``GET ?reveal=true`` would skip the
    audit, so it is never used here."""
    params = {"context": context} if context is not None else None
    response = _request(
        "POST", f"/resumes/{resume_id}/reveal", params=params, client=client
    )
    return response.json()


def get_match_results(resume_id: UUID, *, client: httpx.Client | None = None) -> Any:
    response = _request("GET", f"/resumes/{resume_id}/match-results", client=client)
    return response.json()


def export_shortlist(
    job_id: UUID,
    *,
    format: ExportFormat = "csv",
    reveal: bool = False,
    client: httpx.Client | None = None,
) -> httpx.Response:
    """Returns the raw ``httpx.Response`` so the Flask route can proxy
    ``Content-Disposition``/body straight through without re-encoding it."""
    return _request(
        "GET",
        f"/jobs/{job_id}/shortlist/export",
        params={"format": format, "reveal": reveal},
        client=client,
    )


__all__ = [
    "BackendError",
    "NotFound",
    "BackendUnavailable",
    "BadRequest",
    "Conflict",
    "build_client",
    "list_jobs",
    "create_job",
    "extract_jd",
    "get_job",
    "transition_status",
    "patch_job",
    "list_resumes",
    "upload_resumes",
    "generate_shortlist",
    "list_shortlist",
    "get_shortlist_entry",
    "get_resume",
    "reveal_resume",
    "get_match_results",
    "export_shortlist",
]
