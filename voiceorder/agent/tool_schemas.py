from __future__ import annotations

# Anthropic tool definitions for the six tools in core/tools.py. The shape
# never changes with the menu -- refs are validated server-side against
# whichever catalog is loaded for the call, so the schema stays static
# while the system prompt (see prompt.py) carries the restaurant-specific
# instructions.
TOOL_SCHEMAS: list[dict] = [
    {
        "name": "search_menu",
        "description": (
            "Look up items on the menu by what the caller said. Returns matching "
            "items with their refs, sizes and modifier groups. Call this before "
            "add_item whenever you don't already have a ref for what they asked for."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the caller said, e.g. 'steak burrito' or 'horchata'.",
                }
            },
            "required": ["query"],
        },
    },
    {
        "name": "add_item",
        "description": "Add a line to the order. item_ref and variation_ref/modifier_refs must come from search_menu results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_ref": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1, "default": 1},
                "variation_ref": {
                    "type": "string",
                    "description": "Size ref, if the item has sizes.",
                },
                "modifier_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Modifier refs to apply, e.g. add-ins like no onions.",
                },
                "note": {"type": "string"},
            },
            "required": ["item_ref"],
        },
    },
    {
        "name": "update_item",
        "description": "Change one existing line by its line_id: quantity, size, modifiers or note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "line_id": {"type": "integer"},
                "quantity": {"type": "integer", "minimum": 1},
                "variation_ref": {"type": "string"},
                "modifier_refs": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"},
            },
            "required": ["line_id"],
        },
    },
    {
        "name": "remove_item",
        "description": "Remove one line from the order by its line_id.",
        "input_schema": {
            "type": "object",
            "properties": {"line_id": {"type": "integer"}},
            "required": ["line_id"],
        },
    },
    {
        "name": "get_cart",
        "description": (
            "Get the current order with totals and a spoken-style read-back. "
            "You must call this and read it back to the caller before submit_order."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_order",
        "description": (
            "Submit the order to the kitchen. Only call this after you've read the "
            "order back with get_cart and the caller has clearly said yes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_name": {"type": "string"},
                "confirmed": {
                    "type": "boolean",
                    "description": "True only if the caller just confirmed the read-back.",
                },
            },
            "required": ["customer_name", "confirmed"],
        },
    },
    {
        "name": "transfer_call",
        "description": (
            "Hand the call to restaurant staff. Use this when the caller directly "
            "asks for a person, or when search_menu has returned no match twice in "
            "a row for the same request."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Short reason, e.g. 'caller asked for a person' or 'item not found twice'.",
                }
            },
            "required": ["reason"],
        },
    },
]
