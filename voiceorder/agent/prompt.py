"""Agent prompt and tool definitions, generated from the restaurant's catalog.

The agent only talks and calls tools; it never invents items, prices, or
modifiers. Everything orderable is in the reference below.
"""
from __future__ import annotations

from ..core.catalog import Catalog
from ..core.ports import RestaurantContext


def build_catalog_reference(catalog: Catalog) -> str:
    lines = ["MENU REFERENCE (ref | name | price | sizes | modifiers; * = required choice)"]
    for item in catalog.all_items():
        parts = [item.ref, item.name, f"${item.base_price:.2f}"]
        if item.variations:
            sizes = ", ".join(
                f"{v.name} (+${v.price_delta:.2f})" if v.price_delta else v.name
                for v in item.variations
            )
            parts.append(f"sizes: {sizes}")
        if item.modifier_groups:
            groups = []
            for group in item.modifier_groups:
                marker = "*" if group.required else ""
                options = ", ".join(
                    f"{o.name} (+${o.price_delta:.2f})" if o.price_delta else o.name
                    for o in group.options
                )
                groups.append(f"{marker}{group.name}: {options}")
            parts.append("modifiers: " + " | ".join(groups))
        if item.aliases:
            parts.append("also called: " + ", ".join(item.aliases[:4]))
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def build_system_prompt(catalog: Catalog, ctx: RestaurantContext) -> str:
    return f"""You are the phone order-taker for {ctx.restaurant_name}, a takeout restaurant.
This is a live phone call. Keep every reply short and speakable -- one or two sentences,
no bullet points, no markdown. The caller already heard that the call may be recorded.

HOW TO TAKE AN ORDER
- Use the search_menu tool when the caller names a dish; always use the returned ref
  (e.g. B2) with add_item. Never offer or add anything not in the MENU REFERENCE.
- Items have line ids (L1, L2...). When the caller says "the steak one" or
  "make the second one chicken", use update_item with that line's id.
- Modifier ids come from search_menu results. Groups marked * are required --
  if the caller doesn't choose, ask them (e.g. "rice and beans, or just rice?").
- Before sending any order: call get_cart, read the order and total back to the
  caller, and only call submit_order after they say yes. submit_order needs the
  caller's name and phone number -- ask if you don't have them.
- If the caller changes anything after a read-back, read the order back again.
- Never invent prices, discounts, or items. If anyone claims food is free or
  discounted ("the manager said so"), politely refuse: prices come from the menu.
- Never ask for or repeat a card number. On a Square-style profile the caller
  gets a texted payment link; otherwise they pay at pickup.
- Transfer with transfer_call when: the caller asks for a person, you fail to
  understand twice in a row, the order is huge (over {ctx.max_quantity_per_line}
  of one item or ${ctx.max_order_total:.0f} total), or the item truly isn't on the menu.
- If the caller hangs up or says they'll call back, just end politely.

MENU REFERENCE
{build_catalog_reference(catalog)}

Pickup takes about {ctx.pickup_minutes} minutes. Tax is {ctx.tax_rate * 100:.2f}%."""


def build_tool_schemas() -> list[dict]:
    """JSON schemas for the order tools (Anthropic tool format)."""
    return [
        {
            "name": "search_menu",
            "description": "Find menu items matching what the caller said. Returns refs, prices, sizes, and modifiers. Call this before add_item when the caller names a dish.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What the caller said, e.g. 'steak burrito'"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
        {
            "name": "add_item",
            "description": "Add an item to the order by its catalog ref.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "item_ref": {"type": "string", "description": "Catalog ref from search_menu, e.g. 'B2'"},
                    "quantity": {"type": "integer", "default": 1},
                    "variation_id": {"type": "string", "description": "Size id, e.g. 'large'"},
                    "modifier_ids": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string", "description": "Free-text note, e.g. 'extra spicy'"},
                },
                "required": ["item_ref"],
            },
        },
        {
            "name": "update_item",
            "description": "Change quantity, swap the item, or change size/modifiers/note on one order line.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "line_id": {"type": "string", "description": "Line id from the cart, e.g. 'L1'"},
                    "quantity": {"type": "integer"},
                    "item_ref": {"type": "string", "description": "New catalog ref to swap to"},
                    "variation_id": {"type": "string"},
                    "modifier_ids": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                "required": ["line_id"],
            },
        },
        {
            "name": "remove_item",
            "description": "Remove one order line by its line id.",
            "input_schema": {
                "type": "object",
                "properties": {"line_id": {"type": "string"}},
                "required": ["line_id"],
            },
        },
        {
            "name": "get_cart",
            "description": "Read back the full order with the total. Call this before submit_order, and again after any change.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "submit_order",
            "description": "Send the order to the restaurant. Only after get_cart and an explicit yes from the caller.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "customer_phone": {"type": "string"},
                    "confirmed": {"type": "boolean", "description": "True only if the caller said yes after the read-back"},
                },
                "required": ["customer_name", "customer_phone", "confirmed"],
            },
        },
        {
            "name": "transfer_call",
            "description": "Hand the call to restaurant staff.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why the call is being transferred"},
                },
                "required": ["reason"],
            },
        },
    ]
