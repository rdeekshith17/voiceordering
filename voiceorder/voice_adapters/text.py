from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.ports import CallRecord, CallStart, ToolCall, ToolResult


@dataclass
class TextVoiceAdapter:
    """Plain JSON in and out, for chat and tests. Real platforms (ElevenLabs, Vapi)
    get their own adapters in Phase 5 that speak the same tool contract."""

    def parse_call_start(self, req: dict[str, Any]) -> CallStart:
        return CallStart(
            call_id=req["call_id"],
            called_number=req["called_number"],
            caller_number=req.get("caller_number"),
        )

    def call_start_response(self, restaurant: Any) -> dict[str, Any]:
        return {
            "restaurant_id": restaurant.id,
            "greeting": f"Thanks for calling {restaurant.name}, what can I get started for you?",
        }

    def parse_tool_calls(self, req: dict[str, Any]) -> list[ToolCall]:
        return [
            ToolCall(
                call_id=req["call_id"],
                tool_name=tc["name"],
                arguments=tc.get("arguments", {}),
                tool_call_id=tc["tool_call_id"],
            )
            for tc in req["tool_calls"]
        ]

    def tool_results_response(self, results: list[ToolResult]) -> dict[str, Any]:
        return {
            "results": [
                {"tool_call_id": r.tool_call_id, "result": r.result} for r in results
            ]
        }

    def parse_call_end(self, req: dict[str, Any]) -> CallRecord:
        return CallRecord(
            call_id=req["call_id"],
            transcript=req.get("transcript"),
            ended_reason=req.get("ended_reason"),
        )
