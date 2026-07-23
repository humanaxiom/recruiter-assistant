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

import datetime as dt
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from frontend import api_client, csrf
from src.settings import get_settings

# The backend caps résumé uploads at ~10 MB/file and up to 20 files per
# request (`src.api.routes.resumes`), plus the (much smaller) JD-extract
# upload. This is a defensive total-body cap so an oversized multipart
# request is rejected by Werkzeug with 413 BEFORE it is buffered into Flask
# process memory, rather than after the backend's own per-file/file-count
# limits have a chance to run.
MAX_UPLOAD_BYTES = 210 * 1024 * 1024  # 20 files * 10 MB + headroom

_settings = get_settings()
app = Flask(__name__)
app.secret_key = _settings.flask_secret_key
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
API = _settings.api_base_url


# Routes reachable with no CAS session even when `cas_enabled=True` — just
# the liveness probe, so orchestration/health-checks never need a session.
_CAS_GATE_EXEMPT_PATHS = frozenset({"/health"})


@app.before_request
def _cas_auth_gate() -> Any:
    """FU-5 slice 11 (ADR-019 §10/§3/§10b) — gate unauthenticated browser
    access when CAS is enabled.

    Reads settings via a FRESH :func:`get_settings` call every request (never
    the frozen ``_settings`` captured once at import time below) so tests —
    and a real config reload — can flip ``cas_enabled`` without restarting the
    process. ``cas_enabled=False`` is an unconditional passthrough (dev mode,
    §10b) with NO call to the backend at all.

    When enabled, delegates the actual session check to the backend's own
    ``GET /auth/cas/user`` (never 401s) via :func:`api_client.get_cas_user` —
    this hook holds no session-validity logic of its own, it only acts on the
    backend's answer. A backend that cannot be reached fails CLOSED (503),
    never silently lets the request through as if it were authenticated.
    """
    settings = get_settings()
    if not settings.cas_enabled:
        return None
    if request.path in _CAS_GATE_EXEMPT_PATHS:
        return None
    try:
        status = api_client.get_cas_user()
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    if status.get("authenticated"):
        return None
    login_url = (
        f"{settings.cas_service_base_url.rstrip('/')}/auth/cas/login?"
        + urlencode({"next": request.path})
    )
    return redirect(login_url)


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


@app.post("/jobs/bulk")
def bulk_create_jobs() -> Any:
    """Bulk-JD upload: many JD files (or a ``.zip``) plus an optional CSV
    metadata manifest, forwarded to the backend which creates ONE draft job per
    file. Renders a created/duplicate/failed summary with a link back to the
    jobs list. The ``.zip`` is expanded SERVER-side by the backend — we only
    forward the raw parts, never expand here."""
    uploads = request.files.getlist("files")
    files: list[tuple[str, bytes, str]] = [
        (
            upload.filename or "upload",
            upload.read(),
            upload.content_type or "application/octet-stream",
        )
        for upload in uploads
        if upload.filename
    ]
    manifest_upload = request.files.get("manifest")
    manifest: tuple[str, bytes, str] | None = None
    if manifest_upload is not None and manifest_upload.filename:
        manifest = (
            manifest_upload.filename,
            manifest_upload.read(),
            manifest_upload.content_type or "text/csv",
        )
    try:
        results = api_client.bulk_create_jobs(files, manifest=manifest)
    except api_client.BadRequest as exc:
        return (
            render_template(
                "jobs_bulk.html", results=[], error=_format_error(exc.detail)
            ),
            200,
        )
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "jobs_bulk.html", results=results, summary=_summarise_bulk(results), error=None
    )


def _summarise_bulk(results: Any) -> dict[str, int]:
    """Created/duplicate/failed counts for the bulk-JD result summary — mirrors
    the résumé-upload ``_summarise_upload`` counting pattern."""
    rows = results if isinstance(results, list) else []
    return {
        "created": sum(1 for r in rows if r.get("outcome") == "created"),
        "duplicate": sum(1 for r in rows if r.get("outcome") == "duplicate"),
        "failed": sum(1 for r in rows if r.get("outcome") == "failed"),
    }


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


