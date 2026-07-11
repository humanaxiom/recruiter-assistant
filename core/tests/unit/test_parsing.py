"""Unit tests for robust model-output parsing (file blocks + JSON)."""

from __future__ import annotations

import json

import pytest

from src.agents.parsing import extract_file_blocks, extract_json


class TestExtractFileBlocks:
    def test_single_block(self) -> None:
        text = "```python path=src/a.py\nprint('hi')\n```"
        assert extract_file_blocks(text) == {"src/a.py": "print('hi')\n"}

    def test_multiple_blocks(self) -> None:
        text = (
            "```python path=src/a.py\nA\n```\n\n"
            "```python path=tests/test_a.py\nB\n```"
        )
        blocks = extract_file_blocks(text)
        assert blocks == {"src/a.py": "A\n", "tests/test_a.py": "B\n"}

    def test_nested_fences_are_preserved(self) -> None:
        """A Markdown/Mermaid body with inner fences must not truncate."""
        body = "# ADR\n\n```mermaid\ngraph LR\n  A-->B\n```\n\nDone.\n"
        text = f"```markdown path=docs/adr/1.md\n{body}```"
        blocks = extract_file_blocks(text)
        assert blocks["docs/adr/1.md"] == body
        assert "```mermaid" in blocks["docs/adr/1.md"]

    def test_trailing_prose_after_closer_dropped(self) -> None:
        text = "```python path=src/a.py\ncode\n```\nsome trailing prose"
        assert extract_file_blocks(text) == {"src/a.py": "code\n"}

    def test_no_blocks_returns_empty(self) -> None:
        assert extract_file_blocks("no fences here") == {}

    def test_block_without_closer(self) -> None:
        text = "```python path=src/a.py\ncode without closing fence"
        assert extract_file_blocks(text) == {"src/a.py": "code without closing fence"}


class TestExtractJson:
    def test_bare_json(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self) -> None:
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_language(self) -> None:
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_wrapped_json(self) -> None:
        text = 'Here is the plan:\n{"a": 1, "b": [2, 3]}\nThanks!'
        assert extract_json(text) == {"a": 1, "b": [2, 3]}

    def test_invalid_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            extract_json("not json at all")
