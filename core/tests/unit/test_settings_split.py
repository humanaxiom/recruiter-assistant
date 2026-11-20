"""Slice C1 (RED) -- the four split settings (plan decisions 4 and 5).

``split_proposal_ttl_h=72`` (unaccepted proposals expire after this many
hours), ``split_max_file_mib=40``, ``split_max_pages=300``,
``split_max_files=5``. All plain ``int`` fields read from the environment as
``SPLIT_*`` like every other setting.
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest import MonkeyPatch

from src.settings import Settings

_DEFAULTS = {
    "split_proposal_ttl_h": 72,
    "split_max_file_mib": 40,
    "split_max_pages": 300,
    "split_max_files": 5,
}


@pytest.mark.parametrize(("field", "default"), sorted(_DEFAULTS.items()))
def test_split_setting_has_the_approved_default(field: str, default: int) -> None:
    value = getattr(Settings(), field)
    assert value == default
    assert type(value) is int


@pytest.mark.parametrize("field", sorted(_DEFAULTS))
def test_split_setting_is_overridable_from_the_environment(
    monkeypatch: MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv(field.upper(), "7")
    assert getattr(Settings(), field) == 7


@pytest.mark.parametrize("field", sorted(_DEFAULTS))
def test_split_setting_is_overridable_by_constructor(field: str) -> None:
    kwargs: dict[str, Any] = {field: 3}
    assert getattr(Settings(**kwargs), field) == 3
