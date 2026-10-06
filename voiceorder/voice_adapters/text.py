"""Text voice adapter: plain JSON in and out.

Used for the terminal chat, the web demo, and all Phase 1 tests. ElevenLabs
and Vapi adapters implement the same VoiceAdapter protocol in Phase 5.
"""
from __future__ import annotations

import uuid

from ..core.ports import (
    CallRecord,
    CallStart,
    RestaurantContext,
    ToolCall,
    ToolResult,
)


class TextAdapter:
    def parse_call_start(self, req: dict) -> CallStart:
        return CallStart(
            called_number=str(req.get("called_number", "unknown")),
            caller_id=str(req.get("caller_id", "unknown")),
            call_id=str(req.get("call_id") or uuid.uuid4().hex[:12]),
        )

    def call_start_response(self, ctx: RestaurantContext) -> dict:
        return {
            "restaurant": ctx.restaurant_name,
            "greeting": (
                f"Thanks for calling {ctx.restaurant_name}! "
                "This call may be recorded. What can I get started for you?"
            ),
            "config": {
                "pos_profile": ctx.pos_profile,
                "pickup_minutes": ctx.pickup_minutes,
                "transfer_number": ctx.transfer_number,
            },
        }

    def parse_tool_calls(self, req: dict) -> list[ToolCall]:
        calls = []
        for entry in req.get("tool_calls", []):
            calls.append(
                ToolCall(
                    name=str(entry.get("name", "")),
                    arguments=dict(entry.get("arguments", {})),
                    call_id=str(entry.get("call_id") or uuid.uuid4().hex[:8]),
                )
            )
        return calls

    def tool_results_response(self, results: list[ToolResult]) -> dict:
        return {"results": [r.to_dict() for r in results]}

    def parse_call_end(self, req: dict) -> CallRecord:
        return CallRecord(
            call_id=str(req.get("call_id", "unknown")),
            restaurant_id=str(req.get("restaurant_id", "unknown")),
            duration_seconds=float(req.get("duration_seconds", 0.0)),
            transcript=str(req.get("transcript", "")),
            order_id=req.get("order_id"),
        )

