"""Demo: a full simulated phone order against a running VoiceOrderAI server.

Usage:
    uvicorn voiceorder.api.main:app --port 8000 &
    python scripts/demo_call.py
"""
from __future__ import annotations

import sys

import httpx

BASE = "http://localhost:8000"


def say(role: str, text: str) -> None:
    print(f"\n[{role}] {text}")


def tool(client: httpx.Client, call_id: str, cart_id: str, name: str, **args) -> dict:
    resp = client.post(
        f"{BASE}/tools/{name}",
        json={"call_id": call_id, "cart_id": cart_id, "arguments": args},
    )
    resp.raise_for_status()
    body = resp.json()
    say("AGENT", body["message"])
    return body


def main() -> int:
    # trust_env=False: this sandbox sets proxy env vars that don't parse for
    # localhost traffic; the demo talks to the local server directly.
    client = httpx.Client(timeout=10.0, trust_env=False)
    try:
        health = client.get(f"{BASE}/health").json()
    except httpx.ConnectError:
        print(f"Cannot reach {BASE}. Start the server first:\n"
              "  uvicorn voiceorder.api.main:app --port 8000")
        return 1
    say("SYSTEM", f"connected to {health['restaurant']} "
                  f"(POS profile: {health['pos_profile']}, menu: {health['menu_items']} items)")

    start = client.post(
        f"{BASE}/call/start",
        json={"called_number": "+15550130000", "caller_id": "+15551234567"},
    ).json()
    call_id, cart_id = start["call_id"], start["cart_id"]
    say("AGENT", start["greeting"])

    say("CALLER", "Hi! Can I get two chicken burritos?")
    body = tool(client, call_id, cart_id, "search_menu", query="chicken burrito")
    ref = body["data"]["matches"][0]["ref"]
    added = tool(client, call_id, cart_id, "add_item",
                 item_ref=ref, quantity=2, modifier_ids=["burrito_rice_beans"])
    chicken_line = added["data"]["line_id"]

    say("CALLER", "Actually, make one of them steak.")
    tool(client, call_id, cart_id, "update_item", line_id=chicken_line, quantity=1)
    body = tool(client, call_id, cart_id, "search_menu", query="steak burrito")
    steak_ref = body["data"]["matches"][0]["ref"]
    added = tool(client, call_id, cart_id, "add_item",
                 item_ref=steak_ref, quantity=1, modifier_ids=["burrito_rice_beans"])
    steak_line = added["data"]["line_id"]

    say("CALLER", "No onions on the steak one, please.")
    tool(client, call_id, cart_id, "update_item", line_id=steak_line,
         modifier_ids=["burrito_rice_beans", "burrito_no_onions"])

    say("CALLER", "And add a large horchata... wait, is it dairy-free?")
    body = tool(client, call_id, cart_id, "search_menu", query="horchata")
    horchata_ref = body["data"]["matches"][0]["ref"]
    say("AGENT", "The horchata is made with rice milk, so yes -- dairy-free!")
    added = tool(client, call_id, cart_id, "add_item",
                 item_ref=horchata_ref, quantity=1, variation_id="large")
    horchata_line = added["data"]["line_id"]

    say("CALLER", "Never mind, make it a Coke instead.")
    tool(client, call_id, cart_id, "remove_item", line_id=horchata_line)
    body = tool(client, call_id, cart_id, "search_menu", query="coke")
    coke_ref = body["data"]["matches"][0]["ref"]
    tool(client, call_id, cart_id, "add_item",
         item_ref=coke_ref, quantity=1, variation_id="large")

    say("CALLER", "That's everything.")
    read_back = tool(client, call_id, cart_id, "get_cart")

    say("CALLER", "Yes, that's right. Name's Deekshith, number 555-123-4567.")
    submitted = tool(client, call_id, cart_id, "submit_order",
                     customer_name="Deekshith", customer_phone="+15551234567",
                     confirmed=True)
    order = submitted["data"]["order"]
    say("SYSTEM", f"order #{order['order_number']} posted to the "
                  f"{health['pos_profile']} POS as '{order['status']}'")

    end = client.post(f"{BASE}/call/end", json={
        "call_id": call_id, "cart_id": cart_id,
        "duration_seconds": 95.0, "transcript": "demo burrito call",
    }).json()
    say("SYSTEM", f"call ended; order_id={end['order_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
