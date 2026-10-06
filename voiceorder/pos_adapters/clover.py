"""Real Clover adapter: the order engine talks to Clover's REST API.

Stdlib only (urllib) -- no Clover SDK, and core/ stays vendor-free by
construction. The app owns the menu (local catalog); Clover is the order
backend: orders are created open with ad-hoc line items so staff see them
immediately, and the customer pays at pickup -- mirroring the clover_like
fake profile.

Clover specifics:
  - Auth: ``Authorization: Bearer <api_token>`` (merchant API token).
  - Base: https://apisandbox.dev.clover.com (sandbox)
           https://api.clover.com (production)
  - Amounts are integer cents.
  - Order create has no native idempotency key, so the adapter keeps a local
    idempotency_key -> order cache (same as the fake). A process restart
    clears it; Clover-side dedupe for that window is a documented gap.
  - Modifiers must be linked to the item in OUR catalog (the fake's rule);
    they ride along in the line-item note because ad-hoc line items cannot
    reference Clover inventory modifier groups.

Credentials arrive at runtime, never in code:
  CLOVER_ACCESS_TOKEN   merchant API token
  CLOVER_MERCHANT_ID    merchant id
  CLOVER_ENVIRONMENT    "sandbox" (default) or "production"

Set POS_PROFILE=clover to use this adapter.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from .. import net
from ..core.cart import Cart
from ..core.catalog import Catalog
from ..core.ports import (
    PaymentStep,
    PosCapabilities,
    PosError,
    PosOrder,
    RestaurantContext,
    Totals,
)

_BASE_URLS = {
    "sandbox": "https://apisandbox.dev.clover.com",
    "production": "https://api.clover.com",
}


class CloverPosAdapter:
    """Clover Orders API over plain HTTPS."""

    capabilities = PosCapabilities(
        unpaid_orders_visible=True, payment_links=False, pay_at_pickup=True
    )

    def __init__(
        self,
        access_token: str,
        merchant_id: str,
        *,
        environment: str = "sandbox",
        catalog: Catalog,
        tax_rate: float = 0.0825,
        pickup_minutes: int = 20,
    ) -> None:
        if not access_token:
            raise ValueError("CloverPosAdapter needs a CLOVER_ACCESS_TOKEN")
        if not merchant_id:
            raise ValueError("CloverPosAdapter needs a CLOVER_MERCHANT_ID")
        if environment not in _BASE_URLS:
            raise ValueError(f"unknown Clover environment: {environment}")
        self._token = access_token
        self._base = f"{_BASE_URLS[environment]}/v3/merchants/{merchant_id}"
        self._catalog = catalog
        self._tax_rate = tax_rate
        self._pickup_minutes = pickup_minutes
        self._orders: dict[str, PosOrder] = {}  # idempotency_key -> order

    # -- PosAdapter ---------------------------------------------------------
    def ping(self) -> dict:
        """Read-only connectivity check: fetch the merchant record."""
        merchant = self._request("GET", "")
        return {"ok": True, "merchant_name": merchant.get("name", "")}

    def sync_catalog(self, restaurant: RestaurantContext) -> Catalog:
        """The app owns the menu; Clover receives ad-hoc line items per order.

        A future pass can pull Clover inventory here and diff it against the
        local menu -- the seam is this method."""
        return self._catalog

    def is_available(self, item_ref: str) -> bool:
        return self._catalog.is_available(item_ref)

    def quote(self, cart: Cart) -> Totals:
        subtotal = round(cart.subtotal, 2)
        tax = round(subtotal * self._tax_rate, 2)
        return Totals(
            subtotal=subtotal,
            tax=tax,
            total=round(subtotal + tax, 2),
            currency=self._catalog.currency,
        )

    def submit(self, cart: Cart, idempotency_key: str) -> PosOrder:
        if idempotency_key in self._orders:
            return self._orders[idempotency_key]
        for line in cart.lines:
            if not self.is_available(line.item_ref):
                raise PosError(
                    f"item {line.item_ref} is sold out; the order cannot fire"
                )
        self._check_modifiers_linked(cart)
        order_id = self._create_order(cart)
        for line in cart.lines:
            self._add_line_item(order_id, line)
        pickup = (datetime.now() + timedelta(minutes=self._pickup_minutes)).strftime(
            "%-I:%M %p"
        )
        order = PosOrder(
            order_id=order_id,
            # Clover API order ids are opaque; the pickup code is the tail --
            # staff read the full id off the Clover dashboard.
            order_number=order_id.replace("-", "")[-6:].upper(),
            status="received",  # open + visible to staff, unpaid
            pickup_time=pickup,
            totals=self.quote(cart),
        )
        self._orders[idempotency_key] = order
        return order

    def payment_step(self, order: PosOrder) -> PaymentStep:
        return PaymentStep(
            kind="pay_at_pickup",
            instructions="You can pay when you pick up.",
        )

    # -- Clover REST ----------------------------------------------------------
    def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self._base + path,
            data=data,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with net.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise PosError(f"Clover rejected the request: {self._clover_detail(exc)}") from exc
        except OSError as exc:
            raise PosError(f"Clover unreachable: {exc}") from exc

    @staticmethod
    def _clover_detail(exc: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read()[:2000] or b"{}")
            message = payload.get("message") or payload.get("error")
            if message:
                return f"HTTP {exc.code} {message}"
        except (ValueError, OSError):
            pass
        return f"HTTP {exc.code}"

    def _create_order(self, cart: Cart) -> str:
        note_bits = []
        if cart.customer_name:
            note_bits.append(cart.customer_name)
        if cart.customer_phone:
            note_bits.append(cart.customer_phone)
        note_bits.append("VoiceOrderAI phone order")
        resp = self._request(
            "POST",
            "/orders",
            {"state": "open", "note": " - ".join(note_bits)},
        )
        order_id = resp.get("id")
        if not order_id:
            raise PosError("Clover returned no order id")
        return str(order_id)

    def _add_line_item(self, order_id: str, line) -> None:
        name = line.item_name
        if line.variation_name:
            name += f" ({line.variation_name})"
        note_bits = []
        if line.modifier_names:
            note_bits.append("with " + ", ".join(line.modifier_names))
        if line.note:
            note_bits.append(line.note)
        body: dict = {
            "name": name,
            "price": int(round(line.unit_price * 100)),
            "unitQty": line.quantity,
        }
        if note_bits:
            body["note"] = "; ".join(note_bits)
        self._request("POST", f"/orders/{order_id}/line_items", body)

    def _check_modifiers_linked(self, cart: Cart) -> None:
        """Clover would not print an unlinked modifier -- fail fast instead."""
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
