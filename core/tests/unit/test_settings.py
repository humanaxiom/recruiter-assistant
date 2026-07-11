"""Unit tests for Settings — defaults and lru_cache identity."""

from __future__ import annotations

from src.settings import Settings, get_settings


def test_defaults() -> None:
    s = Settings()
    assert s.agent_model == "qwen2.5-coder:14b"
    assert s.embed_model == "nomic-embed-text"
    assert s.max_review_iterations == 5
    assert s.coverage_threshold == 80
    assert s.ollama_base_url.endswith("/v1")
    assert s.api_base_url == "http://api:8000"


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_env_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("COVERAGE_THRESHOLD", "55")
    assert Settings().coverage_threshold == 55
