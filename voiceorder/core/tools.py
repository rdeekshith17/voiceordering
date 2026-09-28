from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from .backup import BackupStore
from .cart import CallState, Cart, CartError
from .catalog import Catalog, Item
from .matching import search_menu as fuzzy_search
from .ports import PosAdapter, PosError

logger = logging.getLogger("voiceorder")

SEARCH_MISSES_BEFORE_TRANSFER = 2
POS_SUBMIT_ATTEMPTS = 2


class ToolError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass
class OrderTools:
    """The tools the agent can call. Nothing reaches the POS except through here."""

    cart: Cart
    catalog: Catalog
    pos: PosAdapter
    max_quantity_per_line: int = 20
    max_total_cents: int = 50_000
    backup: BackupStore | None = None
    transfer_number: str | None = None

    def search_menu(self, query: str) -> dict:
        matches = fuzzy_search(query, self.catalog)
        self.cart.search_misses = 0 if matches else self.cart.search_misses + 1
        return {
            "suggest_transfer": self.cart.search_misses >= SEARCH_MISSES_BEFORE_TRANSFER,
            "matches": [
                {
                    "ref": m.item.id,
                    "name": m.item.name,
                    "sizes": [
                        {"ref": v.id, "name": v.name, "price_cents": v.price_cents}
                        for v in m.item.variations
                    ],
                    "modifier_groups": [
                        {
                            "ref": g.id,
                            "name": g.name,
                            "required": g.required,
                            "options": [
                                {
                                    "ref": o.id,
                                    "name": o.name,
                                    "price_delta_cents": o.price_delta_cents,
                                }
                                for o in g.options
                            ],
                        }
                        for g in m.item.modifier_groups
                    ],
                }
                for m in matches
            ]
        }

    def add_item(
        self,
        item_ref: str,
        quantity: int = 1,
        variation_ref: str | None = None,
        modifier_refs: list[str] | None = None,
        note: str | None = None,
    ) -> dict:
        item = self._resolve_item(item_ref)
        self._check_quantity(quantity)
        variation = self._resolve_variation(item, variation_ref)
        modifier_names, modifier_price_cents = self._resolve_modifiers(
            item, modifier_refs or []
        )

        line = self.cart.add_line(
            item_id=item.id,
            name=item.name,
            quantity=quantity,
            unit_price_cents=(variation.price_cents if variation else 0)
            + modifier_price_cents,
            variation_id=variation.id if variation else None,
            variation_name=variation.name if variation else None,
            modifier_ids=list(modifier_refs or []),
            modifier_names=modifier_names,
            note=note,
        )
        return {"line_id": line.line_id, "cart": self._cart_summary()}

    def update_item(
        self,
        line_id: int,
        quantity: int | None = None,
        variation_ref: str | None = None,
        modifier_refs: list[str] | None = None,
        note: str | None = None,
    ) -> dict:
        line = self._resolve_line(line_id)
        item = self._resolve_item(line.item_id)

        if quantity is not None:
            self._check_quantity(quantity)

        variation = (
            self._resolve_variation(item, variation_ref)
            if variation_ref is not None
            else item.variation(line.variation_id)
        )

        changes: dict = {}
        if quantity is not None:
            changes["quantity"] = quantity
        if note is not None:
            changes["note"] = note
        if variation_ref is not None:
            changes["variation_id"] = variation.id if variation else None
            changes["variation_name"] = variation.name if variation else None

        if modifier_refs is not None:
            modifier_names, modifier_price_cents = self._resolve_modifiers(
                item, modifier_refs
            )
            changes["modifier_ids"] = list(modifier_refs)
            changes["modifier_names"] = modifier_names
            changes["unit_price_cents"] = (
                variation.price_cents if variation else 0
            ) + modifier_price_cents
        elif variation_ref is not None:
            _, modifier_price_cents = self._resolve_modifiers(
                item, line.modifier_ids
            )
            changes["unit_price_cents"] = (
                variation.price_cents if variation else 0
            ) + modifier_price_cents

        self.cart.update_line(line_id, **changes)
        return {"cart": self._cart_summary()}

    def remove_item(self, line_id: int) -> dict:
        try:
            self.cart.remove_line(line_id)
        except CartError as e:
            raise ToolError(str(e)) from e
        return {"cart": self._cart_summary()}

    def get_cart(self) -> dict:
        self.cart.mark_read_back()
        return self._cart_summary(include_readback_text=True)

    def transfer_call(self, reason: str) -> dict:
        """Caller asked for a person, or search_menu kept missing (section 8)."""
        self.cart.state = CallState.TRANSFERRED
        cart_summary = (
            "; ".join(line.describe() for line in self.cart.lines) or "no items yet"
        )
        logger.warning("call %s transferred: %s", self.cart.call_id, reason)
        if self.backup is not None:
            self.backup.record(
                self.cart.call_id, "transferred", f"{reason} -- cart: {cart_summary}"
            )
        return {
            "transferred": True,
            "reason": reason,
            "cart_summary": cart_summary,
            "transfer_number": self.transfer_number,
        }

    def submit_order(self, customer_name: str, confirmed: bool) -> dict:
        if self.cart.is_empty():
            raise ToolError("cart is empty")
        if self.cart.needs_readback:
            raise ToolError("must read back the order before submitting")
        if not confirmed:
            raise ToolError("caller has not confirmed the order")

        totals = self.pos.quote(self.cart)
        if totals.total_cents > self.max_total_cents:
            raise ToolError(
                "order total exceeds the phone-order limit; transfer to staff"
            )

        self.cart.customer_name = customer_name
        self.cart.state = CallState.SUBMITTED
        idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_URL, self.cart.call_id))

        order = None
        last_error: PosError | None = None
        for attempt in range(1, POS_SUBMIT_ATTEMPTS + 1):
            try:
                order = self.pos.submit(self.cart, idempotency_key)
                break
            except PosError as e:
                last_error = e
                logger.warning(
                    "POS submit attempt %d/%d failed for call %s: %s",
                    attempt,
                    POS_SUBMIT_ATTEMPTS,
                    self.cart.call_id,
                    e,
                )

        if order is None:
            if self.backup is not None:
                self.backup.record(
                    self.cart.call_id,
                    "failed",
                    f"POS submit failed after {POS_SUBMIT_ATTEMPTS} attempts: {last_error}",
                )
            return {
                "status": "pending_confirmation",
                "message": "I've saved your order and the restaurant will confirm it shortly.",
            }

        payment = self.pos.payment_step(order)
        if self.backup is not None:
            self.backup.record(
                self.cart.call_id, "confirmed", f"order {order.order_id} submitted"
            )
            if payment.kind == "link":
                self.backup.record(
                    self.cart.call_id,
                    "unpaid",
                    f"order {order.order_id} awaiting payment via {payment.detail}",
                )

        return {
            "status": "confirmed",
            "order_id": order.order_id,
            "pickup_time": order.pickup_time,
            "payment": {"kind": payment.kind, "detail": payment.detail},
        }

    def _resolve_item(self, item_ref: str) -> Item:
        item = self.catalog.item(item_ref)
        if item is None or not item.available:
            raise ToolError(f"item not found or unavailable: {item_ref}")
        return item

    def _resolve_line(self, line_id: int):
        try:
            return self.cart.line(line_id)
        except CartError as e:
            raise ToolError(str(e)) from e

    def _check_quantity(self, quantity: int) -> None:
        if quantity < 1 or quantity > self.max_quantity_per_line:
            raise ToolError(
                f"quantity must be between 1 and {self.max_quantity_per_line}"
            )

    def _resolve_variation(self, item: Item, variation_ref: str | None):
        if not item.variations:
            return None
        variation = (
            item.variation(variation_ref) if variation_ref else item.variations[0]
        )
        if variation is None:
            raise ToolError(f"unknown size for {item.name}: {variation_ref}")
        return variation

    def _resolve_modifiers(
        self, item: Item, modifier_refs: list[str]
    ) -> tuple[list[str], int]:
        matched_refs: set[str] = set()
        modifier_names: list[str] = []
        price_delta_cents = 0

        for group in item.modifier_groups:
            selected = [r for r in modifier_refs if group.option(r) is not None]
            if group.required and not selected:
                raise ToolError(f"{item.name} requires a choice for {group.name}")
            matched_refs.update(selected)
            for ref in selected:
                option = group.option(ref)
                price_delta_cents += option.price_delta_cents
                modifier_names.append(option.name)

        if self.pos.capabilities.requires_linked_modifiers:
            unmatched = set(modifier_refs) - matched_refs
            if unmatched:
                raise ToolError(
                    f"modifiers must be linked to the item on this POS: {sorted(unmatched)}"
                )

        return modifier_names, price_delta_cents

    def _cart_summary(self, include_readback_text: bool = False) -> dict:
        totals = self.pos.quote(self.cart)
        summary = {
            "lines": [
                {
                    "line_id": line.line_id,
                    "description": line.describe(),
                    "quantity": line.quantity,
                    "line_total_cents": line.line_total_cents,
                }
                for line in self.cart.lines
            ],
            "subtotal_cents": totals.subtotal_cents,
            "tax_cents": totals.tax_cents,
            "total_cents": totals.total_cents,
        }
        if include_readback_text:
            lines_text = "; ".join(line.describe() for line in self.cart.lines)
            summary["readback_text"] = (
                f"Let me read that back: {lines_text or 'nothing yet'}. "
                f"Your total is ${totals.total_cents / 100:.2f}. Does that sound right?"
            )
        return summary
