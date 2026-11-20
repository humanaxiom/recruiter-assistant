"""RED -- slice H: the Flask side of the resume export download.

* ``api_client.export_resumes_zip(job_id, *, client=None) -> bytes`` -- GET
  ``/jobs/{id}/resumes/export.zip``; 404 -> ``NotFound``, other 4xx (a 403 is
  one) -> ``BadRequest``, 5xx -> ``BackendUnavailable``.
* ``GET /jobs/<id>/resumes/export.zip`` (Flask) -- streams those bytes as an
  attachment named ``job-<id>-resumes.zip`` (the BFF builds the name itself;
  nothing candidate-derived), content type ``application/zip``. Backend 404 ->
  404, 403/4xx -> 302 back to the job page (flash), 503 -> 503 error page.
* ``job_detail.html`` links to it ("Download résumés (as-is + anonymized)")
  for writers only.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from frontend.app import app
from src.settings import Settings

_ZIP = b"PK\x03\x04 fake zip bytes"


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _http(captured: list[httpx.Request], status: int, content: bytes) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, content=content)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t")


# ---------------------------------------------------------------- api_client


def test_export_resumes_zip_gets_the_route_and_returns_raw_bytes() -> None:
    job_id = uuid4()
    seen: list[httpx.Request] = []

    out = api_client.export_resumes_zip(job_id, client=_http(seen, 200, _ZIP))

    assert out == _ZIP
    assert isinstance(out, bytes)
    (req,) = seen
    assert req.method == "GET"
    assert req.url.path == f"/jobs/{job_id}/resumes/export.zip"


def test_export_resumes_zip_404_is_not_found() -> None:
    with pytest.raises(api_client.NotFound):
        api_client.export_resumes_zip(uuid4(), client=_http([], 404, b"{}"))


def test_export_resumes_zip_403_is_bad_request() -> None:
    with pytest.raises(api_client.BadRequest):
        api_client.export_resumes_zip(uuid4(), client=_http([], 403, b"{}"))


@pytest.mark.parametrize("status", [500, 502, 503])
def test_export_resumes_zip_5xx_is_backend_unavailable(status: int) -> None:
    with pytest.raises(api_client.BackendUnavailable):
        api_client.export_resumes_zip(uuid4(), client=_http([], status, b"{}"))


# --------------------------------------------------------------- Flask route


def _install(monkeypatch: Any, result: Any) -> MagicMock:
    spy = MagicMock(side_effect=result if isinstance(result, Exception) else None)
    if not isinstance(result, Exception):
        spy.return_value = result
    monkeypatch.setattr(api_client, "export_resumes_zip", spy)
    return spy


def test_route_streams_the_zip_as_a_safe_named_attachment(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = _install(monkeypatch, _ZIP)

    resp = client.get(f"/jobs/{job_id}/resumes/export.zip")

    assert resp.status_code == 200
    assert resp.data == _ZIP
    assert resp.headers["Content-Type"].startswith("application/zip")
    disposition = resp.headers["Content-Disposition"]
    assert disposition.lower().startswith("attachment")
    assert f"job-{job_id}-resumes.zip" in disposition
    assert spy.call_args.args[0] == job_id


def test_route_backend_404_is_404(monkeypatch: Any, client: Any) -> None:
    _install(monkeypatch, api_client.NotFound("no such job"))

    assert client.get(f"/jobs/{uuid4()}/resumes/export.zip").status_code == 404


def test_route_backend_403_redirects_to_the_job_page_never_500(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    _install(
        monkeypatch,
        api_client.BadRequest(
            "backend 403: session role not permitted for this route",
            status_code=403,
            detail={"detail": "session role not permitted for this route"},
        ),
    )

    resp = client.get(f"/jobs/{job_id}/resumes/export.zip")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")


def test_route_backend_unavailable_is_503(monkeypatch: Any, client: Any) -> None:
    _install(monkeypatch, api_client.BackendUnavailable("down"))

    assert client.get(f"/jobs/{uuid4()}/resumes/export.zip").status_code == 503


# ----------------------------------------------------------- job_detail link


def _job(job_id: Any) -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": "open",
        "blind_review": False,
        "parsed_at": "2026-07-17T00:00:00Z",
        "description_parsed": {"required_skills": []},
    }


def _page(monkeypatch: Any, client: Any, job_id: Any) -> str:
    monkeypatch.setattr(api_client, "get_job", lambda jid, **kw: _job(job_id))
    monkeypatch.setattr(api_client, "list_resumes", lambda jid, **kw: [])
    return str(client.get(f"/jobs/{job_id}").get_data(as_text=True))


def test_job_page_links_the_export_for_a_writer(monkeypatch: Any, client: Any) -> None:
    job_id = uuid4()

    body = _page(monkeypatch, client, job_id)

    assert f"/jobs/{job_id}/resumes/export.zip" in body
    assert "Download résumés (as-is + anonymized)" in body


@pytest.mark.parametrize("role", ["hiring_manager", "auditor"])
def test_job_page_hides_the_export_from_a_non_writer(
    monkeypatch: Any, client: Any, role: str
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

    body = _page(monkeypatch, client, job_id)

    assert "resumes/export.zip" not in body
    assert "Download résumés" not in body
