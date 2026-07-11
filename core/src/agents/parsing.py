"""Robust extraction of agent model output.

Two things every agent needs to pull out of a raw LLM response:

- **File blocks** — fenced sections tagged ``path=`` that carry full file
  contents. The naive ``r"```...(.*?)```"`` regex breaks the moment a block
  body contains its own fence (Markdown, Mermaid, nested code samples), which
  is exactly what the DocsAgent is asked to emit. This parser keys off the
  ``path=`` openers instead, so inner fences never truncate a block.
- **JSON** — planner/reviewer/security expect a single JSON object, but models
  wrap it in prose or ```` ```json ```` fences. ``extract_json`` tries the
  plausible candidates in order rather than a brittle prefix/suffix strip.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Opening line of a file block, e.g. ``` ```python path=src/foo.py ```
_OPENER = re.compile(r"^```[A-Za-z0-9_+-]*[ \t]+path=(\S+)[^\n]*$", re.MULTILINE)
# A standalone closing fence on its own line.
_CLOSER = re.compile(r"^```[ \t]*$", re.MULTILINE)


def extract_file_blocks(text: str) -> dict[str, str]:
    """Return ``{path: content}`` for every ``path=``-tagged fenced block.

    Robust to fences *inside* a block body: a block runs from its ``path=``
    opener to the next opener (or end of text), and its trailing closing fence
    is the last standalone ```` ``` ```` in that span — so paired inner fences
    are preserved and stray prose after the closer is dropped.
    """
    openers = list(_OPENER.finditer(text))
    files: dict[str, str] = {}
    for i, match in enumerate(openers):
        path = match.group(1)
        start = match.end()
        end = openers[i + 1].start() if i + 1 < len(openers) else len(text)
        body = text[start:end]
        if body.startswith("\n"):
            body = body[1:]
        closers = list(_CLOSER.finditer(body))
        if closers:
            body = body[: closers[-1].start()]
        files[path] = body
    return files


def _json_candidates(text: str) -> list[str]:
    stripped = text.strip()
    candidates = [stripped]
    fenced = re.search(r"```(?:json)?[ \t]*\n(.*?)\n```", stripped, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    first, last = stripped.find("{"), stripped.rfind("}")
    if 0 <= first < last:
        candidates.append(stripped[first : last + 1])
    return candidates


def extract_json(text: str) -> Any:
    """Parse the first valid JSON object embedded in ``text``.

    Tries the whole string, then any fenced block, then the ``{...}`` span.
    Raises ``json.JSONDecodeError`` if none parse — callers already treat that
    as a structured failure.
    """
    for candidate in _json_candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("no valid JSON found in model output", text, 0)
