"""RED -- slice H3: the Flask side of the background résumé export.

``frontend.api_client`` (all raise through the usual ``_raise_for_status``)::

    start_resume_export(job_id, *, client=None) -> dict
        POST /jobs/{id}/resumes/exports            -> {"id", "status", ...}
    get_latest_resume_export(job_id, *, client=None) -> dict
        GET  /jobs/{id}/resumes/exports/latest     (404 -> NotFound)
    download_resume_export(export_id, *, client=None) -> tuple[bytes, str]
        POST /resume-exports/{id}/download         -> (zip bytes, the backend's
        Content-Disposition); 409 -> Conflict, 404 -> NotFound

``export_resumes_zip`` is removed with the synchronous route, and every
``httpx.TimeoutException`` is a ``BackendUnavailable``.

Flask routes (endpoint names are pinned)::

    POST /jobs/<id>/resumes/exports          endpoint ``export_start``
        ordinary page CSRF token (hook-protected, NOT exempt); 302 to the job.
    POST /resume-exports/<id>/download       endpoint ``export_download``
        ``csrf.same_origin`` + ``verify_and_consume(export_id, token,
        action="export")`` (exempt from the page-token hook, like
        ``resume_document``); the form also carries a hidden ``job_id`` so a
        409 can return to the job page. Streams ``application/zip`` with the
        backend's Content-Disposition, ``Cache-Control: no-store``.

``job_detail.html`` (writers only) has the start form labelled "Export
résumés (as-is + anonymized)" and, from the requester's latest export, its
status (in an element carrying ``data-export-status="<status>"``) -- and when
``ready`` a POST download form with a one-shot token.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

from frontend import api_client, csrf
from frontend import app as frontend_app_module
from frontend.app import app
from src.settings import Settings

_ZIP = b"PK\x03\x04 fake zip bytes"
_STAMP = "2026-11-20T12:00:00+00:00"


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


@pytest.fixture
def plain_client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _http(
    captured: list[httpx.Request], status: int, content: bytes = b"{}", **headers: str
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, content=content, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t")


# ---------------------------------------------------------------- api_client


def test_start_resume_export_posts_and_returns_the_json() -> None:
    job_id = uuid4()
    seen: list[httpx.Request] = []
    body = b'{"id": "abc", "status": "queued"}'

    out = api_client.start_resume_export(job_id, client=_http(seen, 202, body))

    assert out == {"id": "abc", "status": "queued"}
    (req,) = seen
    assert req.method == "POST"
    assert req.url.path == f"/jobs/{job_id}/resumes/exports"


@pytest.mark.parametrize("status", [403, 422])
def test_start_resume_export_4xx_is_bad_request(status: int) -> None:
    with pytest.raises(api_client.BadRequest):
        api_client.start_resume_export(uuid4(), client=_http([], status))


def test_start_resume_export_404_is_not_found() -> None:
    with pytest.raises(api_client.NotFound):
        api_client.start_resume_export(uuid4(), client=_http([], 404))


def test_get_latest_resume_export_gets_the_route() -> None:
    job_id = uuid4()
    seen: list[httpx.Request] = []

    out = api_client.get_latest_resume_export(
        job_id, client=_http(seen, 200, b'{"id": "x", "status": "ready"}')
    )

    assert out["status"] == "ready"
    (req,) = seen
    assert req.method == "GET"
    assert req.url.path == f"/jobs/{job_id}/resumes/exports/latest"


def test_get_latest_resume_export_404_is_not_found() -> None:
    with pytest.raises(api_client.NotFound):
        api_client.get_latest_resume_export(uuid4(), client=_http([], 404))


def test_download_resume_export_posts_and_returns_bytes_and_disposition() -> None:
    export_id = uuid4()
    seen: list[httpx.Request] = []
    cd = 'attachment; filename="job-1-resumes.zip"'

    body, disposition = api_client.download_resume_export(
        export_id, client=_http(seen, 200, _ZIP, **{"content-disposition": cd})
    )

    assert body == _ZIP
    assert isinstance(body, bytes)
    assert disposition == cd
    (req,) = seen
    assert req.method == "POST"
    assert req.url.path == f"/resume-exports/{export_id}/download"


def test_download_resume_export_409_is_a_conflict_not_a_generic_bad_request() -> None:
    with pytest.raises(api_client.Conflict):
        api_client.download_resume_export(uuid4(), client=_http([], 409))


def test_download_resume_export_404_is_not_found() -> None:
    with pytest.raises(api_client.NotFound):
        api_client.download_resume_export(uuid4(), client=_http([], 404))


def test_the_synchronous_export_client_function_is_gone() -> None:
    assert not hasattr(api_client, "export_resumes_zip")


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.WriteTimeout("slow"),
        httpx.PoolTimeout("slow"),
    ],
)
def test_any_httpx_timeout_is_backend_unavailable(exc: httpx.TimeoutException) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    stub = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t")

    with pytest.raises(api_client.BackendUnavailable):
        api_client.list_jobs(client=stub)


# ----------------------------------------------------------------- the page


def _job(job_id: Any, *, blind: bool = False) -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": "open",
        "blind_review": blind,
        "parsed_at": "2026-07-17T00:00:00Z",
        "description_parsed": {"required_skills": []},
    }


def _export(export_id: Any, status: str = "ready") -> dict[str, Any]:
    return {
        "id": str(export_id),
        "status": status,
        "counts": {"applicants": 3, "anonymized": 2, "not_anonymized": 1},
        "created_at": _STAMP,
        "expires_at": _STAMP if status == "ready" else None,
    }


def _page(
    monkeypatch: Any, client: Any, job_id: Any, latest: Any = None
) -> tuple[str, MagicMock]:
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    if isinstance(latest, Exception) or latest is None:
        spy = MagicMock(side_effect=latest or api_client.NotFound("none"))
    else:
        spy = MagicMock(return_value=latest)
    monkeypatch.setattr(api_client, "get_latest_resume_export", spy)
    return str(client.get(f"/jobs/{job_id}").get_data(as_text=True)), spy


def _form(body: str, path_suffix: str) -> str | None:
    m = re.search(
        r'<form[^>]*action="[^"]*' + re.escape(path_suffix) + r'"[^>]*>(.*?)</form>',
        body,
        re.DOTALL,
    )
    return m.group(0) if m else None


def _hidden(form: str, name: str) -> str:
    for tag in re.findall(r"<input[^>]*>", form):
        if re.search(rf'name="{name}"', tag):
            m = re.search(r'value="([^"]*)"', tag)
            if m:
                return m.group(1)
    raise AssertionError(f"no hidden {name} input")


def test_job_page_has_the_post_start_form_with_a_token_for_a_writer(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()

    body, _spy = _page(monkeypatch, client, job_id)

    form = _form(body, f"/jobs/{job_id}/resumes/exports")
    assert form is not None, "no form posting to the export start route"
    assert re.search(r'method="post"', form, re.I)
    assert "Export résumés (as-is + anonymized)" in form
    assert _hidden(form, "csrf_token")
    assert "resumes/export.zip" not in body


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_job_page_hides_the_export_from_a_non_writer(
    monkeypatch: Any, plain_client: Any, role: str
) -> None:
    settings = Settings(cas_enabled=True)
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        api_client,
        "get_cas_user",
        MagicMock(
            return_value={
                "authenticated": True,
                "username": "jordan",
                "cas_enabled": True,
                "role": role,
            }
        ),
    )
    job_id = uuid4()

    body, spy = _page(monkeypatch, plain_client, job_id, _export(uuid4()))

    assert "resumes/exports" not in body
    assert "resume-exports" not in body
    assert "Export résumés" not in body
    assert "data-export-status" not in body
    spy.assert_not_called()


@pytest.mark.parametrize("status", ["queued", "building", "failed", "expired"])
def test_job_page_shows_a_non_ready_status_and_no_download_form(
    monkeypatch: Any, client: Any, status: str
) -> None:
    job_id, export_id = uuid4(), uuid4()

    body, _spy = _page(monkeypatch, client, job_id, _export(export_id, status))

    assert f'data-export-status="{status}"' in body
    assert f"/resume-exports/{export_id}/download" not in body


def test_job_page_with_no_export_shows_only_the_start_button(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()

    body, spy = _page(monkeypatch, client, job_id)

    spy.assert_called_once()
    assert spy.call_args.args[0] == job_id
    assert "/resume-exports/" not in body
    assert "data-export-status" not in body
    assert _form(body, f"/jobs/{job_id}/resumes/exports") is not None


def test_job_page_survives_the_latest_lookup_failing(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()

    body, _spy = _page(
        monkeypatch, client, job_id, api_client.BackendUnavailable("down")
    )

    assert _form(body, f"/jobs/{job_id}/resumes/exports") is not None


def test_a_ready_export_renders_a_post_download_form_with_a_live_token(
    monkeypatch: Any, client: Any
) -> None:
    job_id, export_id = uuid4(), uuid4()

    body, _spy = _page(monkeypatch, client, job_id, _export(export_id))

    form = _form(body, f"/resume-exports/{export_id}/download")
    assert form is not None, "no download form for a ready export"
    assert 'data-export-status="ready"' in body
    assert re.search(r'method="post"', form, re.I)
    assert _hidden(form, "csrf_token")
    assert _hidden(form, "job_id") == str(job_id)
    assert f'href="/resume-exports/{export_id}' not in body  # never a link


# ------------------------------------------------------------- start (POST)


def test_start_posts_to_the_backend_and_redirects_to_the_job(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value={"id": str(uuid4()), "status": "queued"})
    monkeypatch.setattr(api_client, "start_resume_export", spy)

    resp = client.post(f"/jobs/{job_id}/resumes/exports")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    assert spy.call_args.args[0] == job_id


def test_start_without_a_csrf_token_is_403_and_never_reaches_the_backend(
    monkeypatch: Any, plain_client: Any
) -> None:
    spy = MagicMock()
    monkeypatch.setattr(api_client, "start_resume_export", spy)

    resp = plain_client.post(f"/jobs/{uuid4()}/resumes/exports")

    assert resp.status_code == 403
    spy.assert_not_called()


@pytest.mark.parametrize("status", [403, 422])
def test_start_rejected_by_the_backend_flashes_and_returns_to_the_job(
    monkeypatch: Any, client: Any, status: int
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "start_resume_export",
        MagicMock(
            side_effect=api_client.BadRequest(
                f"backend {status}", status_code=status, detail={"detail": "no"}
            )
        ),
    )

    resp = client.post(f"/jobs/{job_id}/resumes/exports")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    with client.session_transaction() as sess:
        assert sess.get("_flashes")


def test_start_backend_unavailable_is_503_and_unknown_job_is_404(
    monkeypatch: Any, client: Any
) -> None:
    monkeypatch.setattr(
        api_client,
        "start_resume_export",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    assert client.post(f"/jobs/{uuid4()}/resumes/exports").status_code == 503
    monkeypatch.setattr(
        api_client,
        "start_resume_export",
        MagicMock(side_effect=api_client.NotFound("no job")),
    )
    assert client.post(f"/jobs/{uuid4()}/resumes/exports").status_code == 404


def test_the_old_get_download_route_is_gone(client: Any) -> None:
    assert client.get(f"/jobs/{uuid4()}/resumes/export.zip").status_code in (404, 405)


# ----------------------------------------------------------- download (POST)


def _token(monkeypatch: Any, client: Any, job_id: UUID, export_id: UUID) -> str:
    body, _spy = _page(monkeypatch, client, job_id, _export(export_id))
    form = _form(body, f"/resume-exports/{export_id}/download")
    assert form is not None
    return _hidden(form, "csrf_token")


def _install_download(monkeypatch: Any, result: Any) -> MagicMock:
    spy = MagicMock(side_effect=result if isinstance(result, Exception) else None)
    if not isinstance(result, Exception):
        spy.return_value = result
    monkeypatch.setattr(api_client, "download_resume_export", spy)
    return spy


def test_download_streams_the_zip_with_no_store(monkeypatch: Any, client: Any) -> None:
    job_id, export_id = uuid4(), uuid4()
    token = _token(monkeypatch, client, job_id, export_id)
    cd = f'attachment; filename="job-{job_id}-resumes.zip"'
    spy = _install_download(monkeypatch, (_ZIP, cd))

    resp = client.post(
        f"/resume-exports/{export_id}/download",
        data={csrf.FORM_FIELD: token, "job_id": str(job_id)},
    )

    assert resp.status_code == 200
    assert resp.data == _ZIP
    assert resp.headers["Content-Type"].startswith("application/zip")
    assert resp.headers["Content-Disposition"] == cd
    assert resp.headers["Cache-Control"] == "no-store"
    assert spy.call_args.args[0] == export_id


def test_download_token_is_one_shot(monkeypatch: Any, client: Any) -> None:
    job_id, export_id = uuid4(), uuid4()
    token = _token(monkeypatch, client, job_id, export_id)
    spy = _install_download(monkeypatch, (_ZIP, "attachment"))
    data = {csrf.FORM_FIELD: token, "job_id": str(job_id)}
    url = f"/resume-exports/{export_id}/download"

    assert client.post(url, data=data).status_code == 200
    assert client.post(url, data=data).status_code == 403
    assert spy.call_count == 1


def test_download_without_a_token_or_with_a_page_token_is_403(
    monkeypatch: Any, client: Any
) -> None:
    job_id, export_id = uuid4(), uuid4()
    spy = _install_download(monkeypatch, (_ZIP, "attachment"))
    url = f"/resume-exports/{export_id}/download"

    assert client.post(url, data={"job_id": str(job_id)}).status_code == 403
    # The session page token (what the hook accepts elsewhere) must not open it.
    from tests.unit.conftest import TEST_PAGE_TOKEN

    bad = {csrf.FORM_FIELD: TEST_PAGE_TOKEN, "job_id": str(job_id)}
    assert client.post(url, data=bad).status_code == 403
    spy.assert_not_called()


def test_download_token_is_bound_to_its_export(monkeypatch: Any, client: Any) -> None:
    job_id, export_id = uuid4(), uuid4()
    token = _token(monkeypatch, client, job_id, export_id)
    spy = _install_download(monkeypatch, (_ZIP, "attachment"))

    resp = client.post(
        f"/resume-exports/{uuid4()}/download",
        data={csrf.FORM_FIELD: token, "job_id": str(job_id)},
    )

    assert resp.status_code == 403
    spy.assert_not_called()


def test_download_from_another_origin_is_403(monkeypatch: Any, client: Any) -> None:
    job_id, export_id = uuid4(), uuid4()
    token = _token(monkeypatch, client, job_id, export_id)
    spy = _install_download(monkeypatch, (_ZIP, "attachment"))

    resp = client.post(
        f"/resume-exports/{export_id}/download",
        data={csrf.FORM_FIELD: token, "job_id": str(job_id)},
        headers={"Origin": "https://evil.example"},
    )

    assert resp.status_code == 403
    spy.assert_not_called()


def test_a_409_says_not_ready_or_file_missing_never_a_permission_message(
    monkeypatch: Any, client: Any
) -> None:
    job_id, export_id = uuid4(), uuid4()
    token = _token(monkeypatch, client, job_id, export_id)
    _install_download(
        monkeypatch,
        api_client.Conflict("backend 409", status_code=409, detail={"detail": "x"}),
    )

    resp = client.post(
        f"/resume-exports/{export_id}/download",
        data={csrf.FORM_FIELD: token, "job_id": str(job_id)},
    )

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    with client.session_transaction() as sess:
        messages = " ".join(str(m) for _cat, m in sess.get("_flashes", [])).lower()
    assert "not ready" in messages or "missing" in messages
    assert "permission" not in messages
    assert "cannot download" not in messages


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (api_client.NotFound("gone"), 404),
        (api_client.BackendUnavailable("down"), 503),
        (api_client.BadRequest("no", status_code=403, detail={}), 403),
    ],
)
def test_download_maps_the_other_backend_errors(
    monkeypatch: Any, client: Any, exc: Exception, status: int
) -> None:
    job_id, export_id = uuid4(), uuid4()
    token = _token(monkeypatch, client, job_id, export_id)
    _install_download(monkeypatch, exc)

    resp = client.post(
        f"/resume-exports/{export_id}/download",
        data={csrf.FORM_FIELD: token, "job_id": str(job_id)},
    )

    assert resp.status_code == status


# ------------------------------------------------- endpoint names / hook


def test_endpoints_exist_under_the_pinned_names() -> None:
    assert "export_start" in app.view_functions
    assert "export_download" in app.view_functions
    assert "export_resumes" not in app.view_functions


def test_start_is_hook_protected_and_download_is_exempt_for_its_own_token() -> None:
    exempt = frontend_app_module._CSRF_HOOK_EXEMPT_ENDPOINTS

    assert "export_download" in exempt
    assert "export_start" not in exempt
