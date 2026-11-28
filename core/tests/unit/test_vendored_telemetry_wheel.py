"""ADR-048 — the vendored sfu-telemetry wheel named in requirements.txt is the
one installed.

pip keeps an installed distribution whose version already satisfies the
requirement. If the vendored wheel is rebuilt WITHOUT a version bump, an
environment that already has that version (a dev venv, a long-lived image
layer) silently keeps the old code. The 0.1.0+sfu rebuild did exactly that on
the pilot's end-to-end run. These tests tie the three together: the filename
in requirements.txt, the file in core/vendor/, and the installed version.
"""

from __future__ import annotations

import re
from pathlib import Path

import sfu_telemetry

CORE = Path(__file__).parents[2]


def _wheel_line() -> str:
    lines = [
        ln.strip()
        for ln in (CORE / "requirements.txt").read_text().splitlines()
        if "sfu_telemetry-" in ln and not ln.lstrip().startswith("#")
    ]
    assert len(lines) == 1, lines
    return lines[0]


def test_requirements_names_exactly_one_vendored_wheel_that_exists() -> None:
    path = CORE / _wheel_line()
    assert path.is_file(), path
    assert [p.name for p in (CORE / "vendor").glob("sfu_telemetry-*.whl")] == [
        path.name
    ]


def test_installed_version_is_the_vendored_one() -> None:
    m = re.search(r"sfu_telemetry-([^-]+)-py3", _wheel_line())
    assert m is not None
    assert sfu_telemetry.__version__ == m.group(1)
    assert "+sfu" in sfu_telemetry.__version__  # local label: never on PyPI
