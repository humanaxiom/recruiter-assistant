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

from flask import (
    Flask,
    Response,
    abort,
    redirect,
    render_template,
    request,
    url_for,
)

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


_JOB_STATUSES = ("draft", "open", "closed", "archived")


@app.get("/")
def index() -> Any:
    status = request.args.get("status") or None
    try:
        jobs = api_client.list_jobs(status=status)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "index.html",
        jobs=jobs,
        statuses=_JOB_STATUSES,
        status_filter=status,
        form={},
        errors=None,
        show_form=False,
    )


def _job_create_payload(form: Any) -> dict[str, Any]:
    """Build the ``JobCreate`` dict from the submitted form. Empty optional
    fields collapse to ``None``; the Blind-review checkbox is present in the
    form IFF checked (default checked) — absence means the recruiter opted
    out."""
    min_years_raw = (form.get("min_years") or "").strip()
    min_years: int | None
    try:
        min_years = int(min_years_raw) if min_years_raw else None
    except ValueError:
        min_years = None
    return {
        "title": (form.get("title") or "").strip(),
        "department": (form.get("department") or "").strip() or None,
        "location": (form.get("location") or "").strip() or None,
        "min_years": min_years,
        "description_raw": form.get("description_raw") or "",
        "blind_review": "blind_review" in form,
    }


@app.post("/jobs/jd-extract")
def jd_extract() -> Any:
    """Proxy a JD upload to the backend extractor and return the extracted
    text as the HTMX swap fragment that prefills the ``#description``
    textarea."""
    upload = request.files.get("file")
    if upload is None:
        abort(400)
    try:
        result = api_client.extract_jd(
            upload.filename or "upload",
            upload.read(),
            upload.content_type or "application/octet-stream",
        )
    except api_client.BadRequest:
        return "Could not extract text from this file.", 200
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return str(result.get("text", ""))


@app.post("/jobs")
def create_job() -> Any:
    payload = _job_create_payload(request.form)
    try:
        job = api_client.create_job(payload)
    except api_client.BadRequest as exc:
        try:
            jobs = api_client.list_jobs()
        except api_client.BackendUnavailable as unavail:
            return _unavailable(unavail)
        return (
            render_template(
                "index.html",
                jobs=jobs,
                statuses=_JOB_STATUSES,
                status_filter=None,
                form=request.form,
                errors=_format_error(exc.detail),
                show_form=True,
            ),
            200,
        )
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return redirect(url_for("job_detail", job_id=job["id"]))


def _format_error(detail: Any) -> str:
    """Render a backend validation ``detail`` into a short human message."""
    if detail is None:
        return "Please correct the highlighted fields and try again."
    if isinstance(detail, dict):
        inner = detail.get("detail", detail)
        return str(inner)
    return str(detail)


# Legal job-status edges (mirrors the backend's transition guard):
# draft→{open,archived}, open→{closed,archived}, closed→{archived}.
_LEGAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("open", "archived"),
    "open": ("closed", "archived"),
    "closed": ("archived",),
    "archived": (),
}
_TRANSITION_LABELS: dict[str, str] = {
    "open": "Open for applicants",
    "closed": "Close",
    "archived": "Archive",
}


def _render_job_detail(
    job_id: UUID, *, error: str | None = None, status_code: int = 200
) -> Any:
    try:
        job = api_client.get_job(job_id)
        resumes = api_client.list_resumes(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    next_states = _LEGAL_TRANSITIONS.get(job.get("status", ""), ())
    return (
        render_template(
            "job_detail.html",
            job=job,
            resumes=resumes,
            next_states=next_states,
            transition_labels=_TRANSITION_LABELS,
            error=error,
        ),
        status_code,
    )


@app.get("/jobs/<uuid:job_id>")
def job_detail(job_id: UUID) -> Any:
    return _render_job_detail(job_id)


@app.get("/jobs/<uuid:job_id>/parse-status")
def parse_status(job_id: UUID) -> Any:
    """HTMX poll fragment. While ``parsed_at`` is null it renders a
    ``parsing…`` badge AND keeps its ``hx-trigger`` so the browser polls again;
    once the LLM sets ``parsed_at`` it renders the required-skill pills WITHOUT
    the trigger, so polling stops."""
    try:
        job = api_client.get_job(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template("parse_status.html", job=job)


@app.post("/jobs/<uuid:job_id>/status")
def transition_status(job_id: UUID) -> Any:
    to = (request.form.get("to") or "").strip()
    try:
        api_client.transition_status(job_id, to)
    except api_client.Conflict as exc:
        return _render_job_detail(
            job_id, error=_format_error(exc.detail), status_code=409
        )
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return redirect(url_for("job_detail", job_id=job_id))


@app.post("/jobs/<uuid:job_id>/blind-review")
def blind_review(job_id: UUID) -> Any:
    desired = (request.form.get("blind_review") or "").strip().lower() in (
        "true",
        "1",
        "on",
        "yes",
    )
    try:
        api_client.patch_job(job_id, {"blind_review": desired})
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return redirect(url_for("job_detail", job_id=job_id))


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
