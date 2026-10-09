"""The six order tools. The LLM only talks and calls these; the server owns
the cart, the prices, and the final order.
"""
from __future__ import annotations

from .cart import Cart, CartError, CartState
from .catalog import Catalog, Item
from .matching import search_catalog
from .ports import (
    PaymentStep,
    PosAdapter,
    PosError,
    RestaurantContext,
    ToolResult,
    Totals,
)

TOOL_NAMES = [
    "search_menu",
    "add_item",
    "update_item",
    "remove_item",
    "get_cart",
    "submit_order",
    "transfer_call",
]


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------

def _resolve_selection(
    item: Item,
    variation_id: str | None,
    modifier_ids: list[str],
) -> tuple[str | None, str | None, list[str], list[str], float, list[str]]:
    """Validate refs against the catalog. Returns
    (variation_id, variation_name, modifier_ids, modifier_names, unit_price, errors).
    """
    errors: list[str] = []
    variation_name: str | None = None
    if variation_id:
        variation = item.variation(variation_id)
        if variation is None:
            errors.append(f"'{variation_id}' is not a size for {item.name}")
        else:
            variation_name = variation.name
            variation_id = variation.id
    else:
        variation = None

    modifier_names: list[str] = []
    chosen: list[str] = []
    for mid in modifier_ids:
        modifier = item.modifier(mid)
        if modifier is None:
            errors.append(f"'{mid}' is not an option for {item.name}")
        else:
            chosen.append(modifier.id)
            modifier_names.append(modifier.name)

    chosen_groups = {
        item.group_for_modifier(mid).id  # type: ignore[union-attr]
        for mid in chosen
    }
    for group in item.modifier_groups:
        if group.required and group.id not in chosen_groups:
            options = ", ".join(o.name for o in group.options)
            errors.append(f"{item.name} needs a choice for {group.name}: {options}")

    unit_price = item.base_price
    if variation is not None:
        unit_price += variation.price_delta
    for mid in chosen:
        modifier = item.modifier(mid)
        if modifier is not None:
            unit_price += modifier.price_delta
    return variation_id, variation_name, chosen, modifier_names, round(unit_price, 2), errors


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _read_back_text(cart: Cart, totals: Totals) -> str:
    lines = ", ".join(line.describe() for line in cart.lines)
    return (
        f"Here's what I have: {lines}. "
        f"Your total comes to {_money(totals.total)}. "
        "Should I send this order to the kitchen?"
    )


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def search_menu(catalog: Catalog, query: str, limit: int = 5) -> ToolResult:
    hits = search_catalog(catalog, query, limit=limit)
    if not hits:
        return ToolResult(
            ok=False,
            message=(
                "I'm not finding that on the menu. "
                "Want me to connect you to someone at the restaurant?"
            ),
            data={"matches": [], "query": query},
            error_code="no_match",
        )
    names = ", ".join(f"{h['name']} ({_money(h['price'])})" for h in hits[:3])
    return ToolResult(
        ok=True,
        message=f"I found: {names}.",
        data={"matches": hits, "query": query},
    )


