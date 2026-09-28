from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from voiceorder.api import main as api_main

DEMO_NUMBER = "+15551234567"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("VOICEORDER_WEBHOOK_SECRET", raising=False)
    api_main._sessions.clear()
    for screen in api_main._backup_screens.values():
        screen.entries.clear()
    return TestClient(api_main.app)


def _start_call(client: TestClient, call_id: str) -> dict:
    resp = client.post("/call/start", json={"call_id": call_id, "called_number": DEMO_NUMBER})
    assert resp.status_code == 200
    return resp.json()


def _call_tool(client: TestClient, call_id: str, tool_name: str, **arguments) -> dict:
    resp = client.post(
        f"/tools/{tool_name}", json={"call_id": call_id, "arguments": arguments}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_full_order_flow_via_api(client):
    start = _start_call(client, "call-1")
    assert start["restaurant_id"] == "taqueria-demo"
    assert start["hours"]

    _call_tool(client, "call-1", "add_item", item_ref="churros", quantity=1)
    _call_tool(client, "call-1", "get_cart")
    result = _call_tool(
        client, "call-1", "submit_order", customer_name="Alex", confirmed=True
    )
    assert result["status"] == "confirmed"

    end = client.post("/call/end", json={"call_id": "call-1"})
    assert end.status_code == 200


def test_unknown_tool_name_is_rejected(client):
    _start_call(client, "call-1")
    resp = client.post(
        "/tools/delete_everything", json={"call_id": "call-1", "arguments": {}}
    )
    assert resp.status_code == 404


def test_tool_call_without_call_id_is_rejected(client):
    resp = client.post("/tools/get_cart", json={"arguments": {}})
    assert resp.status_code == 400


def test_backup_screen_reflects_unpaid_square_order(client):
    _start_call(client, "call-1")
    _call_tool(client, "call-1", "add_item", item_ref="churros")
    _call_tool(client, "call-1", "get_cart")
    _call_tool(client, "call-1", "submit_order", customer_name="Alex", confirmed=True)

    resp = client.get("/backup-screen/taqueria-demo")
    assert resp.status_code == 200
    kinds = [e["kind"] for e in resp.json()["entries"]]
    assert "unpaid" in kinds


def test_backup_screen_unknown_restaurant_404s(client):
    resp = client.get("/backup-screen/not-a-real-restaurant")
    assert resp.status_code == 404


def test_two_calls_do_not_leak_carts_into_each_other(client):
    _start_call(client, "call-a")
    _start_call(client, "call-b")

    _call_tool(client, "call-a", "add_item", item_ref="churros")
    _call_tool(client, "call-b", "add_item", item_ref="flan")

    cart_a = _call_tool(client, "call-a", "get_cart")
    cart_b = _call_tool(client, "call-b", "get_cart")

    assert [line["description"] for line in cart_a["lines"]] == ["1 x Churros"]
    assert [line["description"] for line in cart_b["lines"]] == ["1 x Flan"]


def test_webhook_secret_enforced_only_when_configured(client, monkeypatch):
    monkeypatch.setenv("VOICEORDER_WEBHOOK_SECRET", "s3cret")

    unauthorized = client.post(
        "/call/start", json={"call_id": "call-1", "called_number": DEMO_NUMBER}
    )
    assert unauthorized.status_code == 401

    authorized = client.post(
        "/call/start",
        json={"call_id": "call-1", "called_number": DEMO_NUMBER},
        headers={"Authorization": "Bearer s3cret"},
    )
    assert authorized.status_code == 200
