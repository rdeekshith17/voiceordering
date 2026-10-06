"""Cart: line items plus the 5-state order state machine.

States: empty -> building -> read_back -> confirmed -> submitted.
The single decision point: submit_order only succeeds after a read-back and an
explicit yes from the caller. Any mutation after a read-back drops the cart
back to `building`, so a changed order is always read back again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CartState(str, Enum):
    EMPTY = "empty"
    BUILDING = "building"
    READ_BACK = "read_back"
    CONFIRMED = "confirmed"
    SUBMITTED = "submitted"


class CartError(Exception):
    """Raised when an operation violates the cart state machine."""


@dataclass
class CartLine:
    line_id: str
    item_ref: str
    item_name: str
    quantity: int
    unit_price: float
    variation_id: str | None = None
    variation_name: str | None = None
    modifier_ids: list[str] = field(default_factory=list)
    modifier_names: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def line_total(self) -> float:
        return round(self.unit_price * self.quantity, 2)

    def describe(self) -> str:
        parts = [f"{self.quantity} x {self.item_name}"]
        if self.variation_name:
            parts.append(f"({self.variation_name})")
        if self.modifier_names:
            parts.append("[" + ", ".join(self.modifier_names) + "]")
        if self.note:
            parts.append(f"[{self.note}]")
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "item_ref": self.item_ref,
            "item_name": self.item_name,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "variation_id": self.variation_id,
            "variation_name": self.variation_name,
            "modifier_ids": list(self.modifier_ids),
            "modifier_names": list(self.modifier_names),
            "note": self.note,
            "line_total": self.line_total,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "CartLine":
        return cls(
            line_id=raw["line_id"],
            item_ref=raw["item_ref"],
            item_name=raw["item_name"],
            quantity=int(raw["quantity"]),
            unit_price=float(raw["unit_price"]),
            variation_id=raw.get("variation_id"),
            variation_name=raw.get("variation_name"),
            modifier_ids=list(raw.get("modifier_ids", [])),
            modifier_names=list(raw.get("modifier_names", [])),
            note=raw.get("note"),
        )


class Cart:
    def __init__(self, cart_id: str, restaurant_id: str, idempotency_key: str):
        self.cart_id = cart_id
        self.restaurant_id = restaurant_id
        self.idempotency_key = idempotency_key
        self.lines: list[CartLine] = []
        self.state = CartState.EMPTY
        self.customer_name: str | None = None
        self.customer_phone: str | None = None
        self.last_order: dict | None = None
        self.transferred: bool = False
        self.transfer_reason: str | None = None
        self._line_seq = 0

    # -- state machine -----------------------------------------------------
    def _ensure_mutable(self) -> None:
        if self.transferred:
            raise CartError("call was transferred to staff; the cart is frozen")
        if self.state in (CartState.CONFIRMED, CartState.SUBMITTED):
            raise CartError(f"cart is {self.state.value}; it can no longer change")
        if self.state == CartState.READ_BACK:
            # Caller changed something after the read-back: read back again.
            self.state = CartState.BUILDING

    def _next_line_id(self) -> str:
        self._line_seq += 1
        return f"L{self._line_seq}"

    # -- mutations ----------------------------------------------------------
    def add_line(
        self,
        *,
        item_ref: str,
        item_name: str,
        quantity: int,
        unit_price: float,
        variation_id: str | None = None,
        variation_name: str | None = None,
        modifier_ids: list[str] | None = None,
        modifier_names: list[str] | None = None,
        note: str | None = None,
    ) -> CartLine:
        self._ensure_mutable()
        line = CartLine(
            line_id=self._next_line_id(),
            item_ref=item_ref,
            item_name=item_name,
            quantity=quantity,
            unit_price=round(unit_price, 2),
            variation_id=variation_id,
            variation_name=variation_name,
            modifier_ids=list(modifier_ids or []),
            modifier_names=list(modifier_names or []),
            note=note,
        )
        self.lines.append(line)
        self.state = CartState.BUILDING
        return line

    def find_line(self, line_id: str) -> CartLine | None:
        for line in self.lines:
            if line.line_id == line_id:
                return line
        return None

    def update_line(self, line_id: str, **changes) -> CartLine:
        self._ensure_mutable()
        line = self.find_line(line_id)
        if line is None:
            raise CartError(f"no line {line_id} in cart")
        for key, value in changes.items():
            if value is not None and hasattr(line, key):
                setattr(line, key, value)
        if "unit_price" in changes and changes["unit_price"] is not None:
            line.unit_price = round(float(changes["unit_price"]), 2)
        self.state = CartState.BUILDING
        return line

    def remove_line(self, line_id: str) -> CartLine:
        self._ensure_mutable()
        line = self.find_line(line_id)
        if line is None:
            raise CartError(f"no line {line_id} in cart")
        self.lines.remove(line)
        self.state = CartState.BUILDING if self.lines else CartState.EMPTY
        return line

    # -- reads ---------------------------------------------------------------
    @property
    def subtotal(self) -> float:
        return round(sum(l.line_total for l in self.lines), 2)

    def is_empty(self) -> bool:
        return not self.lines

    def summary(self) -> dict:
        return {
            "cart_id": self.cart_id,
            "state": self.state.value,
            "lines": [l.to_dict() for l in self.lines],
            "subtotal": self.subtotal,
        }

    def mark_read_back(self) -> None:
        if self.is_empty():
            raise CartError("cannot read back an empty cart")
        if self.state not in (CartState.BUILDING, CartState.READ_BACK):
            raise CartError(f"cannot read back a cart in state {self.state.value}")
        self.state = CartState.READ_BACK

    def mark_confirmed(self) -> None:
        if self.state != CartState.READ_BACK:
            raise CartError("order must be read back before it can be confirmed")
        self.state = CartState.CONFIRMED

    def mark_submitted(self, order: dict) -> None:
        self.state = CartState.SUBMITTED
        self.last_order = order

    def mark_transferred(self, reason: str) -> None:
        self.transferred = True
        self.transfer_reason = reason

    # -- persistence ----------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "cart_id": self.cart_id,
            "restaurant_id": self.restaurant_id,
            "idempotency_key": self.idempotency_key,
            "lines": [l.to_dict() for l in self.lines],
            "state": self.state.value,
            "customer_name": self.customer_name,
            "customer_phone": self.customer_phone,
            "last_order": self.last_order,
            "transferred": self.transferred,
            "transfer_reason": self.transfer_reason,
            "line_seq": self._line_seq,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Cart":
        cart = cls(
            cart_id=raw["cart_id"],
            restaurant_id=raw["restaurant_id"],
            idempotency_key=raw["idempotency_key"],
        )
        cart.lines = [CartLine.from_dict(l) for l in raw.get("lines", [])]
        cart.state = CartState(raw.get("state", "empty"))
        cart.customer_name = raw.get("customer_name")
        cart.customer_phone = raw.get("customer_phone")
        cart.last_order = raw.get("last_order")
        cart.transferred = bool(raw.get("transferred", False))
        cart.transfer_reason = raw.get("transfer_reason")
        cart._line_seq = int(raw.get("line_seq", len(cart.lines)))
        return cart
