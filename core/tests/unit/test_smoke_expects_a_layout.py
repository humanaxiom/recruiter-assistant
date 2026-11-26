"""Smoke must be told which job-page layout it is checking, and refuse otherwise.

The smoke suite asserts on rendered HTML, and v1 and v2 render different pages.
A run that defaulted to one layout could go green while driving the other, which
is the "green run that tested nothing" failure ``smoke.sh`` exists to prevent.
So the script FAILS when ``SMOKE_EXPECT_JOB_PAGE`` is unset (as it already does
for CAS), and the conftest accepts exactly ``v1`` or ``v2``.

Nothing here runs docker: the script is read as text, and the conftest is loaded
under a patched environment.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SMOKE_SH = _REPO / "scripts" / "smoke.sh"
_CONFTEST = Path(__file__).resolve().parents[1] / "smoke" / "conftest.py"
_VAR = "SMOKE_EXPECT_JOB_PAGE"


def _load_conftest(monkeypatch: pytest.MonkeyPatch, value: str | None) -> ModuleType:
    if value is None:
        monkeypatch.delenv(_VAR, raising=False)
    else:
        monkeypatch.setenv(_VAR, value)
    spec = importlib.util.spec_from_file_location("_smoke_conftest_probe", _CONFTEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── scripts/smoke.sh ─────────────────────────────────────────────────────


def test_smoke_sh_passes_the_variable_into_the_container() -> None:
    text = _SMOKE_SH.read_text(encoding="utf-8")
    assert re.search(rf"-e\s+{_VAR}=", text), f"smoke.sh never passes -e {_VAR}"


def test_smoke_sh_refuses_to_run_when_the_variable_is_unset() -> None:
    text = _SMOKE_SH.read_text(encoding="utf-8")
    # ``${VAR:?message}`` aborts the script when VAR is unset or empty.
    assert re.search(
        rf"-e\s+{_VAR}=\"?\$\{{{_VAR}:\?[^}}]+\}}", text
    ), f"{_VAR} must be passed as ${{{_VAR}:?...}} so an unset value is fatal"


def test_smoke_sh_does_not_default_the_variable() -> None:
    text = _SMOKE_SH.read_text(encoding="utf-8")
    assert not re.search(rf"\$\{{{_VAR}:-", text), "a default defeats the refusal"
    assert not re.search(rf"\$\{{{_VAR}:=", text), "a default defeats the refusal"


def test_smoke_sh_variable_is_passed_before_the_image_argument() -> None:
    """``-e`` after the image name would be handed to pytest, not docker."""
    text = _SMOKE_SH.read_text(encoding="utf-8")
    flag = text.find(f"-e {_VAR}=")
    image = text.find('"$IMAGE"')
    assert flag != -1 and image != -1
    assert flag < image


# ── tests/smoke/conftest.py ──────────────────────────────────────────────


@pytest.mark.parametrize("value", ["v1", "v2"])
def test_conftest_accepts_the_two_layouts(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    module = _load_conftest(monkeypatch, value)
    assert module.EXPECT_LAYOUT == value


@pytest.mark.parametrize("value", [None, "", "v3", "V2", "true", "2", " v2"])
def test_conftest_fails_loudly_on_anything_else(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    with pytest.raises(Exception, match=_VAR):
        _load_conftest(monkeypatch, value)


def test_job_page_url_carries_the_tab_only_on_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    v1 = _load_conftest(monkeypatch, "v1")
    v2 = _load_conftest(monkeypatch, "v2")
    assert v1.job_page_url("abc", "candidates") == "/jobs/abc"
    assert v1.job_page_url("abc", "settings") == "/jobs/abc"
    assert v2.job_page_url("abc", "candidates") == "/jobs/abc?tab=candidates"
    assert v2.job_page_url("abc", "settings") == "/jobs/abc?tab=settings"
