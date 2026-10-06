"""Golden script: the burrito call, end to end over HTTP.

Caller: two chicken burritos
Caller: actually make one of them steak
Caller: no onions on the steak one
Caller: add a large horchata... is it dairy-free?
Caller: never mind, make it a Coke
Caller: yes, that's right

Expected cart: 1 x Chicken Burrito, 1 x Steak Burrito [No onions], 1 x Coke (Large)
Expected: submit_order called once, after the read-back.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from voiceorder.api import main as api_main

app = api_main.app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_voice_secret():
    # Golden scripts exercise the order engine over HTTP, not auth: the repo
    # .env sets VOICE_SECRET for the server, which would otherwise 401.
    api_main.settings.voice_secret = ""
    api_main.settings.voice_secret_previous = ""


def tool(cart_id, call_id, name, **arguments):
    response = client.post(
        f"/tools/{name}",
        json={"call_id": call_id, "cart_id": cart_id, "arguments": arguments},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_burrito_call_end_to_end():
    start = client.post(
        "/call/start", json={"called_number": "+15550130000", "caller_id": "+15551234567"}
    ).json()
    call_id, cart_id = start["call_id"], start["cart_id"]
    assert start["greeting"].startswith("Thanks for calling ")
    assert "What can I get started for you?" in start["greeting"]

    # two chicken burritos
    hits = tool(cart_id, call_id, "search_menu", query="chicken burrito")["data"]["matches"]
    assert hits[0]["ref"] == "B1"
    added = tool(
        cart_id, call_id, "add_item",
        item_ref="B1", quantity=2, modifier_ids=["burrito_rice_beans"],
    )
    assert added["ok"]
    chicken_line = added["data"]["line_id"]

    # actually make one of them steak
    tool(cart_id, call_id, "update_item", line_id=chicken_line, quantity=1)
    added = tool(
        cart_id, call_id, "add_item",
        item_ref="B2", quantity=1, modifier_ids=["burrito_rice_beans"],
    )
    steak_line = added["data"]["line_id"]

    # no onions on the steak one
    updated = tool(
        cart_id, call_id, "update_item",
        line_id=steak_line, modifier_ids=["burrito_rice_beans", "burrito_no_onions"],
    )
    assert updated["ok"]
    assert "No Onions" in updated["data"]["cart"]["lines"][1]["modifier_names"]

    # add a large horchata ... never mind, make it a Coke
    hits = tool(cart_id, call_id, "search_menu", query="horchata")["data"]["matches"]
    assert hits[0]["ref"] == "D1"
    added = tool(
        cart_id, call_id, "add_item",
        item_ref="D1", quantity=1, variation_id="large",
    )
    assert added["ok"]
    horchata_line = added["data"]["line_id"]
    tool(cart_id, call_id, "remove_item", line_id=horchata_line)
    added = tool(
        cart_id, call_id, "add_item",
        item_ref="D3", quantity=1, variation_id="large",
    )
    assert added["ok"]

    # yes, that's right -> read back, then submit exactly once
    read_back = tool(cart_id, call_id, "get_cart")
    assert read_back["ok"]
    assert "$" in read_back["message"]
    lines = read_back["data"]["cart"]["lines"]
    assert len(lines) == 3
    by_ref = {line["item_ref"]: line for line in lines}
    assert by_ref["B1"]["quantity"] == 1
    assert by_ref["B2"]["quantity"] == 1
    assert "No Onions" in by_ref["B2"]["modifier_names"]
    assert by_ref["D3"]["variation_name"] == "Large"

    submitted = tool(
        cart_id, call_id, "submit_order",
        customer_name="Deekshith", customer_phone="+15551234567", confirmed=True,
    )
    assert submitted["ok"]
    order = submitted["data"]["order"]
    assert order["order_number"]
    assert order["totals"]["total"] == read_back["data"]["totals"]["total"]

    end = client.post(
        "/call/end",
        json={"call_id": call_id, "cart_id": cart_id, "duration_seconds": 95.0,
              "transcript": "burrito call"},
    ).json()
    assert end["order_id"] == order["order_id"]


def test_unknown_tool_is_404():
    start = client.post("/call/start", json={}).json()
    response = client.post(
        "/tools/teleport",
        json={"call_id": start["call_id"], "cart_id": start["cart_id"], "arguments": {}},
    )
    assert response.status_code == 404


def test_expired_cart_is_404():
    response = client.post(
        "/tools/get_cart",
        json={"call_id": "nope", "cart_id": "nope", "arguments": {}},
    )
    assert response.status_code == 404


def test_health():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["menu_items"] == 25
