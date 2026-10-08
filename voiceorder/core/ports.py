"""Port interfaces: the boundaries between the order engine and vendors.

The order engine never imports a vendor SDK. Two thin adapter layers translate
at the edges, and each restaurant's config names its POS and voice platform.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Voice side
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CallStart:
    called_number: str
    caller_id: str
    call_id: str


@dataclass(frozen=True)
class RestaurantContext:
    restaurant_id: str
    restaurant_name: str
    pos_profile: str  # square_like | clover_like | toast_like (Phase 1)
    voice_platform: str  # text | elevenlabs | vapi
    transfer_number: str
    pickup_minutes: int = 20
    tax_rate: float = 0.0825
    tenant_id: str = ""  # multi-tenant owner; "" = legacy single-tenant path
    max_lines: int = 20
    max_quantity_per_line: int = 12
    max_order_total: float = 500.0


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict
    call_id: str


@dataclass
class ToolResult:
    ok: bool
    message: str  # spoken-style line for the agent to say
    data: dict = field(default_factory=dict)
    error_code: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "message": self.message,
            "data": self.data,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class CallRecord:
    call_id: str
    restaurant_id: str
    duration_seconds: float
    transcript: str
    order_id: str | None = None


class VoiceAdapter(Protocol):
    """Translates one voice platform's webhooks into engine calls."""

    def parse_call_start(self, req: dict) -> CallStart: ...
    def call_start_response(self, ctx: RestaurantContext) -> dict: ...
    def parse_tool_calls(self, req: dict) -> list[ToolCall]: ...
    def tool_results_response(self, results: list[ToolResult]) -> dict: ...
    def parse_call_end(self, req: dict) -> CallRecord: ...


# ---------------------------------------------------------------------------
# POS side
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PosCapabilities:
    unpaid_orders_visible: bool  # staff can see the order before it's paid
    payment_links: bool  # POS supports hosted payment links
    pay_at_pickup: bool


@dataclass(frozen=True)
class Totals:
    subtotal: float
    tax: float
    total: float
    currency: str = "USD"


@dataclass(frozen=True)
class PosOrder:
    order_id: str
    order_number: str
    status: str  # received | pending_payment | failed
    pickup_time: str
    totals: Totals


@dataclass(frozen=True)
class PaymentStep:
    kind: str  # link | pay_at_pickup
    instructions: str
    url: str | None = None


class PosError(Exception):
    """Raised by a POS adapter when the vendor (or fake) fails."""


class PosAdapter(Protocol):
    """Translates engine operations into one POS system's API."""

    @property
    def capabilities(self) -> PosCapabilities: ...

    def sync_catalog(self, restaurant: RestaurantContext) -> Any: ...
    def quote(self, cart: Any) -> Totals: ...
    def submit(self, cart: Any, idempotency_key: str) -> PosOrder: ...
    def payment_step(self, order: PosOrder) -> PaymentStep: ...
    def is_available(self, item_ref: str) -> bool: ...


def local_now(timezone: str = "") -> datetime:
    """Now in a restaurant's IANA time zone (e.g. "America/Chicago").

    Falls back to the server clock (which honours the TZ env var) when the
    zone is unset or unknown, so a bad setting never breaks an order."""
    if timezone:
        try:
            return datetime.now(ZoneInfo(timezone))
        except Exception:
            pass
    return datetime.now()
