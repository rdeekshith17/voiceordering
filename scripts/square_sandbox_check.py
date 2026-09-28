"""Phase 0's 30-minute Square check (build plan section 6):

Create an order with a modifier and a pickup fulfillment, generate a payment
link, pay it with a Square sandbox test card, and confirm the paid order
shows up correctly. This answers the open question in section 10: whether a
paid payment-link order with modifiers and a pickup fulfillment actually
displays right on the POS -- the Square docs don't say outright.

This is a one-off validation script, not part of the order engine -- the
real, tested Square adapter (core/ports.PosAdapter) gets built in Phase 5
once this check and the pilot restaurant are both confirmed.

Setup (about 5 minutes):
  1. Create a free Square Developer account at https://developer.squareup.com
  2. Create an application; it comes with a Sandbox test account already.
  3. In the app's "Sandbox" tab, copy the "Sandbox Access Token".
  4. Under "Locations", copy the sandbox location's ID.
  5. In your shell (not pasted into chat):
       export SQUARE_ACCESS_TOKEN=sandbox-sq0...
       export SQUARE_LOCATION_ID=L...

Usage:
  python scripts/square_sandbox_check.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

API_BASE = "https://connect.squareupsandbox.com"
SQUARE_VERSION = os.environ.get("SQUARE_VERSION", "2024-10-17")

TEST_CARD_INSTRUCTIONS = """
Open that link in a browser and pay with a Square sandbox test card:
    Card number:  4111 1111 1111 1111
    Expiration:   any future date (e.g. 12/29)
    CVV:          any 3 digits
    Postal code:  any 5 digits
This does not charge anything real -- it's the sandbox.
"""


def _request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Square-Version", SQUARE_VERSION)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"\nSquare API error {e.code} on {method} {path}:", file=sys.stderr)
        print(e.read().decode(), file=sys.stderr)
        raise SystemExit(1) from e


def create_order(token: str, location_id: str) -> dict:
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "order": {
            "location_id": location_id,
            "line_items": [
                {
                    "name": "Steak Burrito",
                    "quantity": "1",
                    "base_price_money": {"amount": 1050, "currency": "USD"},
                    "modifiers": [
                        {
                            "name": "No Onions",
                            "base_price_money": {"amount": 0, "currency": "USD"},
                        }
                    ],
                }
            ],
            "fulfillments": [
                {
                    "type": "PICKUP",
                    "pickup_details": {
                        "recipient": {"display_name": "Alex (sandbox check)"},
                        "schedule_type": "ASAP",
                    },
                }
            ],
        },
    }
    result = _request("POST", "/v2/orders", token, body)
    return result["order"]


def create_payment_link(token: str, order_id: str) -> dict:
    body = {
        "idempotency_key": str(uuid.uuid4()),
        "order_id": order_id,
    }
    result = _request("POST", "/v2/online-checkout/payment-links", token, body)
    return result["payment_link"]


def fetch_order(token: str, order_id: str) -> dict:
    result = _request("GET", f"/v2/orders/{order_id}", token)
    return result["order"]


def describe_order(order: dict) -> str:
    total = order.get("total_money", {})
    tenders = order.get("tenders", [])
    fulfillments = order.get("fulfillments", [])
    lines = [
        f"  order id:     {order['id']}",
        f"  state:        {order.get('state')}",
        f"  total:        ${total.get('amount', 0) / 100:.2f} {total.get('currency', '')}",
        f"  paid:         {'yes' if tenders else 'no'} ({len(tenders)} tender(s))",
    ]
    for f in fulfillments:
        lines.append(f"  fulfillment:  {f.get('type')} / {f.get('state')}")
    return "\n".join(lines)


def main() -> None:
    token = os.environ.get("SQUARE_ACCESS_TOKEN")
    location_id = os.environ.get("SQUARE_LOCATION_ID")
    if not token or not location_id:
        raise SystemExit(
            "Set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID in your shell first "
            "(see the setup steps in this file's docstring)."
        )

    print("Creating a sandbox order: 1x Steak Burrito, No Onions, pickup...")
    order = create_order(token, location_id)
    print(describe_order(order))

    print("\nGenerating a payment link for that order...")
    link = create_payment_link(token, order["id"])
    print(f"  payment link: {link['url']}")
    print(TEST_CARD_INSTRUCTIONS)

    input("Press Enter once you've completed the payment in the browser... ")

    print("\nRe-fetching the order to confirm it's paid:")
    final_order = fetch_order(token, order["id"])
    print(describe_order(final_order))

    print(
        "\nNow check the Square sandbox Seller Dashboard (Orders) and confirm "
        "this order shows up there correctly -- item, modifier, pickup, and "
        "paid status all matching what's printed above."
    )


if __name__ == "__main__":
    main()