def add_item(
    cart: Cart,
    catalog: Catalog,
    pos: PosAdapter,
    ctx: RestaurantContext,
    item_ref: str,
    quantity: int = 1,
    variation_id: str | None = None,
    modifier_ids: list[str] | None = None,
    note: str | None = None,
) -> ToolResult:
    item = catalog.get(item_ref)
    if item is None:
        return ToolResult(
            ok=False,
            message="I'm not finding that item. Could you say that again?",
            data={"item_ref": item_ref},
            error_code="unknown_item",
        )
    if quantity < 1 or quantity > ctx.max_quantity_per_line:
        return ToolResult(
            ok=False,
            message=f"I can add between 1 and {ctx.max_quantity_per_line} of one item.",
            data={"item_ref": item_ref},
            error_code="bad_quantity",
        )
    try:
        if not pos.is_available(item_ref):
            return ToolResult(
                ok=False,
                message=f"Sorry, the {item.name} is sold out today. Can I suggest something else?",
                data={"item_ref": item_ref},
                error_code="sold_out",
            )
    except PosError:
        return ToolResult(
            ok=False,
            message="One moment, I'm having trouble reaching the restaurant's system.",
            data={"item_ref": item_ref},
            error_code="pos_unavailable",
        )

    variation_id, variation_name, chosen, modifier_names, unit_price, errors = (
        _resolve_selection(item, variation_id, modifier_ids or [])
    )
    if errors:
        return ToolResult(
            ok=False,
            message=" ".join(errors),
            data={"item_ref": item_ref},
            error_code="invalid_selection",
        )
    if len(cart.lines) >= ctx.max_lines:
        return ToolResult(
            ok=False,
            message="That's a big order -- let me connect you to the restaurant directly.",
            data={"item_ref": item_ref},
            error_code="order_too_large",
        )
    try:
        line = cart.add_line(
            item_ref=item.ref,
            item_name=item.name,
            quantity=quantity,
            unit_price=unit_price,
            variation_id=variation_id,
            variation_name=variation_name,
            modifier_ids=chosen,
            modifier_names=modifier_names,
            note=note,
        )
    except CartError as exc:
        return ToolResult(
            ok=False, message=str(exc), data={}, error_code="cart_locked"
        )
    return ToolResult(
        ok=True,
        message=f"Added {line.describe()}.",
        data={"line_id": line.line_id, "cart": cart.summary()},
    )


def update_item(
    cart: Cart,
    catalog: Catalog,
    ctx: RestaurantContext,
    line_id: str,
    quantity: int | None = None,
    item_ref: str | None = None,
    variation_id: str | None = None,
    modifier_ids: list[str] | None = None,
    note: str | None = None,
) -> ToolResult:
    line = cart.find_line(line_id)
    if line is None:
        return ToolResult(
            ok=False,
            message="I don't see that item in the order. Could you say that again?",
            data={"line_id": line_id},
            error_code="unknown_line",
        )
    if quantity is not None and (quantity < 1 or quantity > ctx.max_quantity_per_line):
        return ToolResult(
            ok=False,
            message=f"Quantity has to be between 1 and {ctx.max_quantity_per_line}.",
            data={"line_id": line_id},
            error_code="bad_quantity",
        )

    changes: dict = {}
    target_item = catalog.get(item_ref) if item_ref else catalog.get(line.item_ref)
    if item_ref and target_item is None:
        return ToolResult(
            ok=False,
            message="I'm not finding that item on the menu.",
            data={"line_id": line_id},
            error_code="unknown_item",
        )
    assert target_item is not None
    if item_ref:
        # Swap the item on this line ("make the second one steak").
        variation_id = variation_id if variation_id is not None else None
        modifier_ids = modifier_ids if modifier_ids is not None else []
        (_, variation_name, chosen, modifier_names, unit_price, errors) = (
            _resolve_selection(target_item, variation_id, modifier_ids)
        )
        if errors:
            return ToolResult(
                ok=False,
                message=" ".join(errors),
                data={"line_id": line_id},
                error_code="invalid_selection",
            )
        changes.update(
            {
                "item_ref": target_item.ref,
                "item_name": target_item.name,
                "variation_id": variation_id,
                "variation_name": variation_name,
                "modifier_ids": chosen,
                "modifier_names": modifier_names,
                "unit_price": unit_price,
            }
        )
    elif variation_id is not None or modifier_ids is not None:
        (_, variation_name, chosen, modifier_names, unit_price, errors) = (
            _resolve_selection(
                target_item,
                variation_id if variation_id is not None else line.variation_id,
                modifier_ids if modifier_ids is not None else line.modifier_ids,
            )
        )
        if errors:
            return ToolResult(
                ok=False,
                message=" ".join(errors),
                data={"line_id": line_id},
                error_code="invalid_selection",
            )
        changes.update(
            {
                "variation_id": variation_id if variation_id is not None else line.variation_id,
                "variation_name": variation_name,
                "modifier_ids": chosen,
                "modifier_names": modifier_names,
                "unit_price": unit_price,
            }
        )
    if quantity is not None:
        changes["quantity"] = quantity
    if note is not None:
        changes["note"] = note
    try:
        updated = cart.update_line(line_id, **changes)
    except CartError as exc:
        return ToolResult(ok=False, message=str(exc), data={}, error_code="cart_locked")
    return ToolResult(
        ok=True,
        message=f"Updated: {updated.describe()}.",
        data={"line_id": line_id, "cart": cart.summary()},
    )


