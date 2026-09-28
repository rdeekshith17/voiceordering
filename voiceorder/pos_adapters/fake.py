from __future__ import annotations

from dataclasses import dataclass, field

from ..core.cart import Cart
from ..core.catalog import Catalog
from ..core.ports import PaymentStep, PosCapabilities, PosError, PosOrder, Totals

PROFILES: dict[str, PosCapabilities] = {
    "square_like": PosCapabilities(
        unpaid_orders_visible=False,
        payment_link=True,
    ),
    "clover_like": PosCapabilities(
        unpaid_orders_visible=True,
        payment_link=False,
        requires_linked_modifiers=True,
    ),
    "toast_like": PosCapabilities(
        unpaid_orders_visible=True,
        payment_link=False,
        totals_via_quote_only=True,
    ),
}


class FakePosError(PosError):
    pass


class FakePosTimeout(FakePosError):
    pass


@dataclass
class FakePos:
    """Copies each real POS's rules so the app is exercised before a vendor is wired up."""

    profile: str
    catalog: Catalog
    tax_rate: float = 0.0875
    failure_mode: str | None = None  # "down" or "slow"
    pickup_time_text: str = "20 minutes"
    _orders: dict[str, PosOrder] = field(default_factory=dict, repr=False)

    def mark_sold_out(self, item_id: str) -> None:
        """Simulates the POS's own stock running out since the last catalog sync."""
        item = self.catalog.item(item_id)
        if item is not None:
            item.available = False

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"unknown fake POS profile: {self.profile}")
        self.capabilities = PROFILES[self.profile]

    def sync_catalog(self, restaurant_id: str) -> Catalog:
        return self.catalog

    def quote(self, cart: Cart) -> Totals:
        subtotal = sum(line.line_total_cents for line in cart.lines)
        tax = round(subtotal * self.tax_rate)
        return Totals(subtotal_cents=subtotal, tax_cents=tax, total_cents=subtotal + tax)

    def submit(self, cart: Cart, idempotency_key: str) -> PosOrder:
        if self.failure_mode == "down":
            raise FakePosError("POS is unreachable")
        if self.failure_mode == "slow":
            raise FakePosTimeout("POS did not respond in time")
        if idempotency_key in self._orders:
            return self._orders[idempotency_key]
        order = PosOrder(
            order_id=f"{self.profile}-{len(self._orders) + 1}",
            pickup_time=self.pickup_time_text,
            raw={},
        )
        self._orders[idempotency_key] = order
        return order

    def payment_step(self, order: PosOrder) -> PaymentStep:
        if self.capabilities.payment_link:
            return PaymentStep(kind="link", detail=f"https://pay.example.test/{order.order_id}")
        return PaymentStep(kind="pay_at_pickup", detail=None)