# A résumé row is "terminal" once the backend has finished parsing it (or
# given up). While ANY row is still uploaded/parsing the résumés table keeps
# its HTMX poll trigger; once every row is terminal the trigger is dropped so
# the browser stops polling.
_TERMINAL_RESUME_STATUSES = ("parsed", "failed")
# Defensive cap on the free-text cover letter before it ever hits the network.
_MAX_COVER_LETTER_CHARS = 20000
# Bound the shortlist "Generating…" poll so a job that never yields ranked rows
# (e.g. Generate clicked before any résumé parsed) stops after ~20 min at 3s/poll
# with a give-up message, instead of polling forever. Matches hris's safety valve.
_MAX_SHORTLIST_POLL_ATTEMPTS = 400
# Bound the reverse-match "Finding matching jobs…" poll with the same safety
# valve/cap as the shortlist poll: reverse_match_job runs asynchronously, so a
# résumé whose run never lands (or a stack with no worker) stops after the cap
# with a give-up message instead of polling forever.
_MAX_MATCH_POLL_ATTEMPTS = 400


def _any_resume_pending(resumes: list[dict[str, Any]]) -> bool:
    return any(r.get("status") not in _TERMINAL_RESUME_STATUSES for r in resumes)


def _any_resume_parsed(resumes: list[dict[str, Any]]) -> bool:
    return any(r.get("status") == "parsed" for r in resumes)


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
            resumes_pending=_any_resume_pending(resumes),
            next_states=next_states,
            transition_labels=_TRANSITION_LABELS,
            error=error,
        ),
        status_code,
    )


@app.post("/jobs/<uuid:job_id>/resumes")
def upload_resumes(job_id: UUID) -> Any:
    """Multipart résumé upload. Consent is MANDATORY: if the recruiter did not
    tick the consent checkbox we do NOT call the backend at all — we re-render
    the job detail with an error, so no candidate bytes ever leave the browser
    without an explicit PIPEDA/FIPPA acknowledgement."""
    consent = (request.form.get("consent_acknowledged") or "").strip().lower() == "true"
    if not consent:
        return _render_job_detail(
            job_id,
            error="You must confirm the candidate consented to this processing.",
            status_code=400,
        )
    uploads = request.files.getlist("files")
    files: list[tuple[str, bytes, str]] = [
        (
            upload.filename or "upload",
            upload.read(),
            upload.content_type or "application/octet-stream",
        )
        for upload in uploads
        if upload.filename
    ]
    cover_letter_raw = request.form.get("cover_letter_text")
    cover_letter_text: str | None = None
    if cover_letter_raw:
        cover_letter_text = cover_letter_raw[:_MAX_COVER_LETTER_CHARS]

    cover_upload = request.files.get("cover_letter_file")
    cover_letter_file: tuple[str, bytes, str] | None = None
    if cover_upload is not None and cover_upload.filename:
        cover_letter_file = (
            cover_upload.filename,
            cover_upload.read(),
            cover_upload.content_type or "application/octet-stream",
        )

    manifest_upload = request.files.get("pairing_manifest")
    pairing_manifest: tuple[str, bytes, str] | None = None
    if manifest_upload is not None and manifest_upload.filename:
        pairing_manifest = (
            manifest_upload.filename,
            manifest_upload.read(),
            manifest_upload.content_type or "application/json",
        )
    try:
        results = api_client.upload_resumes(
            job_id,
            files,
            consent_acknowledged=True,
            cover_letter_text=cover_letter_text,
            cover_letter_file=cover_letter_file,
            pairing_manifest=pairing_manifest,
        )
    except api_client.BadRequest as exc:
        return _render_job_detail(
            job_id, error=_format_error(exc.detail), status_code=400
        )
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    # Post-upload results summary (flash-style): the recruiter sees what
    # happened per-file — counts + any pairing warnings — on the job-detail
    # page they're redirected to. Warnings are the backend's STATIC English
    # strings, never filename-derived candidate PII.
    for message in _summarise_upload(results):
        flash(message)
    return redirect(url_for("job_detail", job_id=job_id))


