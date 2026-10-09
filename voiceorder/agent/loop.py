"""Agent loop: caller text in, tool calls out, spoken reply back.

One AgentSession owns a cart and a conversation. The harness drives it turn by
turn; the terminal chat drives it from stdin.
"""
from __future__ import annotations

import uuid
from typing import Callable

from ..core import tools
from ..core.cart import Cart
from ..core.catalog import Catalog
from ..core.ports import PosAdapter, RestaurantContext, ToolResult
from .llm import LLMClient
from .prompt import build_system_prompt, build_tool_schemas

MAX_TOOL_ITERATIONS = 8

# Tools whose success message is already a complete, speakable reply: no
# second LLM round-trip is needed to paraphrase them. This cuts a full
# model call off the slowest turns (submit, transfer).
TERMINAL_TOOLS = {"submit_order", "transfer_call"}

# An extra tool the app plugs in (e.g. request_kitchen_approval): its JSON
# schema, the handler, and whether its message is the final spoken reply.
ExtraTool = tuple[dict, Callable[[dict], ToolResult], bool]
# A guard runs before a tool and may refuse it by returning a ToolResult.
Guard = Callable[[dict], "ToolResult | None"]


class AgentSession:
    def __init__(
        self,
        llm: LLMClient,
        catalog: Catalog,
        pos: PosAdapter,
        ctx: RestaurantContext,
        cart: Cart,
        model: str,
        caller: dict | None = None,
        extra_tools: dict[str, ExtraTool] | None = None,
        guards: dict[str, Guard] | None = None,
    ):
        self.llm = llm
        self.catalog = catalog
        self.pos = pos
        self.ctx = ctx
        self.cart = cart
        self.model = model
        self.caller = caller  # {"phone", "name", "order_count", "last_order"} or None
        self.system = build_system_prompt(catalog, ctx, caller)
        self.extra_tools = extra_tools or {}
        self.guards = guards or {}
        self.tool_schemas = build_tool_schemas() + [t[0] for t in self.extra_tools.values()]
        self.history: list[dict] = []
        # Per-call facts the app keeps across turns (saved with the session),
        # e.g. {"hitl": True, "awaiting_approval": "<id>", "allergy": False}.
        self.state: dict = {}
        self.terminal = TERMINAL_TOOLS | {n for n, t in self.extra_tools.items() if t[2]}

    def add_tool(self, name: str, schema: dict, handler: Callable[[dict], ToolResult],
                 terminal: bool = False) -> None:
        self.extra_tools[name] = (schema, handler, terminal)
        self.tool_schemas = build_tool_schemas() + [t[0] for t in self.extra_tools.values()]
        if terminal:
            self.terminal = self.terminal | {name}

    def _run_tool(self, name: str, arguments: dict) -> ToolResult:
        guard = self.guards.get(name)
        refused = guard(arguments) if guard else None
        if refused is not None:
            return refused
        if name in self.extra_tools:
            return self.extra_tools[name][1](arguments)
        return tools.dispatch(name, cart=self.cart, catalog=self.catalog, pos=self.pos,
                              ctx=self.ctx, arguments=arguments)

    def handle_caller_message(self, text: str) -> dict:
        """Run one caller turn. Returns {"text": spoken reply, "tool_calls": [...]}."""
        self.history.append({"role": "user", "content": text})
        tool_calls_made: list[dict] = []
        reply = ""
        for _ in range(MAX_TOOL_ITERATIONS):
            response = self.llm.complete(
                system=self.system,
                messages=self.history,
                tools=self.tool_schemas,
                model=self.model,
            )
            if not response.tool_calls:
                reply = response.text
                self.history.append({"role": "assistant", "content": reply})
                break
            assistant_blocks: list[dict] = []
            result_blocks: list[dict] = []
            terminal_reply: str | None = None
            for call in response.tool_calls:
                call_id = call.get("id") or uuid.uuid4().hex[:8]
                assistant_blocks.append(
                    {
                        "type": "tool_call",
                        "id": call_id,
                        "name": call["name"],
                        "arguments": call.get("arguments", {}),
                    }
                )
                result = self._run_tool(call["name"], call.get("arguments", {}) or {})
                tool_calls_made.append(
                    {
                        "id": call_id,
                        "name": call["name"],
                        "arguments": call.get("arguments", {}),
                        "ok": result.ok,
                        "error_code": result.error_code,
                    }
                )
                result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_call_id": call_id,
                        "content": result.to_dict(),
                        "is_error": not result.ok,
                    }
                )
                if result.ok and call["name"] in self.terminal:
                    terminal_reply = result.message
            self.history.append({"role": "assistant", "content": assistant_blocks})
            self.history.append({"role": "user", "content": result_blocks})
            if terminal_reply is not None:
                # The tool already produced the final spoken reply; skip the
                # extra LLM round-trip that would only paraphrase it.
                reply = terminal_reply
                self.history.append({"role": "assistant", "content": reply})
                break
        else:
            reply = "Let me connect you to the restaurant to make sure we get this right."
            self.history.append({"role": "assistant", "content": reply})
        return {"text": reply, "tool_calls": tool_calls_made}
