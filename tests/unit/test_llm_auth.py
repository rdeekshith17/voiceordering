"""Anthropic credential resolution: explicit > env > vault surrogate (no network)."""
import os

import pytest

from voiceorder.agent import llm as llm_mod
from voiceorder.agent.llm import (
    AnthropicClient,
    MissingCredentials,
    _sanitize_proxy_env,
    resolve_api_key,
)


def test_resolve_prefers_explicit_over_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert resolve_api_key(explicit="explicit-key") == "explicit-key"


def test_resolve_uses_env_when_no_explicit(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert resolve_api_key() == "env-key"


def test_resolve_falls_back_to_vault_surrogate(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_mod, "_vault_surrogate", lambda: "hsurr:test-value")
    assert resolve_api_key() == "hsurr:test-value"


def test_resolve_empty_when_nothing_available(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_mod, "_vault_surrogate", lambda: "")
    assert resolve_api_key() == ""


def test_sanitize_proxy_env_drops_bracketed_entries(monkeypatch):
    monkeypatch.setenv(
        "no_proxy", "localhost,127.0.0.1,[::1],[fd8b:4f84:7d32:99::1]"
    )
    monkeypatch.setenv("NO_PROXY", "example.com,[::1]")
    _sanitize_proxy_env()
    assert os.environ["no_proxy"] == "localhost,127.0.0.1"
    assert os.environ["NO_PROXY"] == "example.com"


def test_sanitize_proxy_env_leaves_clean_values(monkeypatch):
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    _sanitize_proxy_env()
    assert os.environ["no_proxy"] == "localhost,127.0.0.1"


def test_to_api_message_serializes_tool_result_dict():
    msg = AnthropicClient._to_api_message(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_call_id": "c1",
                    "content": {"ok": True, "items": [{"name": "taco"}]},
                    "is_error": False,
                }
            ],
        }
    )
    block = msg["content"][0]
    assert block["type"] == "tool_result"
    assert isinstance(block["content"], str)
    assert '"ok": true' in block["content"] or '"ok":true' in block["content"]


def test_client_uses_resolved_key_without_network(monkeypatch):
    monkeypatch.setattr(llm_mod, "resolve_api_key", lambda explicit=None: "test-key")
    client = AnthropicClient()
    assert client._client.api_key == "test-key"


def test_client_raises_when_no_credential(monkeypatch):
    monkeypatch.setattr(llm_mod, "resolve_api_key", lambda explicit=None: "")
    with pytest.raises(MissingCredentials):
        AnthropicClient()