def remove_item(cart: Cart, line_id: str) -> ToolResult:
    line = cart.find_line(line_id)
    if line is None:
        return ToolResult(
            ok=False,
            message="I don't see that item in the order.",
            data={"line_id": line_id},
            error_code="unknown_line",
        )
    try:
        removed = cart.remove_line(line_id)
    except CartError as exc:
        return ToolResult(ok=False, message=str(exc), data={}, error_code="cart_locked")
    return ToolResult(
        ok=True,
        message=f"Removed {removed.describe()}.",
        data={"line_id": line_id, "cart": cart.summary()},
    )


def get_cart(cart: Cart, pos: PosAdapter) -> ToolResult:
    if cart.is_empty():
        return ToolResult(
            ok=False,
            message="Your order is empty so far. What can I get you?",
            data={"cart": cart.summary()},
            error_code="empty_cart",
        )
    try:
        totals = pos.quote(cart)
    except PosError:
        return ToolResult(
            ok=False,
            message="One moment, I'm having trouble reaching the restaurant's system.",
            data={"cart": cart.summary()},
            error_code="pos_unavailable",
        )
    if totals.total > 0 and totals.total > 10_000:
        pass  # totals sanity hook for Phase 4 alerting
    try:
        cart.mark_read_back()
    except CartError as exc:
        return ToolResult(ok=False, message=str(exc), data={}, error_code="cart_locked")
    data = {
        "cart": cart.summary(),
        "totals": {
            "subtotal": totals.subtotal,
            "tax": totals.tax,
            "total": totals.total,
            "currency": totals.currency,
        },
    }
    return ToolResult(ok=True, message=_read_back_text(cart, totals), data=data)


def submit_order(
    cart: Cart,
    pos: PosAdapter,
    ctx: RestaurantContext,
    customer_name: str | None,
    customer_phone: str | None,
    confirmed: bool,
    idempotency_key: str,
) -> ToolResult:
    # Idempotent retry: a retried webhook with the same key never makes two orders.
    if cart.transferred:
        return ToolResult(
            ok=False,
            message="This call was handed to the restaurant staff.",
            data={},
            error_code="transferred",
        )
    if cart.state == CartState.SUBMITTED and cart.last_order is not None:
        return ToolResult(
            ok=True,
            message="That order already went through.",
            data={"order": cart.last_order},
        )
    if cart.is_empty():
        return ToolResult(
            ok=False,
            message="There's nothing in the order to send.",
            data={},
            error_code="empty_cart",
        )
    if cart.state != CartState.READ_BACK:
        return ToolResult(
            ok=False,
            message="Let me read the order back first before I send it.",
            data={"state": cart.state.value},
            error_code="read_back_required",
        )
    if not confirmed:
        return ToolResult(
            ok=False,
            message="I need a yes from you before I send the order.",
            data={},
            error_code="not_confirmed",
        )
    if not customer_name or not customer_phone:
        return ToolResult(
            ok=False,
            message="Can I get a name and phone number for the pickup?",
            data={},
            error_code="missing_customer",
        )
    cart.customer_name = customer_name
    cart.customer_phone = customer_phone
    try:
        order = pos.submit(cart, idempotency_key)
    except PosError:
        # Safe landing: the app parks the order for staff (saved + alert) instead
        # of letting it vanish; the caller is told it is NOT confirmed yet.
        return ToolResult(
            ok=False,
            message=(
                "I couldn't get your order into the restaurant's system just now, so I've "
                "passed it to the staff. They'll call you back at this number to confirm it."
            ),
            data={"cart": cart.summary(), "backup": True},
            error_code="pos_unavailable",
        )
    try:
        payment = pos.payment_step(order)
    except PosError:
        # The order is in the POS; only the payment link failed. Don't 500
        # the call over it -- staff can take payment at pickup.
        payment = PaymentStep(
            kind="pickup",
            instructions="pay at pickup",
            url=None,
        )
    cart.mark_confirmed()
    order_data = {
        "order_id": order.order_id,
        "order_number": order.order_number,
        "status": order.status,
        "pickup_time": order.pickup_time,
        "totals": {
            "subtotal": order.totals.subtotal,
            "tax": order.totals.tax,
            "total": order.totals.total,
            "currency": order.totals.currency,
        },
        "payment": {
            "kind": payment.kind,
            "instructions": payment.instructions,
            "url": payment.url,
        },
    }
    cart.mark_submitted(order_data)
    if payment.kind == "link":
        spoken = (
            f"Order {order.order_number} is in! It'll be ready around "
            f"{order.pickup_time}. I'm texting you a payment link to complete it."
        )
    else:
        spoken = (
            f"Order {order.order_number} is in! It'll be ready around "
            f"{order.pickup_time}. You can pay when you pick up."
        )
    return ToolResult(ok=True, message=spoken, data={"order": order_data})


