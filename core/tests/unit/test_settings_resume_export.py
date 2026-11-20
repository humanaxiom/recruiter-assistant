"""RED -- slice H3: the three export settings.

``resume_export_ttl_minutes=60`` (a built ZIP is deleted this long after it is
ready), ``resume_export_max_applicants=500`` (a job with more live applicants
is refused with a 422) and ``resume_export_max_pages_per_file=20`` (a longer
file ships as-is only). Plain ``int`` fields read from ``RESUME_EXPORT_*``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest import MonkeyPatch

from src.settings import Settings

_DEFAULTS = {
    "resume_export_ttl_minutes": 60,
    "resume_export_max_applicants": 500,
    "resume_export_max_pages_per_file": 20,
}


@pytest.mark.parametrize(("field", "default"), sorted(_DEFAULTS.items()))
def test_export_setting_has_the_approved_default(field: str, default: int) -> None:
    value = getattr(Settings(), field)
    assert value == default
    assert type(value) is int


@pytest.mark.parametrize("field", sorted(_DEFAULTS))
def test_export_setting_is_overridable_from_the_environment(
    monkeypatch: MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv(field.upper(), "7")
    assert getattr(Settings(), field) == 7


@pytest.mark.parametrize("field", sorted(_DEFAULTS))
def test_export_setting_is_overridable_by_constructor(field: str) -> None:
    kwargs: dict[str, Any] = {field: 3}
    assert getattr(Settings(**kwargs), field) == 3
