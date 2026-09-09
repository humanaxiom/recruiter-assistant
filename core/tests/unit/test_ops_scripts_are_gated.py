"""RED — the ops scripts must be inside a gate, and importable.

``scripts/split_taleo_pdf.py`` is the tool that turns a combined Taleo
applicant export into per-applicant PDFs — the sponsor's §S1 input, and the
one piece of this workflow with no in-app equivalent yet. It sat at the repo
root for eleven days, outside every gate, importing ``pipeline.config`` and
``pipeline.llm``: hris module paths that survived the port and resolve to
nothing here. It would have raised ``ModuleNotFoundError`` on the first line
of real use, and ~6,000 tests had nothing to say about it, because nothing
was looking at the file.

Two guards, because there are two ways to lose it again:

1. **The file has to actually import.** ruff and mypy now cover
   ``core/scripts``, which catches an unresolvable import statically — but
   only for files under that directory, which is guard 2's job.
2. **The lint paths in the Makefile and in CI have to agree.** CI does not
   call ``make gates``; it re-spells the same three commands. CLAUDE.md says
   the Makefile is the single source of truth precisely so the gate cannot
   drift from CI, and a duplicated command list is how that promise gets
   broken silently — widen one, forget the other, and a whole directory
   quietly leaves CI while ``verify.sh`` stays green.
"""

from __future__ import annotations

import re
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2]
_REPO = _CORE.parent


def test_the_taleo_splitter_imports() -> None:
    """Executes the module top-to-bottom. Its ``main()`` is behind the usual
    ``__name__`` guard, so this costs an import and proves the thing the
    original defect broke: that every name it depends on resolves HERE."""
    import importlib.util

    path = _CORE / "scripts" / "split_taleo_pdf.py"
    assert path.is_file(), f"{path} is missing — the splitter is the §S1 input tool"

    spec = importlib.util.spec_from_file_location("_split_taleo_pdf", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # ModuleNotFoundError here = the old bug


def _lint_paths(command: str, text: str, *, is_make: bool) -> list[set[str]]:
    """Every real INVOCATION of ``command`` in ``text``, as its path set.

    "Real" is doing work here. Both files also mention these tool names in
    prose — the Makefile's ``## Offline gate suite (ruff·black·mypy·…)`` help
    text, CI's ``name: "Gates: ruff · black · mypy"`` — and the first draft of
    this helper cheerfully parsed ``· black · mypy`` as a path list. So a
    Makefile line counts only when it is a recipe (tab-indented), and a
    workflow line only when it is a ``run:``.
    """
    out: list[set[str]] = []
    for line in text.splitlines():
        if is_make and not line.startswith("\t"):
            continue
        if not is_make and "run:" not in line:
            continue
        for call in re.findall(rf"\b{command}\b([^&|\n]*)", line):
            paths = {
                tok
                for tok in call.split()
                if not tok.startswith("-") and "=" not in tok and tok != "check"
            }
            if paths:
                out.append(paths)
    return out


def test_ci_lints_exactly_what_the_makefile_lints() -> None:
    """CI re-spells the Makefile's commands instead of calling ``make gates``.
    While that is true, the two lists must be pinned together or a directory
    can leave CI without a single test going red."""
    makefile = (_REPO / "Makefile").read_text(encoding="utf-8")
    ci = (_REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    for tool in ("ruff", "black", "mypy"):
        mk = _lint_paths(tool, makefile, is_make=True)
        gh = _lint_paths(tool, ci, is_make=False)
        assert mk, f"the Makefile no longer runs {tool}"
        assert gh, f"CI no longer runs {tool}"
        # Every path set CI uses must be one the Makefile also uses.
        assert gh[0] == mk[0], (
            f"{tool} lints {sorted(mk[0])} in the Makefile but {sorted(gh[0])} "
            "in CI — one of them was widened and the other was not"
        )


def test_the_ops_scripts_directory_is_linted() -> None:
    """The specific widening this file exists to hold in place."""
    makefile = (_REPO / "Makefile").read_text(encoding="utf-8")
    for tool in ("ruff", "black", "mypy"):
        assert "scripts" in _lint_paths(tool, makefile, is_make=True)[0], (
            f"core/scripts left {tool}'s paths — an ops script with a broken "
            "import is unobserved again"
        )
