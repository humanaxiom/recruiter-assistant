"""Slice S4 — résumé upload + polling table.

Covers ``api_client.upload_resumes`` (multipart shape via ``httpx.MockTransport``:
the ``files=`` parts, the ``consent_acknowledged`` form field as ``true``/
``false``, the optional ``cover_letter_text``, and a backend 4xx mapped to the
typed ``BadRequest``) and the Flask routes: the mandatory-consent upload POST
(unchecked consent → the backend is NEVER called and the page re-renders with an
error) and the HTMX ``resumes-table`` poll fragment (keeps its ``hx-trigger``
while any résumé row is still ``uploaded``/``parsing`` and DROPS it once every
row is terminal ``parsed``/``failed``). Includes a blind-boundary byte-scan:
a planted fake name/email/phone must be absent from the rendered table.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest

from frontend import api_client
from frontend.app import app

_REAL_NAME = "Zzyzxqrst Wibblesworth"
_REAL_EMAIL = "zzyzxqrst.wibblesworth@example.test"
_REAL_PHONE = "604-555-0192"


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def _json_handler(
    status_code: int, body: Any
) -> Callable[[httpx.Request], httpx.Response]:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    return _handler


def _job(job_id: Any, *, status: str = "open") -> dict[str, Any]:
    return {
        "id": str(job_id),
        "title": "Senior Backend Engineer",
        "department": "Engineering",
        "location": "Remote",
        "min_years": 5,
        "status": status,
        "blind_review": True,
        "parsed_at": "2026-07-17T00:00:00Z",
        "description_parsed": {"required_skills": []},
    }


def _resume(
    *,
    status: str = "parsing",
    candidate_name: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": str(uuid4()),
        "original_filename": "resume.pdf",
        "status": status,
        "uploaded_at": "2026-07-17T00:00:00Z",
        "parsed_at": None,
        "candidate_name": candidate_name,
        "has_cover_letter": False,
    }
    row.update(extra)
    return row


# ── api_client.upload_resumes — multipart shape ──────────────────────────


def test_upload_resumes_posts_multipart_to_the_resumes_path() -> None:
    job_id = uuid4()
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = request.url
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(
            202,
            json=[
                {
                    "original_filename": "a.pdf",
                    "outcome": "accepted",
                    "resume_id": str(uuid4()),
                    "reason": None,
                    "cover_letter_filename": None,
                    "warnings": [],
                }
            ],
        )

    result = api_client.upload_resumes(
        job_id,
        [("a.pdf", b"%PDF-1.4 body-bytes", "application/pdf")],
        consent_acknowledged=True,
        client=_client_with(handler),
    )
    assert captured["method"] == "POST"
    assert captured["url"].path == f"/jobs/{job_id}/resumes"
    assert "multipart/form-data" in captured["content_type"]
    body = captured["body"]
    assert b"a.pdf" in body
    assert b"%PDF-1.4 body-bytes" in body
    assert b"consent_acknowledged" in body
    assert b"true" in body
    assert result[0]["outcome"] == "accepted"


def test_upload_resumes_sends_consent_false_when_not_acknowledged() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"x", "application/pdf")],
        consent_acknowledged=False,
        client=_client_with(handler),
    )
    assert b"consent_acknowledged" in captured["body"]
    assert b"false" in captured["body"]


def test_upload_resumes_forwards_multiple_files() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [
            ("a.pdf", b"aaaa", "application/pdf"),
            ("b.docx", b"bbbb", "application/octet-stream"),
        ],
        consent_acknowledged=True,
        client=_client_with(handler),
    )
    body = captured["body"]
    assert b"a.pdf" in body
    assert b"b.docx" in body
    assert b"aaaa" in body
    assert b"bbbb" in body


def test_upload_resumes_includes_cover_letter_text_when_given() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"x", "application/pdf")],
        consent_acknowledged=True,
        cover_letter_text="Dear hiring manager, please consider me.",
        client=_client_with(handler),
    )
    body = captured["body"]
    assert b"cover_letter_text" in body
    assert b"Dear hiring manager" in body


def test_upload_resumes_omits_cover_letter_when_none() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(202, json=[])

    api_client.upload_resumes(
        uuid4(),
        [("a.pdf", b"x", "application/pdf")],
        consent_acknowledged=True,
        client=_client_with(handler),
    )
    assert b"cover_letter_text" not in captured["body"]


def test_upload_resumes_maps_backend_4xx_to_bad_request() -> None:
    client = _client_with(_json_handler(422, {"detail": "unsupported file"}))
    with pytest.raises(api_client.BadRequest):
        api_client.upload_resumes(
            uuid4(),
            [("a.pdf", b"x", "application/pdf")],
            consent_acknowledged=True,
            client=client,
        )


def test_upload_resumes_maps_5xx_to_backend_unavailable() -> None:
    client = _client_with(_json_handler(500, {"detail": "boom"}))
    with pytest.raises(api_client.BackendUnavailable):
        api_client.upload_resumes(
            uuid4(),
            [("a.pdf", b"x", "application/pdf")],
            consent_acknowledged=True,
            client=client,
        )


# ── POST /jobs/<id>/resumes — mandatory consent ──────────────────────────


def test_upload_route_without_consent_never_calls_backend_and_shows_error(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock()
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={"files": (BytesIO(b"pdf-bytes"), "a.pdf")},
        content_type="multipart/form-data",
    )
    spy.assert_not_called()
    assert resp.status_code != 500
    assert b"consent" in resp.data.lower()


def test_upload_route_with_consent_calls_backend_and_redirects(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(
        return_value=[{"original_filename": "a.pdf", "outcome": "accepted"}]
    )
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf-bytes"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/jobs/{job_id}")
    spy.assert_called_once()
    assert spy.call_args.kwargs["consent_acknowledged"] is True


def test_upload_route_forwards_cover_letter_text(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "cover_letter_text": "A short letter.",
            "files": (BytesIO(b"pdf"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert spy.call_args.kwargs["cover_letter_text"] == "A short letter."


def test_upload_route_caps_cover_letter_length_before_network(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    spy = MagicMock(return_value=[])
    monkeypatch.setattr(api_client, "upload_resumes", spy)
    huge = "x" * 100_000
    client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "cover_letter_text": huge,
            "files": (BytesIO(b"pdf"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    sent = spy.call_args.kwargs["cover_letter_text"]
    assert sent is not None
    assert len(sent) < len(huge)


def test_upload_route_backend_unavailable_is_not_a_500(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "upload_resumes",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    resp = client.post(
        f"/jobs/{job_id}/resumes",
        data={
            "consent_acknowledged": "true",
            "files": (BytesIO(b"pdf"), "a.pdf"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code in (502, 503)
    assert resp.status_code != 500


# ── GET /jobs/<id>/resumes-table — poll fragment ─────────────────────────


def test_resumes_table_keeps_polling_while_a_row_is_unparsed(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(
            return_value=[
                _resume(status="parsed"),
                _resume(status="parsing"),
            ]
        ),
    )
    resp = client.get(f"/jobs/{job_id}/resumes-table")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" in body  # keeps polling
    assert "parsing" in body.lower()


def test_resumes_table_stops_polling_once_every_row_is_terminal(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(
            return_value=[
                _resume(status="parsed"),
                _resume(status="failed"),
            ]
        ),
    )
    resp = client.get(f"/jobs/{job_id}/resumes-table")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "hx-trigger" not in body  # polling stopped


def test_resumes_table_404s_when_job_missing(monkeypatch: Any, client: Any) -> None:
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(side_effect=api_client.NotFound("no job")),
    )
    resp = client.get(f"/jobs/{uuid4()}/resumes-table")
    assert resp.status_code == 404


def test_resumes_table_blind_render_never_contains_raw_pii_bytes(
    monkeypatch: Any, client: Any
) -> None:
    """Blind-boundary byte-scan: a planted real name/email/phone (on fields the
    table must never surface) must be absent from the rendered fragment."""
    job_id = uuid4()
    monkeypatch.setattr(
        api_client,
        "list_resumes",
        MagicMock(
            return_value=[
                _resume(
                    status="parsed",
                    candidate_name=None,
                    email=_REAL_EMAIL,
                    phone=_REAL_PHONE,
                    candidate={
                        "name": _REAL_NAME,
                        "email": _REAL_EMAIL,
                        "phone": _REAL_PHONE,
                    },
                )
            ]
        ),
    )
    resp = client.get(f"/jobs/{job_id}/resumes-table")
    raw = resp.get_data(as_text=True)
    assert _REAL_NAME not in raw
    assert _REAL_EMAIL not in raw
    assert _REAL_PHONE not in raw


def test_job_detail_open_job_shows_upload_form_with_consent_checkbox(
    monkeypatch: Any, client: Any
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(api_client, "get_job", MagicMock(return_value=_job(job_id)))
    monkeypatch.setattr(api_client, "list_resumes", MagicMock(return_value=[]))
    body = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert 'name="consent_acknowledged"' in body
    assert 'type="file"' in body
    assert 'name="files"' in body
