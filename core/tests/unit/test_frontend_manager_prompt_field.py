"""The manager's additional-requirements box, browser → Flask → API.

SPONSOR 2026-09-02 §I4. Same reason `test_frontend_work_authorization.py`
exists: ROADMAP names **an absence — a form input that was never rendered,
which no mutation of existing code can make appear** — and the withdrawal
reason was exactly that for months, with the route reading a field no form
collected and every live row recording ``None``.

Here the stakes are the same shape. The column exists, the extraction pass
exists, and the 0.10 weight the sponsor moved off the cover letter is riding on
it. If no box renders, every job carries no note forever, the sub-score is
permanently unmeasured, and the reassigned 10% quietly does nothing — which
would look exactly like the feature working.

Two properties beyond "it exists":

* **It is a SEPARATE field from Description.** ``description_raw`` is the JD of
  record; folding the note into it makes the two indistinguishable forever and
  destroys the provenance the shortlist needs to explain a rank.
* **An untouched box sends nothing.** Empty must reach the backend as ``None``,
  not ``""`` — "nobody asked" and "asked for nothing" are different facts, and
  the ranking combine reads the first one to decide the sub-score is unmeasured
  rather than a measured zero.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from frontend import api_client
from frontend.app import app


@pytest.fixture
def client() -> Any:
    app.config.update(TESTING=True)
    return app.test_client()


def _index(monkeypatch: Any, client: Any) -> str:
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    return client.get("/").get_data(as_text=True)


def _submit(monkeypatch: Any, client: Any, **extra: str) -> MagicMock:
    spy = MagicMock(return_value={"id": str(uuid4())})
    monkeypatch.setattr(api_client, "create_job", spy)
    monkeypatch.setattr(api_client, "list_jobs", MagicMock(return_value=[]))
    token = re.search(r'name="csrf_token" value="([^"]+)"', _index(monkeypatch, client))
    assert token is not None, "no page CSRF token on the create-job form"
    client.post(
        "/jobs",
        data={
            "csrf_token": token.group(1),
            "title": "Research Analyst",
            "description_raw": "A detailed job description of the role. " * 3,
            **extra,
        },
    )
    return spy


# ------------------------------------------------------------- it renders


def test_the_additional_requirements_box_is_on_the_create_form(
    monkeypatch: Any, client: Any
) -> None:
    """THE test this file exists for. Without the box, the sponsor's 10%
    reassignment is inert on every job ever created — and looks like it works."""
    body = _index(monkeypatch, client)
    assert 'name="additional_requirements"' in body, (
        "no additional-requirements input renders — the hiring manager cannot "
        "state a requirement the posting missed, which is requirement §I4"
    )


def test_it_is_a_separate_field_from_the_description(
    monkeypatch: Any, client: Any
) -> None:
    """The JD of record must stay byte-faithful to the posting. Two distinct
    named inputs, not one box the manager is expected to append to."""
    body = _index(monkeypatch, client)
    assert 'name="description_raw"' in body
    assert 'name="additional_requirements"' in body
    assert body.index('name="description_raw"') != body.index(
        'name="additional_requirements"'
    )


def test_the_box_explains_that_plain_statements_are_required(
    monkeypatch: Any, client: Any
) -> None:
    """The must-have-by-default rule is invisible from the control itself, and
    a manager who assumes these are preferences will phrase them as such."""
    body = " ".join(_index(monkeypatch, client).split())
    assert "required" in body.lower()
    for softener in ("nice to have", "bonus", "ideally"):
        assert softener in body.lower(), (
            f"the form does not tell the manager that {softener!r} softens a "
            "requirement, so the only way to write a preference is undiscoverable"
        )


def test_the_box_is_capped_at_the_schema_limit(monkeypatch: Any, client: Any) -> None:
    """Mirrors ``JobCreate.additional_requirements``'s 4000-char cap. A note is
    typed once, at the moment of a decision; losing it to a 422 on submit is
    not recoverable by retrying."""
    body = _index(monkeypatch, client)
    assert 'maxlength="4000"' in body


# --------------------------------------------------------- it reaches the API


def test_a_typed_note_reaches_the_backend(monkeypatch: Any, client: Any) -> None:
    spy = _submit(
        monkeypatch, client, additional_requirements="Must have MEG analysis."
    )
    spy.assert_called_once()
    assert spy.call_args.args[0]["additional_requirements"] == "Must have MEG analysis."


def test_an_untouched_box_sends_none_not_empty_string(
    monkeypatch: Any, client: Any
) -> None:
    """ "Nobody asked" is not "asked for nothing". The combine reads ``None`` to
    mark the sub-score unmeasured; an empty string would assert the manager
    listed no requirements, which is a different and false claim — and would
    burn an LLM round trip per job extracting it."""
    spy = _submit(monkeypatch, client)
    assert spy.call_args.args[0]["additional_requirements"] is None


def test_a_whitespace_only_note_is_also_none(monkeypatch: Any, client: Any) -> None:
    spy = _submit(monkeypatch, client, additional_requirements="   \n\t  ")
    assert spy.call_args.args[0]["additional_requirements"] is None
