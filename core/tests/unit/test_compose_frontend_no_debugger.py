"""Compose-invariant meta-test — pins that the frontend service's ``command:``
in every ``docker-compose*.yml``/``compose*.yml`` in the repo never runs Flask
with the Werkzeug INTERACTIVE debugger enabled.

**The hole this test exists to close.** ``docker-compose.yml`` ran the
frontend as ``flask --app frontend.app run --host 0.0.0.0 --port 5000
--debug``. Flask's ``--debug`` turns on the Werkzeug interactive debugger,
which serves a Python REPL (``/console`` and any 500 traceback page) with NO
authentication of its own — the console is gated only by werkzeug's own PIN
prompt, which the review found reachable through the nginx reverse proxy with
a forged ``Host`` header while the site was live on the public internet at
https://sfuai.ca:8000. An interactive Python shell reachable by anyone who can
reach the proxy is a remote-code-execution hole, not a debugging convenience.

``--reload`` (auto-reload on the bind-mounted source, ``./core:/app`` in
``docker-compose.yml``) is a *different* flag and stays — losing it would
silently break the dev inner loop. ``--no-debugger`` is the fix: it disables
just the interactive console while everything else about ``flask run``
(including ``--reload``) is unchanged.

This is a pure text-parsing meta-test (no code import, no Docker), mirroring
``test_gates_cover_frontend.py``'s "make the rule enforceable by the gate, not
just by convention" approach — grepping the compose file's frontend service
``command:`` line directly, since there is no Python AST for a YAML compose
file's shell command string.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Every compose file in the repo root that could plausibly define services.
_COMPOSE_FILES = sorted(
    p
    for p in _REPO_ROOT.glob("*.yml")
    if p.name.startswith("docker-compose") or p.name.startswith("compose")
)

# Matches a standalone `--debug` flag (not `--debugger`/`--no-debugger`, and
# not some other flag that merely contains the substring).
_STANDALONE_DEBUG_FLAG = re.compile(r"(?<![\w-])--debug(?![\w-])")


def _frontend_command_lines(compose_text: str) -> list[str]:
    """Return every line inside the ``frontend:`` service block (up to the
    next top-level or same-indent service key) that contains ``command:``.

    Scoped to the ``frontend:`` block specifically (not ``api:`` — the API
    already runs uvicorn with ``--reload``, unrelated to this flag) by
    matching from the ``  frontend:`` service header to the next 2-space
    indented key.
    """
    pattern = re.compile(
        r"\n  frontend:\n(.*?)(?=\n  [A-Za-z0-9_-]+:\n|\Z)",
        re.DOTALL,
    )
    m = pattern.search(compose_text)
    if m is None:
        return []
    block = m.group(1)
    return [line for line in block.splitlines() if "command:" in line]


@pytest.mark.parametrize("compose_path", _COMPOSE_FILES, ids=lambda p: p.name)
def test_frontend_command_never_enables_the_interactive_debugger(
    compose_path: Path,
) -> None:
    text = compose_path.read_text(encoding="utf-8")
    command_lines = _frontend_command_lines(text)
    for line in command_lines:
        assert not _STANDALONE_DEBUG_FLAG.search(line), (
            f"{compose_path.name}: frontend `command:` must never run with a "
            f"standalone `--debug` flag (enables the Werkzeug interactive "
            f"debugger/console with no auth of its own), got: {line!r}"
        )


def test_docker_compose_yml_frontend_command_disables_the_debugger_explicitly() -> None:
    """The base ``docker-compose.yml`` is the file that actually starts the
    frontend with ``flask run`` (the CAS override only adds ``environment:``
    entries) — pin the positive fix, not just the absence of ``--debug``, so a
    future edit that drops ``--no-debugger`` without adding `--debug` back is
    still caught."""
    compose_path = _REPO_ROOT / "docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")
    command_lines = _frontend_command_lines(text)
    assert (
        command_lines
    ), "expected a `command:` line in docker-compose.yml's frontend service"
    assert any("--no-debugger" in line for line in command_lines), (
        "docker-compose.yml's frontend `command:` must include `--no-debugger`, "
        f"got: {command_lines!r}"
    )
    assert any("--reload" in line for line in command_lines), (
        "docker-compose.yml's frontend `command:` must keep `--reload` "
        "(bind-mounted source auto-reload) — only the debugger should be "
        f"removed, got: {command_lines!r}"
    )


def test_compose_files_glob_finds_at_least_the_two_known_files() -> None:
    """Guards the glob itself: if it ever matched nothing, the parametrized
    test above would vacuously pass with zero cases collected."""
    names = {p.name for p in _COMPOSE_FILES}
    assert "docker-compose.yml" in names
    assert "compose.cas.yml" in names
