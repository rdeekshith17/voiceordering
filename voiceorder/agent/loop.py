from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import anthropic

from ..core.tools import OrderTools, ToolError
from .prompt import build_system_prompt
from .tool_schemas import TOOL_SCHEMAS

DEFAULT_MODEL = "claude-sonnet-5"


@dataclass
class AgentSession:
    """Drives one call: sends the caller's line to Claude, executes any tool
    calls against OrderTools, and returns the agent's spoken reply. This is
    the same loop a text terminal or a voice adapter's webhook sits on top of."""

    tools: OrderTools
    restaurant_name: str
    client: anthropic.Anthropic = field(default_factory=anthropic.Anthropic)
    model: str = DEFAULT_MODEL
    max_tool_rounds: int = 8
    messages: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.system_prompt = build_system_prompt(self.restaurant_name, self.tools.catalog)

    def send(self, caller_message: str) -> str:
        self.messages.append({"role": "user", "content": caller_message})

        for _ in range(self.max_tool_rounds):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=self.system_prompt,
                tools=TOOL_SCHEMAS,
                messages=self.messages,
            )
            self.messages.append(
                {"role": "assistant", "content": response.model_dump()["content"]}
            )
            if response.stop_reason != "tool_use":
                return "".join(
                    block.text for block in response.content if block.type == "text"
                )
            tool_results = [
                self._run_tool(block)
                for block in response.content
                if block.type == "tool_use"
            ]
            self.messages.append({"role": "user", "content": tool_results})

        raise RuntimeError("agent used too many tool calls in a single turn")

    def _run_tool(self, block: Any) -> dict[str, Any]:
        method = getattr(self.tools, block.name, None)
        try:
            if method is None or block.name.startswith("_"):
                raise ToolError(f"unknown tool: {block.name}")
            result = method(**block.input)
            content = json.dumps(result)
        except ToolError as e:
            content = json.dumps({"error": e.message})
        return {"type": "tool_result", "tool_use_id": block.id, "content": content}
