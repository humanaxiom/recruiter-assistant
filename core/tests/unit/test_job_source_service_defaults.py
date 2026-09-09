"""``src.services.job_source_service.ExternalJobUpsert.blind_review`` default
— REVERSED 2026-09-09 (sponsor decision): "reverse the blind review to be off
by default, keep the on switch button." The Taleo sync (ADR-046) upserts jobs
with no human present to tick the create-form checkbox at all, so its own
dataclass default has to move in lockstep with ``JobCreate``'s and the DDL's,
or every synced job would still be born blind-reviewed while every
manually-created one is not — the exact kind of drift this reversal exists to
avoid.

Pure, I/O-free: constructing the dataclass touches no database. The
behavioural proof that this default actually reaches a real row is
``tests/integration/test_job_source_upsert_pg.py``.
"""

from __future__ import annotations

from src.services.job_source_service import ExternalJobUpsert


def _minimal_kwargs() -> dict[str, object]:
    """Every field ``ExternalJobUpsert`` requires with no default of its own."""
    return {
        "external_id": "7124",
        "external_url": "https://tre.tbe.taleo.net/some/job",
        "title": "Research Analyst",
        "description_raw": "A detailed posting description. " * 3,
    }


def test_external_job_upsert_blind_review_defaults_false() -> None:
    upsert = ExternalJobUpsert(**_minimal_kwargs())
    assert upsert.blind_review is False


def test_external_job_upsert_blind_review_can_still_be_set_true() -> None:
    """The reversal changes the DEFAULT only — a caller must still be able to
    construct an opted-in upsert explicitly."""
    upsert = ExternalJobUpsert(**_minimal_kwargs(), blind_review=True)
    assert upsert.blind_review is True


def test_external_job_upsert_is_frozen() -> None:
    """Unrelated to the reversal, but cheap to pin alongside it: a value used
    as an upsert payload must not be mutable out from under the caller."""
    upsert = ExternalJobUpsert(**_minimal_kwargs())
    try:
        upsert.blind_review = True  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("ExternalJobUpsert must be frozen (immutable)")
