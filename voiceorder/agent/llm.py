"""LLM client abstraction. The agent loop talks to LLMClient; the harness can
swap in Anthropic, a future Vapi/ElevenLabs inline LLM, or a fake for tests.

Message blocks (generic, provider-neutral):
  {"type": "text", "text": str}
  {"type": "tool_call", "id": str, "name": str, "arguments": dict}
  {"type": "tool_result", "tool_call_id": str, "content": str, "is_error": bool}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)  # {id, name, arguments}


class LLMClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        tools: list[dict],
        model: str,
    ) -> LLMResponse: ...


class MissingCredentials(Exception):
    """Raised when an LLM client has no usable credentials."""


def _sanitize_proxy_env() -> None:
    """Drop bracketed IPv6 entries from no_proxy.

    The sandbox sets entries like ``[::1]`` that httpx's proxy handling
    cannot parse (``ValueError``/``InvalidURL`` on client construction).
    Removing them only affects IPv6-localhost bypassing, which this app
    does not rely on.
    """
    for var in ("no_proxy", "NO_PROXY"):
        val = os.environ.get(var)
        if not val:
            continue
        parts = [p for p in val.split(",") if not p.strip().startswith("[")]
        cleaned = ",".join(parts)
        if cleaned != val:
            os.environ[var] = cleaned


def _vault_surrogate() -> str:
    """Fetch the stored Anthropic credential as an egress surrogate.

    Returns the ``hsurr:...`` reference (never the raw key); the sandbox
    egress layer swaps it for the real key on the way out. Returns ""
    when the vault helper is unavailable.
    """
    try:
        import sys

        sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")
        from dynamic_credentials import dynamic_credential_entry

        entry = dynamic_credential_entry("custom.anthropic", "access_token")
        surrogate = str(entry.get("surrogate", "")).strip()
        return surrogate if surrogate.startswith("hsurr:") else ""
    except Exception:
        return ""


def resolve_api_key(explicit: str | None = None) -> str:
    """Resolve the Anthropic API key: explicit arg, env var, then vault."""
    if explicit:
        return explicit
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key
    return _vault_surrogate()


class AnthropicClient:
    """Claude via the Anthropic API. Needs ANTHROPIC_API_KEY."""

    def __init__(self, api_key: str | None = None, max_tokens: int = 1024):
        try:
            import anthropic  # lazy: keeps core/test installs light
        except ImportError as exc:
            raise MissingCredentials(
                "the 'anthropic' package is not installed (pip install anthropic)"
            ) from exc
        _sanitize_proxy_env()
        key = resolve_api_key(explicit=api_key)
        if not key:
            raise MissingCredentials(
                "set ANTHROPIC_API_KEY to run the agent or caller LLM"
            )
        self._client = anthropic.Anthropic(api_key=key)
        self._max_tokens = max_tokens

    def complete(self, *, system, messages, tools, model) -> LLMResponse:
        api_messages = [self._to_api_message(m) for m in messages]
        response = self._client.messages.create(
            model=model,
            max_tokens=self._max_tokens,
            system=system,
            messages=api_messages,
            tools=tools,
        )
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {"id": block.id, "name": block.name, "arguments": dict(block.input)}
                )
        return LLMResponse(text=" ".join(text_parts).strip(), tool_calls=tool_calls)

    @staticmethod
    def _to_api_message(message: dict) -> dict:
        role = message["role"]
        content = message["content"]
        if isinstance(content, str):
            return {"role": role, "content": content}
        blocks = []
        for block in content:
            kind = block["type"]
            if kind == "text":
                blocks.append({"type": "text", "text": block["text"]})
            elif kind == "tool_call":
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": block["arguments"],
                    }
                )
            elif kind == "tool_result":
                content = block["content"]
                if not isinstance(content, str):
                    content = json.dumps(content)
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block["tool_call_id"],
                        "content": content,
                        "is_error": block.get("is_error", False),
                    }
                )
            else:
                raise ValueError(f"unknown block type: {kind}")
        return {"role": role, "content": blocks}
