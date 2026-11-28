"""Smoke tests for the job-page redesign (v2), driven over real HTTP.

``scripts/smoke.sh`` requires ``SMOKE_EXPECT_JOB_PAGE=v1|v2`` and the layout
marker test below fails when the stack serves the other layout, so a run can
never believe it drove v2 while it drove v1. Every other test here is v2-only
and SKIPS with a stated reason on a v1 run -- the one deliberate skip in this
suite, because on v1 these routes and markers do not exist by design, and the
marker test has already proved the stack is v1 rather than merely misconfigured.

The job for the banner and redirect checks is created through the same form POST
the conftest uses, with a synthetic description and NO fixture files.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import EXPECT_LAYOUT, _wait, job_page_url

from tests.e2e.driver import CSRF_HEADER, form_for, hidden_value, page_token

pytestmark = pytest.mark.smoke

requires_v2 = pytest.mark.skipif(
    EXPECT_LAYOUT == "v1",
    reason="v2-only check: SMOKE_EXPECT_JOB_PAGE=v1, the legacy job page has no tabs",
)

_TABS = ("overview", "candidates", "requirements", "settings", "shortlist")

_SYNTHETIC_JD = (
    "Research Assistant, Smoke Lab. Responsibilities: maintain laboratory "
    "equipment, prepare samples, and record results in the lab notebook. "
    "Requirements: a diploma in biology or chemistry and two years of bench "
    "experience with careful documentation habits."
)


def _token(client: httpx.Client, path: str) -> str:
    token = page_token(client.get(path).text)
    assert token, f"no page CSRF token on {path}"
    return token


def _post_form(
    client: httpx.Client, path: str, fields: dict[str, str], token: str
) -> httpx.Response:
    return client.post(
        path,
        data={**fields, "csrf_token": token},
        headers={CSRF_HEADER: token},
    )


def _assert_redirects_to_tab(resp: httpx.Response, job_id: str, tab: str) -> None:
    assert resp.status_code == 302, resp.text[:300]
    location = resp.headers["location"]
    want = f"/jobs/{job_id}?tab={tab}"
    assert location.endswith(want), f"expected ...{want}, got {location!r}"


@pytest.fixture(scope="module")
def fresh_job(client: httpx.Client) -> str:
    """A draft job made through ``POST /jobs`` from a synthetic description."""
    assert len(_SYNTHETIC_JD) >= 50
    token = _token(client, "/")
    created = client.post(
        "/jobs",
        data={
            "title": "Smoke V2 Position",
            "department": "Smoke",
            "description_raw": _SYNTHETIC_JD,
            "blind_review": "on",
            "shortlist_top_percent": "100",
            "csrf_token": token,
        },
        headers={CSRF_HEADER: token},
    )
    assert created.status_code == 302, created.text[:300]
    return created.headers["location"].rstrip("/").rsplit("/", 1)[-1].split("?")[0]


# ── the layout marker, on both layouts ───────────────────────────────────


def test_the_layout_marker_matches_what_the_run_expects(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    body = client.get(job_page_url(ranked_job["job_id"], "candidates")).text
    has_v2 = 'data-job-layout="v2"' in body
    assert has_v2 == (EXPECT_LAYOUT == "v2"), (
        f"SMOKE_EXPECT_JOB_PAGE={EXPECT_LAYOUT} but the stack served "
        f"{'v2' if has_v2 else 'v1'} (is JOB_PAGE_V2 set the way this run says?)"
    )


# ── v2 only ──────────────────────────────────────────────────────────────


@requires_v2
@pytest.mark.parametrize("tab", _TABS)
def test_every_tab_renders_for_the_writer(
    client: httpx.Client, ranked_job: dict[str, Any], tab: str
) -> None:
    resp = client.get(job_page_url(ranked_job["job_id"], tab))
    assert resp.status_code == 200, f"{tab}: {resp.status_code}"
    if tab != "overview":
        assert f'data-job-tab="{tab}"' in resp.text, f"{tab}: no tab wrapper"


@requires_v2
def test_the_candidates_tab_offers_four_intake_cards_and_the_export_menu(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    body = client.get(job_page_url(ranked_job["job_id"], "candidates")).text
    for card in ("single", "zip", "taleo-pdf", "roster"):
        assert f'data-intake-card="{card}"' in body, f"missing intake card {card}"
    assert "data-export-menu" in body


@requires_v2
def test_a_fresh_job_shows_an_intake_banner(
    client: httpx.Client, fresh_job: str
) -> None:
    body = client.get(job_page_url(fresh_job, "candidates")).text
    assert any(
        f'data-intake-banner="{state}"' in body for state in ("parsing", "parsed")
    ), "a freshly created draft job's Candidates tab has no parsing/parsed banner"


@requires_v2
def test_saving_details_redirects_to_settings(
    client: httpx.Client, fresh_job: str
) -> None:
    token = _token(client, job_page_url(fresh_job, "settings"))
    resp = _post_form(
        client,
        f"/jobs/{fresh_job}/details",
        {"department": "Smoke Dept", "location": "Burnaby"},
        token,
    )
    _assert_redirects_to_tab(resp, fresh_job, "settings")


@requires_v2
def test_saving_the_requirements_note_redirects_to_requirements(
    client: httpx.Client, fresh_job: str
) -> None:
    token = _token(client, job_page_url(fresh_job, "requirements"))
    resp = _post_form(
        client,
        f"/jobs/{fresh_job}/requirements",
        {"additional_requirements": "Smoke note: bench experience is a bonus."},
        token,
    )
    _assert_redirects_to_tab(resp, fresh_job, "requirements")


@requires_v2
def test_toggling_blind_review_redirects_to_settings_and_back(
    client: httpx.Client, fresh_job: str
) -> None:
    for desired in ("false", "true"):
        token = _token(client, job_page_url(fresh_job, "settings"))
        resp = _post_form(
            client,
            f"/jobs/{fresh_job}/blind-review",
            {"blind_review": desired},
            token,
        )
        _assert_redirects_to_tab(resp, fresh_job, "settings")


@requires_v2
def test_starting_an_export_redirects_to_candidates(
    client: httpx.Client, ranked_job: dict[str, Any]
) -> None:
    job_id = ranked_job["job_id"]
    token = _token(client, job_page_url(job_id, "candidates"))
    resp = _post_form(client, f"/jobs/{job_id}/resumes/exports", {}, token)
    _assert_redirects_to_tab(resp, job_id, "candidates")


@requires_v2
def test_opening_from_the_settings_form_redirects_to_settings(
    client: httpx.Client, fresh_job: str
) -> None:
    """Last on purpose: it opens the draft the earlier tests edit. The JD must
    have finished parsing first, or the backend (rightly) refuses to open it."""

    def parsed() -> bool:
        frag = client.get(f"/jobs/{fresh_job}/parse-status").text
        return "badge-parsing" not in frag

    _wait("the synthetic JD to finish parsing", parsed, 300)
    html = client.get(job_page_url(fresh_job, "settings")).text
    form = form_for(html, 'name="to" value="open"')
    assert form is not None, "no Open form on the Settings tab of a parsed draft"
    assert hidden_value(form, "tab") == "settings"
    token = hidden_value(form, "csrf_token")
    assert token, "no csrf_token in the status form"
    resp = _post_form(
        client, f"/jobs/{fresh_job}/status", {"to": "open", "tab": "settings"}, token
    )
    _assert_redirects_to_tab(resp, fresh_job, "settings")
