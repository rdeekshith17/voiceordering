"""Real Square adapter: the order engine talks to Square's REST API.

Stdlib only (urllib) -- no Square SDK to install, and core/ stays vendor-free
by construction. The app owns the menu (local catalog, or the synced Square
catalog); Square is the order and payment backend.

Order flow: the order is created via the Orders API in OPEN state, so staff
see it in the Square dashboard immediately and the kitchen can start it.
Payment is collected through the Invoices API: a draft invoice is created for
the order and published, which yields a Square-hosted payment page URL that
the caller opens to pay by card.

Credentials arrive at runtime, never in code:
  SQUARE_ACCESS_TOKEN   Square access token (sandbox or production)
  SQUARE_LOCATION_ID    the restaurant's location id
  SQUARE_ENVIRONMENT    "sandbox" (default) or "production"

Set POS_PROFILE=square to use this adapter; square_like/clover_like/toast_like
remain fakes for development and evals.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta

from .. import net
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

log = logging.getLogger("voiceorder.pos.square")

SQUARE_VERSION = "2024-12-18"
_BASE_URLS = {
    "sandbox": "https://connect.squareupsandbox.com/v2",
    "production": "https://connect.squareup.com/v2",
}


def _cents(dollars: float) -> int:
    return int(round(dollars * 100))


class SquarePosAdapter:
    """Square Orders API + Invoices API, over plain HTTPS.

    Orders land in Square as OPEN (visible in the dashboard right away);
    the payment URL is a published invoice's Square-hosted payment page.
    """

    capabilities = PosCapabilities(
        unpaid_orders_visible=True, payment_links=True, pay_at_pickup=False
    )

    def __init__(
        self,
        access_token: str,
        location_id: str,
        *,
        environment: str = "sandbox",
        catalog: Catalog,
        tax_rate: float = 0.0825,
        pickup_minutes: int = 20,
        timezone: str = "",
    ) -> None:
        if not access_token:
            raise ValueError("SquarePosAdapter needs a SQUARE_ACCESS_TOKEN")
        if not location_id:
            raise ValueError("SquarePosAdapter needs a SQUARE_LOCATION_ID")
        if environment not in _BASE_URLS:
            raise ValueError(f"unknown Square environment: {environment}")
        self._token = access_token
        self._location_id = location_id
        self._base = _BASE_URLS[environment]
        self._catalog = catalog
        self._tax_rate = tax_rate
        self._pickup_minutes = pickup_minutes
        self._timezone = timezone  # restaurant's zone for spoken pickup times
        self._orders: dict[str, PosOrder] = {}  # idempotency_key -> order
        self._payment_links: dict[str, str] = {}  # square order id -> pay URL
        # Invoices need the Square account to be enabled for card
        # processing. If Square says it isn't, stop trying for a while
        # instead of burning API calls on every order; pay-at-pickup covers
        # the gap until the merchant finishes payments onboarding.
        self._invoice_disabled_until: float = 0.0

    # -- PosAdapter ---------------------------------------------------------
    def ping(self) -> dict:
        """Read-only connectivity check: fetch the configured location."""
        # NOTE: _base already ends in /v2, so paths here omit it.
        loc = self._request("GET", f"/locations/{self._location_id}")
        location = loc.get("location") or {}
        return {
            "ok": True,
            "location_name": location.get("name", ""),
            "status": location.get("status", ""),
        }

    def sync_catalog(self, restaurant: RestaurantContext) -> Catalog:
        """The app owns the menu; Square receives ad-hoc line items per order.

        A future pass can pull the Square catalog here and diff it against the
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
        # Local idempotency first: a retried call with the same key returns
        # the same order without touching the network. Square also honors the
        # idempotency key on its side for 24h.
        if idempotency_key in self._orders:
            return self._orders[idempotency_key]
        for line in cart.lines:
            if not self.is_available(line.item_ref):
                raise PosError(
                    f"item {line.item_ref} is sold out; the order cannot fire"
                )
        square_order = self._create_order(cart, idempotency_key)
        totals = self._totals_from_square(square_order) or self.quote(cart)
        order_id = str(square_order["id"])
        pickup = (local_now(self._timezone) + timedelta(minutes=self._pickup_minutes)).strftime(
            "%-I:%M %p"
        )
        order = PosOrder(
            order_id=order_id,
            # Square API orders have no counter number; the pickup code is the
            # tail of the Square order id -- staff read it off the dashboard.
            order_number=order_id.replace("-", "")[-6:].upper(),
            status="pending_payment",
            pickup_time=pickup,
            totals=totals,
        )
        self._orders[idempotency_key] = order
        # The invoice (payment URL) is best-effort: the order itself is
        # already OPEN and visible to staff. If invoicing fails, payment_step
        # falls back to pay-at-pickup instead of failing the order.
        if time.time() >= self._invoice_disabled_until:
            try:
                pay_url = self._create_invoice_link(
                    square_order, cart, idempotency_key
                )
            except PosError as exc:
                log.warning("Square invoice for order %s failed: %s", order_id, exc)
                if "not been enabled to take payments" in str(exc):
                    # Account-level: retry the probe in 15 minutes in case
                    # the merchant finishes payments onboarding.
                    self._invoice_disabled_until = time.time() + 15 * 60
                    log.warning(
                        "Square card processing is not enabled for this "
                        "account -- payment links are off until it is. "
                        "Enable it in the Square dashboard; orders still "
                        "land as OPEN for pay-at-pickup."
                    )
            else:
                self._payment_links[order_id] = pay_url
        return order

    def payment_step(self, order: PosOrder) -> PaymentStep:
        url = self._payment_links.get(order.order_id)
        if url is None:
            # The invoice URL is minted at submit time and kept in-process.
            # A recreated adapter cannot rebuild it (it would need the
            # invoice id); see the saved order record for the original link.
            raise PosError(
                "payment link is not available in this session; "
                "see the saved order record for the original link"
            )
        return PaymentStep(
            kind="link",
            instructions=(
                "I'll text you a secure Square payment link to complete your order."
            ),
            url=url,
        )

    # -- Square REST ----------------------------------------------------------
    def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self._base + path,
            data=data,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Square-Version": SQUARE_VERSION,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with net.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise PosError(f"Square rejected the request: {self._square_detail(exc)}") from exc
        except OSError as exc:
            raise PosError(f"Square unreachable: {exc}") from exc

    @staticmethod
    def _square_detail(exc: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read()[:2000] or b"{}")
            errors = payload.get("errors", [])
            if errors:
                first = errors[0]
                return f"HTTP {exc.code} {first.get('code')}: {first.get('detail')}"
        except (ValueError, OSError):
            pass
        return f"HTTP {exc.code}"

    def _line_item(self, line) -> dict:
        name = line.item_name
        if line.variation_name:
            name += f" ({line.variation_name})"
        note_bits = []
        if line.modifier_names:
            note_bits.append("with " + ", ".join(line.modifier_names))
        if line.note:
            note_bits.append(line.note)
        item: dict = {
            "name": name,
            "quantity": str(line.quantity),
            "base_price_money": {
                "amount": _cents(line.unit_price),
                "currency": self._catalog.currency,
            },
        }
        if note_bits:
            item["note"] = "; ".join(note_bits)
        return item

    def _order_body(self, cart: Cart) -> dict:
        """Inline order payload for the payment-link call.

        The app owns the menu, so line items are ad-hoc (name/price), not
        catalog objects. The Payment Links API creates the Square order
        itself from this definition.
        """
        pickup_at = (
            datetime.now() + timedelta(minutes=self._pickup_minutes)
        ).isoformat()
        order_body: dict = {
            "location_id": self._location_id,
            "line_items": [self._line_item(l) for l in cart.lines],
            # Explicit sales tax: Square does not infer it, and an untaxed
            # order would undercharge the customer at the payment link.
            "taxes": [
                {
                    "uid": "sales-tax",
                    "name": "Sales Tax",
                    "percentage": f"{self._tax_rate * 100:.4f}".rstrip("0").rstrip("."),
                    "scope": "ORDER",
                    "type": "ADDITIVE",
                }
            ],
            "fulfillments": [
                {
                    "type": "PICKUP",
                    "state": "PROPOSED",
                    "pickup_details": {
                        "schedule_type": "SCHEDULED",
                        "pickup_at": pickup_at,
                        "note": "VoiceOrderAI phone order",
                    },
                }
            ],
        }
        if cart.customer_name:
            order_body["fulfillments"][0]["pickup_details"]["recipient"] = {
                "display_name": cart.customer_name
            }
        return order_body

    def _create_order(self, cart: Cart, idempotency_key: str) -> dict:
        """Create the order via the Orders API.

        Orders created this way land in OPEN state, so staff see them in
        the Square dashboard immediately and the kitchen can start them.
        """
        body = {
            "idempotency_key": idempotency_key,
            "order": self._order_body(cart),
        }
        resp = self._request("POST", "/orders", body)
        order = resp.get("order") or {}
        if not order.get("id"):
            raise PosError("Square returned no order id")
        return order

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits.startswith("1"):
            return "+" + digits
        return ("+" + digits) if digits else ""

    def _find_or_create_customer(self, name: str, phone: str) -> str:
        """Square invoices need a customer_id on the recipient."""
        phone = self._normalize_phone(phone)
        if phone:
            resp = self._request(
                "POST",
                "/customers/search",
                {"query": {"filter": {"phone_number": {"exact": phone}}}},
            )
            matches = resp.get("customers") or []
            if matches and matches[0].get("id"):
                return str(matches[0]["id"])
        body: dict = {
            "idempotency_key": uuid.uuid4().hex,
            "given_name": name or "Guest",
        }
        if phone:
            body["phone_number"] = phone
        resp = self._request("POST", "/customers", body)
        customer = resp.get("customer") or {}
        if not customer.get("id"):
            raise PosError("Square returned no customer id")
        return str(customer["id"])

    def _create_invoice_link(
        self, square_order: dict, cart: Cart, idempotency_key: str
    ) -> str:
        """Create + publish an invoice for the order; return its payment URL.

        The invoice is SHARE_MANUALLY: Square does not email or charge
        anything on its own -- it just hosts the payment page, whose URL we
        text to the caller.
        """
        customer_id = self._find_or_create_customer(
            (cart.customer_name or "").strip(), (cart.customer_phone or "").strip()
        )
        due_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        body = {
            "idempotency_key": f"inv-{idempotency_key}",
            "invoice": {
                "order_id": square_order["id"],
                "primary_recipient": {"customer_id": customer_id},
                "payment_requests": [
                    {"request_type": "BALANCE", "due_date": due_date}
                ],
                "delivery_method": "SHARE_MANUALLY",
                "accepted_payment_methods": {"card": True},
                "title": "Phone order",
            },
        }
        resp = self._request("POST", "/invoices", body)
        invoice = resp.get("invoice") or {}
        invoice_id = invoice.get("id")
        version = invoice.get("version")
        if not invoice_id:
            raise PosError("Square returned no invoice id")
        pub = self._request(
            "POST", f"/invoices/{invoice_id}/publish", {"version": version}
        )
        url = (pub.get("invoice") or {}).get("public_url")
        if not url:
            raise PosError("Square returned no invoice payment URL")
        return str(url)

    def _totals_from_square(self, square_order: dict) -> Totals | None:
        total_money = square_order.get("total_money") or {}
        amount = total_money.get("amount")
        if amount is None:
            return None
        total = amount / 100
        tax_money = square_order.get("total_tax_money") or {}
        tax = (tax_money.get("amount") or 0) / 100
        return Totals(
            subtotal=round(total - tax, 2),
            tax=round(tax, 2),
            total=round(total, 2),
            currency=total_money.get("currency", self._catalog.currency),
        )
