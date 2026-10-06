"""Agent loop: caller text in, tool calls out, spoken reply back.

One AgentSession owns a cart and a conversation. The harness drives it turn by
turn; the terminal chat drives it from stdin.
"""
from __future__ import annotations

import uuid

from ..core import tools
from ..core.cart import Cart
from ..core.catalog import Catalog
from ..core.ports import PosAdapter, RestaurantContext
from .llm import LLMClient
from .prompt import build_system_prompt, build_tool_schemas

MAX_TOOL_ITERATIONS = 8

# Tools whose success message is already a complete, speakable reply: no
# second LLM round-trip is needed to paraphrase them. This cuts a full
# model call off the slowest turns (submit, transfer).
TERMINAL_TOOLS = {"submit_order", "transfer_call"}


class AgentSession:
    def __init__(
        self,
        llm: LLMClient,
        catalog: Catalog,
        pos: PosAdapter,
        ctx: RestaurantContext,
        cart: Cart,
        model: str,
    ):
        self.llm = llm
        self.catalog = catalog
        self.pos = pos
        self.ctx = ctx
        self.cart = cart
        self.model = model
        self.system = build_system_prompt(catalog, ctx)
        self.tool_schemas = build_tool_schemas()
        self.history: list[dict] = []

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
                result = tools.dispatch(
                    call["name"],
                    cart=self.cart,
                    catalog=self.catalog,
                    pos=self.pos,
                    ctx=self.ctx,
                    arguments=call.get("arguments", {}),
                )
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
                if result.ok and call["name"] in TERMINAL_TOOLS:
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
