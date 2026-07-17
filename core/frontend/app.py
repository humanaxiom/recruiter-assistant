"""Flask frontend — Phase 7 read-only viewer.

Talks to the FastAPI backend over HTTP via ``frontend.api_client``. Every
route renders server-side only (Jinja2 templates, no client-side JS that
re-fetches raw/reveal endpoints) — the redaction boundary established by the
backend (ADR-011/012: résumés and shortlist entries are redacted server-side
before ever leaving the FastAPI process) is enforced HERE too, at the second
hop: this module never forwards a browser-supplied ``?reveal=`` query string
to the backend, and the shortlist list/detail routes never pass a ``reveal``
kwarg to ``api_client`` at all (mirroring ``shortlist_service`` itself taking
no such parameter).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from flask import Flask, Response, abort, render_template

from frontend import api_client
from src.settings import get_settings

_settings = get_settings()
app = Flask(__name__)
app.secret_key = _settings.flask_secret_key
API = _settings.api_base_url


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _unavailable(exc: api_client.BackendUnavailable) -> Any:
    return render_template("error.html"), 503


@app.get("/")
def index() -> Any:
    try:
        jobs = api_client.list_jobs()
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template("index.html", jobs=jobs)


@app.get("/jobs/<uuid:job_id>")
def job_detail(job_id: UUID) -> Any:
    try:
        job = api_client.get_job(job_id)
        resumes = api_client.list_resumes(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template("job_detail.html", job=job, resumes=resumes)


@app.get("/jobs/<uuid:job_id>/shortlist")
def job_shortlist(job_id: UUID) -> Any:
    # Blind by design: no `reveal` kwarg is ever passed here — the shortlist
    # list read is unconditionally redacted, matching
    # `shortlist_service.list_for_job` accepting no such parameter either.
    try:
        entries = api_client.list_shortlist(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template("shortlist_list.html", job_id=job_id, entries=entries)


@app.get("/shortlist/<uuid:entry_id>")
def shortlist_entry_detail(entry_id: UUID) -> Any:
    try:
        entry = api_client.get_shortlist_entry(entry_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template("shortlist_entry.html", entry=entry)


@app.get("/resumes/<uuid:resume_id>")
def resume_detail(resume_id: UUID) -> Any:
    # CRITICAL redaction-boundary: any browser-supplied `?reveal=` query
    # string is deliberately never read/forwarded — `reveal` is always
    # False here, so a visitor cannot re-introduce de-anonymization by
    # editing the URL.
    try:
        resume = api_client.get_resume(resume_id, reveal=False)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template("resume_detail.html", resume=resume)


@app.get("/resumes/<uuid:resume_id>/match-results")
def resume_match_results(resume_id: UUID) -> Any:
    try:
        results = api_client.get_match_results(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template("match_results.html", results=results)


@app.get("/jobs/<uuid:job_id>/shortlist/export")
def shortlist_export(job_id: UUID) -> Any:
    """Server-side export proxy. Streams the backend response body straight
    through and preserves ``Content-Disposition``, without ever exposing the
    backend ``X-API-Key`` (attached only on the outbound leg by
    ``api_client.build_client``) to the browser — only the content type,
    disposition and body are copied onto the Flask response."""
    try:
        backend_resp = api_client.export_shortlist(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)

    headers: dict[str, str] = {}
    content_disposition = backend_resp.headers.get("content-disposition")
    if content_disposition is not None:
        headers["Content-Disposition"] = content_disposition
    return Response(
        backend_resp.content,
        status=backend_resp.status_code,
        content_type=backend_resp.headers.get(
            "content-type", "application/octet-stream"
        ),
        headers=headers,
    )
