"""Model config: swap models in code, route through the LangSmith gateway via env.

Follows the LSD deployment demo pattern — model names are code constants and the
gateway credentials/endpoint are resolved from the single LangSmith key.
"""

from __future__ import annotations

from throughline.models import (
    EDITOR_MODEL,
    WORKER_MODEL,
    _gateway_kwargs,
    build_model,
)


def test_model_names_are_code_config() -> None:
    # Swappable in code (not via env), and used to build the module models.
    assert WORKER_MODEL == "claude-haiku-4-5"
    assert EDITOR_MODEL == "claude-sonnet-4-6"
    assert build_model(WORKER_MODEL).model == "claude-haiku-4-5"
    assert build_model(EDITOR_MODEL).model == "claude-sonnet-4-6"


def test_gateway_routing_uses_langsmith_key_and_base_url(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY_GATEWAY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.smith.langchain.com/anthropic")

    kwargs = _gateway_kwargs()
    assert kwargs["api_key"] == "lsv2_test_key"
    assert kwargs["base_url"] == "https://gateway.smith.langchain.com/anthropic"


def test_direct_anthropic_when_no_gateway(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY_GATEWAY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-direct")

    kwargs = _gateway_kwargs()
    assert kwargs["api_key"] == "sk-ant-direct"
    assert "base_url" not in kwargs


def test_gateway_key_override_takes_precedence(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY_GATEWAY", "lsv2_gateway_override")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-ignored")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_should-be-ignored")

    assert _gateway_kwargs()["api_key"] == "lsv2_gateway_override"
