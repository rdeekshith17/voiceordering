from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .cart import Cart
    from .catalog import Catalog


@dataclass(frozen=True)
class PosCapabilities:
    unpaid_orders_visible: bool
    payment_link: bool
    requires_linked_modifiers: bool = False
    totals_via_quote_only: bool = False


@dataclass
class Totals:
    subtotal_cents: int
    tax_cents: int
    total_cents: int


@dataclass
class PosOrder:
    order_id: str
    pickup_time: str
    raw: dict[str, Any]


@dataclass
class PaymentStep:
    kind: str  # "link" or "pay_at_pickup"
    detail: str | None = None


class PosAdapter(Protocol):
    capabilities: PosCapabilities

    def sync_catalog(self, restaurant_id: str) -> Catalog: ...
    def quote(self, cart: Cart) -> Totals: ...
    def submit(self, cart: Cart, idempotency_key: str) -> PosOrder: ...
    def payment_step(self, order: PosOrder) -> PaymentStep: ...


@dataclass
class CallStart:
    call_id: str
    called_number: str
    caller_number: str | None = None


@dataclass
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    tool_call_id: str


@dataclass
class ToolResult:
    tool_call_id: str
    result: dict[str, Any]


@dataclass
class CallRecord:
    call_id: str
    transcript: str | None = None
    ended_reason: str | None = None


class VoiceAdapter(Protocol):
    def parse_call_start(self, req: Any) -> CallStart: ...
    def call_start_response(self, restaurant: Any) -> dict[str, Any]: ...
    def parse_tool_calls(self, req: Any) -> list[ToolCall]: ...
    def tool_results_response(self, results: list[ToolResult]) -> dict[str, Any]: ...
    def parse_call_end(self, req: Any) -> CallRecord: ...
