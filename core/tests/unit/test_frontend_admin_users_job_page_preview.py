"""S1a (job-page redesign) -- the admin users page control for the new job page
preview, and the Flask POST behind it.

RED: ``admin_users.html`` has no preview control, ``/admin/users/<uuid>/job-page-
preview`` is not registered, and ``api_client.set_user_job_page_preview`` does
not exist.

Pinned for the coder:

* Flask route ``admin_set_user_job_page_preview``: ``POST
  /admin/users/<uuid:user_id>/job-page-preview``, form field ``enabled`` equal
  to ``"true"`` or ``"false"``. It calls
  ``api_client.set_user_job_page_preview(user_id, enabled: bool)`` and redirects
  (302) to ``admin_users``. Same admin gate as ``admin_set_user_role``.
* ``api_client.set_user_job_page_preview(user_id, enabled, *, client=None)`` is
  ``PATCH /users/{user_id}/job-page-preview`` with JSON ``{"enabled": enabled}``.
* ``admin_users.html`` shows, per row, a form to that route carrying the page
  CSRF token (``csrf_token`` hidden input) and a control named ``enabled`` whose
  value is the TARGET state: ``"true"`` for a user who is currently off,
  ``"false"`` for one who is on (``user.job_page_v2``, missing means off). The
  row mentions "New job page preview".
* A backend 4xx (for example a 403) renders the backend's status, never a 500.

Pure Flask wiring over a stubbed ``api_client``; no real I/O.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import httpx
import pytest

from frontend import api_client
from frontend import app as frontend_app_module
from src.settings import Settings
from tests.unit.conftest import TEST_PAGE_TOKEN

_NOW = "2026-07-01T00:00:00Z"
_LABEL = "new job page preview"


@pytest.fixture
def client(csrf_client: Any) -> Any:
    return csrf_client


def _user(
    *, user_id: UUID | None = None, name: str = "carol", on: bool | None = False
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": str(user_id or uuid4()),
        "cas_username": name,
        "display_name": name.title(),
        "email": f"{name}@example.org",
        "role": "recruiter",
        "active": True,
        "created_at": _NOW,
        "last_seen_at": _NOW,
    }
    if on is not None:
        row["job_page_v2"] = on
    return row


def _cas(enabled: bool) -> Settings:
    return Settings(cas_enabled=enabled)


def _as_role(monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas(True))
    status = {
        "authenticated": True,
        "username": "alice",
        "cas_enabled": True,
        "role": role,
    }
    monkeypatch.setattr(api_client, "get_cas_user", MagicMock(return_value=status))


def _dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(frontend_app_module, "get_settings", lambda: _cas(False))


class _Forms(HTMLParser):
    """Collects each form's action and the (tag, attrs) of the fields inside."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[tuple[str, list[dict[str, str | None]]]] = []
        self._current: list[dict[str, str | None]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        if tag == "form":
            self._current = []
            self.forms.append((d.get("action") or "", self._current))
        elif self._current is not None and tag in {"input", "button", "select"}:
            self._current.append({"tag": tag, **d})

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def _preview_form(html: str, user_id: UUID | str) -> list[dict[str, str | None]]:
    parser = _Forms()
    parser.feed(html)
    suffix = f"/admin/users/{user_id}/job-page-preview"
    matches = [fields for action, fields in parser.forms if action.endswith(suffix)]
    assert len(matches) == 1, f"expected one preview form for {user_id}"
    return matches[0]


def _enabled_values(fields: list[dict[str, str | None]]) -> list[str | None]:
    return [f.get("value") for f in fields if f.get("name") == "enabled"]


# ── the page ───────────────────────────────────────────────────────────────


def test_admin_sees_a_preview_form_per_user_row_with_the_page_csrf_token(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    _as_role(monkeypatch, "admin")
    a, b = uuid4(), uuid4()
    monkeypatch.setattr(
        api_client,
        "list_users",
        MagicMock(return_value=[_user(user_id=a, name="carol"), _user(user_id=b)]),
    )
    resp = client.get("/admin/users", headers={"Cookie": "ra_session=tok-live"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert _LABEL in html.lower()
    for uid in (a, b):
        fields = _preview_form(html, uid)
        tokens = [f for f in fields if f.get("name") == "csrf_token"]
        assert [t.get("value") for t in tokens] == [TEST_PAGE_TOKEN]


def test_the_control_targets_on_for_an_off_user(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    _as_role(monkeypatch, "admin")
    uid = uuid4()
    monkeypatch.setattr(
        api_client, "list_users", MagicMock(return_value=[_user(user_id=uid, on=False)])
    )
    html = client.get(
        "/admin/users", headers={"Cookie": "ra_session=tok-live"}
    ).get_data(as_text=True)
    assert _enabled_values(_preview_form(html, uid)) == ["true"]


def test_the_control_targets_off_for_an_on_user(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    _as_role(monkeypatch, "admin")
    uid = uuid4()
    monkeypatch.setattr(
        api_client, "list_users", MagicMock(return_value=[_user(user_id=uid, on=True)])
    )
    html = client.get(
        "/admin/users", headers={"Cookie": "ra_session=tok-live"}
    ).get_data(as_text=True)
    assert _enabled_values(_preview_form(html, uid)) == ["false"]


def test_a_row_without_the_field_is_treated_as_off(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    _as_role(monkeypatch, "admin")
    uid = uuid4()
    monkeypatch.setattr(
        api_client, "list_users", MagicMock(return_value=[_user(user_id=uid, on=None)])
    )
    html = client.get(
        "/admin/users", headers={"Cookie": "ra_session=tok-live"}
    ).get_data(as_text=True)
    assert _enabled_values(_preview_form(html, uid)) == ["true"]


def test_dev_mode_page_also_offers_the_control(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    _dev(monkeypatch)
    uid = uuid4()
    monkeypatch.setattr(
        api_client, "list_users", MagicMock(return_value=[_user(user_id=uid)])
    )
    html = client.get("/admin/users").get_data(as_text=True)
    assert _preview_form(html, uid)


@pytest.mark.parametrize("role", ["recruiter", "hiring_manager", "auditor"])
def test_a_non_admin_never_sees_the_control(
    monkeypatch: pytest.MonkeyPatch, client: Any, role: str
) -> None:
    _as_role(monkeypatch, role)
    monkeypatch.setattr(
        api_client, "list_users", MagicMock(return_value=[_user(name="carol")])
    )
    resp = client.get("/admin/users", headers={"Cookie": "ra_session=tok-live"})
    assert resp.status_code == 403
    assert "job-page-preview" not in resp.get_data(as_text=True)
    assert _LABEL not in resp.get_data(as_text=True).lower()


# ── the POST ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_post_calls_the_client_and_redirects_to_the_users_page(
    monkeypatch: pytest.MonkeyPatch, client: Any, raw: str, expected: bool
) -> None:
    _dev(monkeypatch)
    uid = uuid4()
    mock = MagicMock(return_value=_user(user_id=uid, on=expected))
    monkeypatch.setattr(api_client, "set_user_job_page_preview", mock)
    resp = client.post(
        f"/admin/users/{uid}/job-page-preview",
        data={"enabled": raw},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].rstrip("/").endswith("/admin/users")
    mock.assert_called_once_with(uid, expected)


@pytest.mark.parametrize("raw", [None, "", "maybe", "1"])
def test_post_with_a_missing_or_unrecognised_value_is_400_and_calls_nothing(
    monkeypatch: pytest.MonkeyPatch, client: Any, raw: str | None
) -> None:
    _dev(monkeypatch)
    mock = MagicMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(api_client, "set_user_job_page_preview", mock)
    data = {} if raw is None else {"enabled": raw}
    resp = client.post(f"/admin/users/{uuid4()}/job-page-preview", data=data)
    assert resp.status_code == 400
    mock.assert_not_called()


@pytest.mark.parametrize("role", ["recruiter", "hiring_manager", "auditor"])
def test_a_non_admin_session_cannot_post(
    monkeypatch: pytest.MonkeyPatch, client: Any, role: str
) -> None:
    _as_role(monkeypatch, role)
    mock = MagicMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(api_client, "set_user_job_page_preview", mock)
    resp = client.post(
        f"/admin/users/{uuid4()}/job-page-preview",
        data={"enabled": "true"},
        headers={"Cookie": "ra_session=tok-live"},
    )
    assert resp.status_code == 403
    mock.assert_not_called()


def test_a_post_without_the_page_token_is_rejected_and_calls_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dev(monkeypatch)
    mock = MagicMock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(api_client, "set_user_job_page_preview", mock)
    frontend_app_module.app.config.update(TESTING=True)
    bare = frontend_app_module.app.test_client()
    resp = bare.post(
        f"/admin/users/{uuid4()}/job-page-preview", data={"enabled": "true"}
    )
    assert resp.status_code == 403
    mock.assert_not_called()


def test_a_backend_403_renders_the_backend_status_not_a_500(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    _dev(monkeypatch)
    monkeypatch.setattr(
        api_client,
        "set_user_job_page_preview",
        MagicMock(
            side_effect=api_client.BadRequest(
                "backend 403", status_code=403, detail={"detail": "admin required"}
            )
        ),
    )
    monkeypatch.setattr(api_client, "list_users", MagicMock(return_value=[_user()]))
    resp = client.post(
        f"/admin/users/{uuid4()}/job-page-preview", data={"enabled": "true"}
    )
    assert resp.status_code == 403
    assert "traceback" not in resp.get_data(as_text=True).lower()


def test_a_backend_not_found_is_404(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    _dev(monkeypatch)
    monkeypatch.setattr(
        api_client,
        "set_user_job_page_preview",
        MagicMock(side_effect=api_client.NotFound("no such user")),
    )
    resp = client.post(
        f"/admin/users/{uuid4()}/job-page-preview", data={"enabled": "true"}
    )
    assert resp.status_code == 404


def test_backend_unavailable_is_503(
    monkeypatch: pytest.MonkeyPatch, client: Any
) -> None:
    _dev(monkeypatch)
    monkeypatch.setattr(
        api_client,
        "set_user_job_page_preview",
        MagicMock(side_effect=api_client.BackendUnavailable("down")),
    )
    resp = client.post(
        f"/admin/users/{uuid4()}/job-page-preview", data={"enabled": "true"}
    )
    assert resp.status_code == 503


# ── api_client wiring ──────────────────────────────────────────────────────


@pytest.mark.parametrize("enabled", [True, False])
def test_the_client_patches_the_expected_path_with_the_json_body(
    enabled: bool,
) -> None:
    import json

    seen: dict[str, Any] = {}
    uid = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_user(user_id=uid, on=enabled))

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t")
    result = api_client.set_user_job_page_preview(uid, enabled, client=http)
    assert seen == {
        "method": "PATCH",
        "path": f"/users/{uid}/job-page-preview",
        "body": {"enabled": enabled},
    }
    assert result["job_page_v2"] is enabled


@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (404, api_client.NotFound),
        (403, api_client.BadRequest),
        (422, api_client.BadRequest),
        (500, api_client.BackendUnavailable),
    ],
)
def test_the_client_maps_backend_errors(status: int, exc: type[Exception]) -> None:
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={})),
        base_url="http://t",
    )
    with pytest.raises(exc):
        api_client.set_user_job_page_preview(uuid4(), True, client=http)
