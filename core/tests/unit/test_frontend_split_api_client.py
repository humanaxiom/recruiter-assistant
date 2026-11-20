"""RED -- slice E1: ``frontend.api_client`` methods for the in-app Taleo split.

Contract pinned (every function takes the optional keyword-only ``client``):

* ``split_upload(job_id, files, *, consent_acknowledged)`` -> POST
  ``/jobs/{id}/resumes/split-upload`` multipart (``files`` parts + a
  ``consent_acknowledged`` form field of ``"true"``/``"false"``).
* ``get_resume_split(split_id)`` -> GET ``/resume-splits/{id}``.
* ``correct_resume_split(split_id, *, proposal_version, applicants,
  ignored_pages)`` -> POST ``/resume-splits/{id}/correct`` JSON body.
* ``accept_resume_split(split_id, *, proposal_version)`` -> POST
  ``/resume-splits/{id}/accept`` JSON ``{"proposal_version": n}``.
* ``discard_resume_split(split_id)`` -> POST ``/resume-splits/{id}/discard``.

A 404 is ``NotFound``, a 409 is ``Conflict``, other 4xx ``BadRequest``, 5xx
``BackendUnavailable``.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from frontend import api_client


def _client(captured: list[httpx.Request], status: int, body: Any) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def test_split_upload_posts_multipart_with_consent_and_every_file() -> None:
    job_id = uuid4()
    seen: list[httpx.Request] = []
    body = {"splits": [{"id": str(uuid4()), "status": "queued"}], "split_ids": []}

    out = api_client.split_upload(
        job_id,
        [("a.pdf", b"%PDF-1", "application/pdf"), ("b.pdf", b"%PDF-2", "x/y")],
        consent_acknowledged=True,
        client=_client(seen, 202, body),
    )

    assert out == body
    (req,) = seen
    assert req.method == "POST"
    assert req.url.path == f"/jobs/{job_id}/resumes/split-upload"
    raw = req.content
    assert b'name="consent_acknowledged"' in raw
    assert b"true" in raw
    assert raw.count(b'name="files"') == 2
    assert b'filename="a.pdf"' in raw and b'filename="b.pdf"' in raw


def test_split_upload_sends_false_when_consent_is_false() -> None:
    seen: list[httpx.Request] = []
    api_client.split_upload(
        uuid4(),
        [("a.pdf", b"%PDF-1", "application/pdf")],
        consent_acknowledged=False,
        client=_client(seen, 202, {"splits": [], "split_ids": []}),
    )
    assert b"false" in seen[0].content


def test_get_resume_split_gets_the_view() -> None:
    split_id = uuid4()
    seen: list[httpx.Request] = []
    view = {"id": str(split_id), "status": "queued"}

    out = api_client.get_resume_split(split_id, client=_client(seen, 200, view))

    assert out == view
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"/resume-splits/{split_id}"


def test_correct_resume_split_posts_the_json_body() -> None:
    split_id = uuid4()
    seen: list[httpx.Request] = []
    applicants = [
        {"candidate_name": "", "resume_pages": [1, 2], "cover_letter_pages": [3]}
    ]

    api_client.correct_resume_split(
        split_id,
        proposal_version=4,
        applicants=applicants,
        ignored_pages=[9],
        client=_client(seen, 200, {"id": str(split_id)}),
    )

    req = seen[0]
    assert req.method == "POST"
    assert req.url.path == f"/resume-splits/{split_id}/correct"
    assert json.loads(req.content) == {
        "proposal_version": 4,
        "applicants": applicants,
        "ignored_pages": [9],
    }


def test_accept_resume_split_posts_the_version() -> None:
    split_id = uuid4()
    seen: list[httpx.Request] = []
    rows = [{"original_filename": "applicant-001_resume.pdf", "outcome": "accepted"}]

    out = api_client.accept_resume_split(
        split_id, proposal_version=7, client=_client(seen, 200, rows)
    )

    assert out == rows
    assert seen[0].url.path == f"/resume-splits/{split_id}/accept"
    assert json.loads(seen[0].content) == {"proposal_version": 7}


def test_discard_resume_split_posts_to_discard() -> None:
    split_id = uuid4()
    seen: list[httpx.Request] = []

    api_client.discard_resume_split(
        split_id, client=_client(seen, 200, {"id": str(split_id)})
    )

    assert seen[0].method == "POST"
    assert seen[0].url.path == f"/resume-splits/{split_id}/discard"


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (404, api_client.NotFound),
        (409, api_client.Conflict),
        (422, api_client.BadRequest),
        (503, api_client.BackendUnavailable),
    ],
)
def test_split_calls_map_http_errors_to_typed_exceptions(
    status: int, exc: type[Exception]
) -> None:
    split_id = uuid4()
    with pytest.raises(exc):
        api_client.accept_resume_split(
            split_id,
            proposal_version=1,
            client=_client([], status, {"code": "x", "message": "m"}),
        )


def test_a_409_carries_the_backend_body_as_detail() -> None:
    body = {"code": "resource.conflict", "message": "stale", "current_version": 5}
    with pytest.raises(api_client.Conflict) as info:
        api_client.correct_resume_split(
            uuid4(),
            proposal_version=1,
            applicants=[],
            ignored_pages=[],
            client=_client([], 409, body),
        )
    assert info.value.detail == body
