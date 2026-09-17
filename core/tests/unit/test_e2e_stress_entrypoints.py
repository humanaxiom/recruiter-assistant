"""RED — pins that ``scripts/e2e.sh`` and ``scripts/stress.sh`` exist,
are executable, run under ``set -euo pipefail`` (the same discipline every
other script in ``scripts/`` uses), and target the ISOLATED stress compose
project (``-p recruiter-stress``) rather than the default project — so a
stress run can never collide with, or reset, a developer's normal
``docker compose`` stack.

``core/tests/unit/test_ops_scripts_are_gated.py`` covers ``core/scripts``
(the Python ops-tooling directory under mypy/ruff); it does not enumerate the
repo-root ``scripts/`` shell scripts, so this is a separate, narrower check
in the same spirit rather than an extension of that file.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO / "scripts"

ENTRYPOINTS = ["e2e.sh", "stress.sh"]


@pytest.mark.parametrize("name", ENTRYPOINTS)
def test_entrypoint_exists(name: str) -> None:
    path = _SCRIPTS / name
    assert path.is_file(), f"{path} is missing"


@pytest.mark.parametrize("name", ENTRYPOINTS)
def test_entrypoint_is_executable(name: str) -> None:
    path = _SCRIPTS / name
    if not path.is_file():
        pytest.fail(f"{path} is missing")
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR, f"{path} is not marked executable (chmod +x)"


@pytest.mark.parametrize("name", ENTRYPOINTS)
def test_entrypoint_uses_strict_bash_mode(name: str) -> None:
    path = _SCRIPTS / name
    if not path.is_file():
        pytest.fail(f"{path} is missing")
    text = path.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text, (
        f"{path} must run under `set -euo pipefail`, like every other script "
        "in scripts/"
    )


@pytest.mark.parametrize("name", ENTRYPOINTS)
def test_entrypoint_targets_the_isolated_stress_compose_project(name: str) -> None:
    """The task brief is explicit: the isolated stack is brought up with
    ``docker compose -p recruiter-stress``. Running the stress/e2e harness
    against the DEFAULT compose project would let it reset or collide with a
    developer's normal dev stack."""
    path = _SCRIPTS / name
    if not path.is_file():
        pytest.fail(f"{path} is missing")
    text = path.read_text(encoding="utf-8")
    assert "-p recruiter-stress" in text, (
        f"{path} must target the isolated `-p recruiter-stress` compose "
        "project, never the default one"
    )


@pytest.mark.parametrize("name", ENTRYPOINTS)
def test_entrypoint_has_a_shebang(name: str) -> None:
    path = _SCRIPTS / name
    if not path.is_file():
        pytest.fail(f"{path} is missing")
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    assert first_line.startswith("#!"), f"{path} has no shebang line"


def test_entrypoints_are_distinct_files() -> None:
    """e2e.sh (functional flow) and stress.sh (load/concurrency) are two
    different concerns — one must not be a symlink/alias of the other,
    silently making one of the two purposes untestable on its own."""
    e2e = _SCRIPTS / "e2e.sh"
    stress = _SCRIPTS / "stress.sh"
    if not (e2e.is_file() and stress.is_file()):
        pytest.fail("both scripts/e2e.sh and scripts/stress.sh must exist")
    assert os.path.realpath(e2e) != os.path.realpath(stress)
