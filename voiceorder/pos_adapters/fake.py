"""Fake POS adapter with per-vendor profiles.

Each profile copies one real POS's rules so the app handles them long before
a real connection exists:

- square_like: unpaid orders are NOT visible to staff; payment via texted link.
  An order only counts as received once paid.
- clover_like: unpaid orders ARE visible; pay at pickup. Modifiers must be
  linked to the item or the order won't print.
- toast_like: unpaid orders ARE visible; pay at pickup. Totals only come from
  a price quote call.

Failure modes (for Phase 4 hardening): ok | slow | down | sold_out | flaky.
flaky fails exactly one call, then stays healthy, so retry logic is exercised
deterministically.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path

from ..core.cart import Cart
from ..core.catalog import Catalog
from ..core.ports import (
    local_now,
    PaymentStep,
    PosCapabilities,
    PosError,
    PosOrder,
    RestaurantContext,
    Totals,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "menu_taqueria.json"

PROFILES: dict[str, PosCapabilities] = {
    "square_like": PosCapabilities(
        unpaid_orders_visible=False, payment_links=True, pay_at_pickup=False
    ),
    "clover_like": PosCapabilities(
        unpaid_orders_visible=True, payment_links=False, pay_at_pickup=True
    ),
    "toast_like": PosCapabilities(
        unpaid_orders_visible=True, payment_links=False, pay_at_pickup=True
    ),
}


class FakePos:
    """A POS adapter that behaves like Square / Clover / Toast without the SDK."""

    def __init__(
        self,
        profile: str = "square_like",
        catalog: Catalog | None = None,
        mode: str = "ok",
        tax_rate: float = 0.0825,
        timezone: str = "",
        sold_out: tuple[str, ...] = (),
    ):
        if profile not in PROFILES:
            raise ValueError(f"unknown POS profile: {profile}")
        if mode not in ("ok", "slow", "down", "sold_out", "flaky"):
            raise ValueError(f"unknown failure mode: {mode}")
        self.profile = profile
        self.mode = mode
        self.tax_rate = tax_rate
        self._timezone = timezone  # restaurant's zone for spoken pickup times
        self._catalog = catalog or Catalog.from_json(FIXTURE)
        self._sold_out = set(sold_out)
        self._orders: dict[str, PosOrder] = {}  # idempotency_key -> order
        self._quoted: set[str] = set()  # cart ids with a fresh price quote
        self._seq = 1041
        self._flaky_calls = 0

    @property
    def capabilities(self) -> PosCapabilities:
        return PROFILES[self.profile]

    # -- PosAdapter ---------------------------------------------------------
    def sync_catalog(self, restaurant: RestaurantContext) -> Catalog:
        return self._catalog

    def _line_available(self, item_ref: str) -> bool:
        """Pure availability check (no failure injection)."""
        if self.mode == "sold_out" or item_ref in self._sold_out:
            return False
        return self._catalog.is_available(item_ref)

    def is_available(self, item_ref: str) -> bool:
        self._maybe_fail("availability check")
        return self._line_available(item_ref)

    def quote(self, cart: Cart) -> Totals:
        self._maybe_fail("price quote")
        subtotal = round(cart.subtotal, 2)
        tax = round(subtotal * self.tax_rate, 2)
        self._quoted.add(cart.cart_id)
        return Totals(
            subtotal=subtotal,
            tax=tax,
            total=round(subtotal + tax, 2),
            currency=self._catalog.currency,
        )

    def submit(self, cart: Cart, idempotency_key: str) -> PosOrder:
        # Idempotency: a retried call with the same key returns the same order.
        if idempotency_key in self._orders:
            return self._orders[idempotency_key]
        self._maybe_fail("order submit")
        for line in cart.lines:
            if not self._line_available(line.item_ref):
                raise PosError(
                    f"item {line.item_ref} is sold out; the order cannot fire"
                )
        if self.profile == "toast_like" and cart.cart_id not in self._quoted:
            raise PosError("toast requires a price quote before an order can post")
        if self.profile == "clover_like":
            self._check_modifiers_linked(cart)
        totals = self.quote(cart)
        self._seq += 1
        pickup = (local_now(self._timezone) + timedelta(minutes=20)).strftime("%-I:%M %p")
        status = (
            "pending_payment"
            if self.profile == "square_like"
            else "received"
        )
        order = PosOrder(
            order_id=f"ord_{self._seq}",
            order_number=str(self._seq),
            status=status,
            pickup_time=pickup,
            totals=totals,
        )
        self._orders[idempotency_key] = order
        return order

    def payment_step(self, order: PosOrder) -> PaymentStep:
        if self.profile == "square_like":
            return PaymentStep(
                kind="link",
                instructions="I'll text you a secure payment link to complete your order.",
                url=f"https://pay.fake/checkout/{order.order_id}",
            )
        return PaymentStep(
            kind="pay_at_pickup",
            instructions="You can pay when you pick up.",
        )

    # -- helpers -------------------------------------------------------------
    def _maybe_fail(self, operation: str) -> None:
        if self.mode == "slow":
            time.sleep(0.2)
        elif self.mode == "down":
            raise PosError(f"fake POS is down during {operation}")
        elif self.mode == "flaky":
            # Fails exactly once (the first injected call), then stays healthy:
            # deterministic retry testing.
            self._flaky_calls += 1
            if self._flaky_calls == 1:
                raise PosError(f"fake POS flaked during {operation} (first call)")

    def _check_modifiers_linked(self, cart: Cart) -> None:
        for line in cart.lines:
            item = self._catalog.get(line.item_ref)
            if item is None:
                raise PosError(f"unknown item {line.item_ref}")
            valid = {m.id for g in item.modifier_groups for m in g.options}
            for mid in line.modifier_ids:
                if mid not in valid:
                    raise PosError(
                        f"modifier {mid} is not linked to {item.ref}; "
                        "Clover would not print it"
                    )

    def mark_sold_out(self, *refs: str) -> None:
        self._sold_out.update(refs)