def _summarise_upload(results: Any) -> list[str]:
    """Build a short human summary from the ``ResumeUploadResult[]`` the backend
    returns: an accepted/with-cover/duplicate/rejected count line, one line per
    rejection, and any per-file pairing warnings."""
    rows = results if isinstance(results, list) else []
    accepted = [r for r in rows if r.get("outcome") == "accepted"]
    with_cover = [r for r in accepted if r.get("cover_letter_filename")]
    duplicate = [r for r in rows if r.get("outcome") == "duplicate"]
    rejected = [r for r in rows if r.get("outcome") == "rejected"]

    summary = f"{len(accepted)} accepted"
    if with_cover:
        summary += f" ({len(with_cover)} with a cover letter)"
    if duplicate:
        summary += f", {len(duplicate)} duplicate"
    if rejected:
        summary += f", {len(rejected)} rejected"
    messages = [summary]
    for r in rejected:
        reason = r.get("reason") or "rejected"
        messages.append(f"Rejected {r.get('original_filename')}: {reason}")
    for r in rows:
        for warning in r.get("warnings") or []:
            messages.append(warning)
    return messages


@app.get("/jobs/<uuid:job_id>/resumes-table")
def resumes_table(job_id: UUID) -> Any:
    """HTMX poll fragment. While any résumé row is still uploaded/parsing it
    keeps its ``hx-trigger`` so the browser re-polls every 3s; once every row
    is terminal (parsed/failed) it renders without the trigger, so polling
    stops."""
    try:
        resumes = api_client.list_resumes(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "resumes_table.html",
        job_id=job_id,
        resumes=resumes,
        resumes_pending=_any_resume_pending(resumes),
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


def _mint_card_tokens(entries: list[dict[str, Any]] | None) -> dict[str, str]:
    """Mint one per-résumé CSRF token per shortlist card (FU-4/D4).

    Returns a ``str(resume_id) -> token`` mapping the card template indexes by
    its own entry's résumé id, so every card on one render carries an
    independently valid, independently one-shot token. Entries without a usable
    ``resume_id`` are skipped rather than minting an unusable slot.
    """
    tokens: dict[str, str] = {}
    for entry in entries or []:
        resume_id = entry.get("resume_id") if isinstance(entry, dict) else None
        if resume_id is None:
            continue
        tokens[str(resume_id)] = csrf.issue_token(resume_id)
    return tokens


@app.get("/jobs/<uuid:job_id>/shortlist")
def job_shortlist(job_id: UUID) -> Any:
    # Blind by design: no `reveal` kwarg is ever passed here — the shortlist
    # list read is unconditionally redacted, matching
    # `shortlist_service.list_for_job` accepting no such parameter either.
    try:
        entries = api_client.list_shortlist(job_id)
        # Gate "Generate": ranking a job with no parsed résumé yields an empty
        # shortlist and an endless "Generating…" poll — so disable the button
        # until at least one résumé has finished parsing.
        resumes = api_client.list_resumes(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "shortlist_list.html",
        job_id=job_id,
        entries=entries,
        any_resume_parsed=_any_resume_parsed(resumes),
        attempt=0,
        max_attempts=_MAX_SHORTLIST_POLL_ATTEMPTS,
        # The included `shortlist_cards.html` carries one reveal form per card,
        # all posting to the SAME guarded route, so each card needs its OWN
        # token keyed by its own résumé id (FU-4/D4).
        csrf_tokens=_mint_card_tokens(entries),
    )


@app.post("/jobs/<uuid:job_id>/shortlist")
def generate_shortlist(job_id: UUID) -> Any:
    """Enqueue the ranking job, then return the pollable ``shortlist-cards``
    fragment. Ranking runs asynchronously on the backend, so the fragment comes
    back empty → it shows "Generating…" and keeps its ``hx-trigger`` until the
    ranked entries appear."""
    try:
        api_client.generate_shortlist(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return _render_shortlist_cards(job_id)


@app.get("/jobs/<uuid:job_id>/shortlist-cards")
def shortlist_cards(job_id: UUID) -> Any:
    """HTMX poll fragment. While the (blind) shortlist read is still empty it
    renders "Generating…" AND keeps its ``hx-trigger`` so the browser re-polls
    every 3s; once ranked entries exist it renders the cards WITHOUT the
    trigger, so polling stops. A bounded ``attempt`` counter (clamped
    server-side) stops the poll after ``_MAX_SHORTLIST_POLL_ATTEMPTS`` with a
    give-up message, so a job that never produces entries doesn't poll forever."""
    attempt = request.args.get("attempt", default=0, type=int) or 0
    attempt = max(0, min(attempt, _MAX_SHORTLIST_POLL_ATTEMPTS))
    return _render_shortlist_cards(job_id, attempt=attempt)


def _render_shortlist_cards(job_id: UUID, *, attempt: int = 0) -> Any:
    # Blind by design: no `reveal` kwarg is ever passed here — the card-render
    # read is unconditionally redacted, exactly like the list read above.
    try:
        entries = api_client.list_shortlist(job_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "shortlist_cards.html",
        job_id=job_id,
        entries=entries,
        attempt=attempt,
        max_attempts=_MAX_SHORTLIST_POLL_ATTEMPTS,
        # Each poll re-renders the cards, so re-minting here keeps every card's
        # token in the swapped-in DOM in sync with its session slot. Re-minting
        # for a résumé already present overwrites that résumé's slot in place,
        # so repeated polls cannot grow the mapping or evict unrelated tokens.
        csrf_tokens=_mint_card_tokens(entries),
    )


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
        resume = api_client.get_resume(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    # `current_year` drives the skill-recency colour buckets in the template
    # (current/aging/stale). Passed in so the comparison stays deterministic
    # and doesn't need any candidate.* field.
    return render_template(
        "resume_detail.html",
        resume=resume,
        current_year=dt.date.today().year,
        revealed=False,
        # FU-4/D4: mint the one-shot anti-forgery token the reveal form posts
        # back, bound to THIS résumé id, so a cross-site auto-submit cannot
        # manufacture an audit row.
        csrf_token=csrf.issue_token(resume_id),
    )


@app.post("/resumes/<uuid:resume_id>/reveal")
def resume_reveal(resume_id: UUID) -> Any:
    """AUDITED de-anonymization (FU-1). Deliberately POST-only — a GET could be
    prefetched/link-crawled, but a reveal must be an explicit act. Calls the
    backend's audited reveal endpoint (which records who/what/when), then
    re-renders the résumé UN-blinded in place. Blind stays the default; this is
    the only path that surfaces identity.

    FU-4/D4: guarded by a session-bound one-shot CSRF token plus an
    ``Origin``/``Referer`` same-origin check. BOTH are evaluated BEFORE any call
    reaches the backend, so a rejected forgery attempt can never imply a
    ``reveal_audit`` row."""
    if not csrf.same_origin(request):
        abort(403)
    if not csrf.verify_and_consume(resume_id, request.form.get(csrf.FORM_FIELD)):
        abort(403)
    # `context` records WHERE the reveal was triggered (shortlist card vs the
    # résumé page) in the audit row; defaults to the résumé page.
    context = (request.form.get("context") or "resume_detail").strip()[:64]
    try:
        resume = api_client.reveal_resume(resume_id, context=context)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "resume_detail.html",
        resume=resume,
        current_year=dt.date.today().year,
        revealed=True,
    )


@app.get("/resumes/<uuid:resume_id>/match-results")
def resume_match_results(resume_id: UUID) -> Any:
    # Full-page view of the (candidate→jobs) reverse match. No redaction on this
    # path (ADR-012 §4 — the caller owns the résumé; jobs are not PII), so no
    # `reveal` kwarg is ever passed. Renders the pollable cards fragment so an
    # in-flight run keeps filling in as ranked jobs land.
    try:
        results = api_client.get_match_results(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "match_results.html",
        resume_id=resume_id,
        results=results,
        attempt=0,
        max_attempts=_MAX_MATCH_POLL_ATTEMPTS,
    )


@app.post("/resumes/<uuid:resume_id>/match-jobs")
def resume_match_jobs(resume_id: UUID) -> Any:
    """Enqueue the reverse-match job, then return the pollable match-results
    fragment. POST-only: a side-effecting trigger must never be a prefetchable
    GET. ``reverse_match_job`` runs asynchronously on the backend, so the
    fragment comes back empty → it shows "Finding matching jobs…" and keeps its
    ``hx-trigger`` until the ranked jobs appear (mirrors ``generate_shortlist``)."""
    try:
        api_client.trigger_reverse_match(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return _render_match_cards(resume_id)


@app.get("/resumes/<uuid:resume_id>/match-results-cards")
def resume_match_results_cards(resume_id: UUID) -> Any:
    """HTMX poll fragment (mirrors ``shortlist_cards``). While the reverse-match
    read is still empty AND the run is not yet done it renders "Finding matching
    jobs…" AND keeps its ``hx-trigger`` so the browser re-polls every 3s; once
    ranked jobs exist it renders them WITHOUT the trigger, so polling stops. A
    bounded ``attempt`` counter (clamped server-side) stops the poll after
    ``_MAX_MATCH_POLL_ATTEMPTS`` with a give-up message, so a run that never
    lands doesn't poll forever."""
    attempt = request.args.get("attempt", default=0, type=int) or 0
    attempt = max(0, min(attempt, _MAX_MATCH_POLL_ATTEMPTS))
    return _render_match_cards(resume_id, attempt=attempt)


def _render_match_cards(resume_id: UUID, *, attempt: int = 0) -> Any:
    # No redaction on this path (ADR-012 §4): the caller owns the résumé and
    # jobs are not PII, so real job titles/departments are shown here — this is
    # correct, unlike the blind shortlist. No `reveal` kwarg is ever passed.
    try:
        results = api_client.get_match_results(resume_id)
    except api_client.NotFound:
        abort(404)
    except api_client.BackendUnavailable as exc:
        return _unavailable(exc)
    return render_template(
        "match_results_cards.html",
        resume_id=resume_id,
        results=results,
        attempt=attempt,
        max_attempts=_MAX_MATCH_POLL_ATTEMPTS,
    )


_EXPORT_FORMATS: tuple[api_client.ExportFormat, ...] = ("csv", "evidence-csv", "json")


def _validated_export_format(raw: str | None) -> api_client.ExportFormat:
    """Validate a browser-supplied ``?format=`` against the allowed set,
    falling back to ``"csv"`` for a missing/unknown value. Never raises."""
    for candidate in _EXPORT_FORMATS:
        if raw == candidate:
            return candidate
    return "csv"


@app.get("/jobs/<uuid:job_id>/shortlist/export")
def shortlist_export(job_id: UUID) -> Any:
    """Server-side export proxy. Streams the backend response body straight
    through and preserves ``Content-Disposition``, without ever exposing the
    backend ``X-API-Key`` (attached only on the outbound leg by
    ``api_client.build_client``) to the browser — only the content type,
    disposition and body are copied onto the Flask response.

    Reads ``?format=`` from the query string and validates it against the
    allowed set, falling back to ``"csv"`` for a missing/unknown value
    (never raises). A browser-supplied ``?reveal=`` is deliberately never
    read or forwarded — exports stay anonymized (``reveal=False`` default)."""
    export_format = _validated_export_format(request.args.get("format"))
    try:
        backend_resp = api_client.export_shortlist(job_id, format=export_format)
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