def transfer_call(cart: Cart, ctx: RestaurantContext, reason: str) -> ToolResult:
    """Hand the call to restaurant staff. The cart is frozen and a summary
    goes to the staff phone (texting stubbed until Phase 5)."""
    if cart.transferred:
        return ToolResult(
            ok=False, message="Already connecting you.", data={}, error_code="already_transferred"
        )
    cart.mark_transferred(reason or "caller request")
    summary = "; ".join(line.describe() for line in cart.lines) or "empty order"
    return ToolResult(
        ok=True,
        message="Sure, connecting you to the restaurant now. They'll have your order so far.",
        data={
            "transferred": True,
            "reason": cart.transfer_reason,
            "transfer_number": ctx.transfer_number,
            "cart_summary": summary,
        },
    )


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def dispatch(
    name: str,
    *,
    cart: Cart,
    catalog: Catalog,
    pos: PosAdapter,
    ctx: RestaurantContext,
    arguments: dict,
) -> ToolResult:
    args = dict(arguments or {})
    if name == "search_menu":
        return search_menu(catalog, str(args.get("query", "")), int(args.get("limit", 5)))
    if name == "add_item":
        return add_item(
            cart, catalog, pos, ctx,
            item_ref=str(args.get("item_ref", "")),
            quantity=int(args.get("quantity", 1)),
            variation_id=args.get("variation_id"),
            modifier_ids=list(args.get("modifier_ids", []) or []),
            note=args.get("note"),
        )
    if name == "update_item":
        return update_item(
            cart, catalog, ctx,
            line_id=str(args.get("line_id", "")),
            quantity=args.get("quantity"),
            item_ref=args.get("item_ref"),
            variation_id=args.get("variation_id"),
            modifier_ids=args.get("modifier_ids"),
            note=args.get("note"),
        )
    if name == "remove_item":
        return remove_item(cart, str(args.get("line_id", "")))
    if name == "get_cart":
        return get_cart(cart, pos)
    if name == "transfer_call":
        return transfer_call(cart, ctx, str(args.get("reason", "caller request")))
    if name == "submit_order":
        return submit_order(
            cart, pos, ctx,
            customer_name=args.get("customer_name"),
            customer_phone=args.get("customer_phone"),
            confirmed=bool(args.get("confirmed", False)),
            idempotency_key=str(args.get("idempotency_key") or cart.idempotency_key),
        )
    return ToolResult(
        ok=False,
        message="I didn't catch that.",
        data={"tool": name},
        error_code="unknown_tool",
    )
