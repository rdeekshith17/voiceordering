from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class CallState(str, Enum):
    GREETING = "greeting"
    TAKING_ORDER = "taking_order"
    TRANSFERRED = "transferred"
    SUBMITTED = "submitted"


class CartError(Exception):
    pass


@dataclass
class CartLine:
    line_id: int
    item_id: str
    name: str
    quantity: int
    unit_price_cents: int
    variation_id: str | None = None
    variation_name: str | None = None
    modifier_ids: list[str] = field(default_factory=list)
    modifier_names: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def line_total_cents(self) -> int:
        return self.unit_price_cents * self.quantity

    def describe(self) -> str:
        parts = [f"{self.quantity} x {self.name}"]
        if self.variation_name and self.variation_name != "Regular":
            parts.append(f"({self.variation_name})")
        if self.modifier_names:
            parts.append(f"[{', '.join(self.modifier_names)}]")
        return " ".join(parts)


@dataclass
class Cart:
    call_id: str
    lines: list[CartLine] = field(default_factory=list)
    state: CallState = CallState.GREETING
    needs_readback: bool = False
    customer_name: str | None = None
    customer_phone: str | None = None
    _next_line_id: int = field(default=1, repr=False)

    def add_line(self, **kwargs) -> CartLine:
        line = CartLine(line_id=self._next_line_id, **kwargs)
        self._next_line_id += 1
        self.lines.append(line)
        self.state = CallState.TAKING_ORDER
        self.needs_readback = True
        return line

    def line(self, line_id: int) -> CartLine:
        for existing in self.lines:
            if existing.line_id == line_id:
                return existing
        raise CartError(f"no such line id: {line_id}")

    def update_line(self, line_id: int, **changes) -> CartLine:
        line = self.line(line_id)
        for key, value in changes.items():
            if value is not None:
                setattr(line, key, value)
        self.needs_readback = True
        return line

    def remove_line(self, line_id: int) -> None:
        line = self.line(line_id)
        self.lines.remove(line)
        self.needs_readback = True

    def mark_read_back(self) -> None:
        self.needs_readback = False

    def is_empty(self) -> bool:
        return len(self.lines) == 0
