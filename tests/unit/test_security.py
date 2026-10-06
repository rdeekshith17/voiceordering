"""Phase 4 security pass: no card data anywhere, auth on owner endpoints,
secret rotation on the voice API."""
import json
import re
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from voiceorder.api import main as api_main
from voiceorder.api.owner import OwnerDeps, build_owner_router
from voiceorder.api.storage import InMemoryCartStore, InMemoryOrderStore
from voiceorder.core import tools
from voiceorder.core.cart import Cart
from voiceorder.core.catalog import Catalog
from voiceorder.core.ports import RestaurantContext
from voiceorder.pos_adapters.fake import FakePos

FIXTURE = Path(__file__).resolve().parents[2] / "voiceorder" / "fixtures" / "menu_taqueria.json"
CARD_RE = re.compile(r"\d{13,19}")  # card numbers are 13-19 contiguous digits


def _env():
    catalog = Catalog.from_json(FIXTURE)
    ctx = RestaurantContext(
        restaurant_id="taqueria-demo",
        restaurant_name="Taqueria Demo",
        pos_profile="square_like",
        voice_platform="text",
        transfer_number="+15550134200",
    )
    return catalog, ctx, FakePos(profile="square_like", catalog=catalog)


def test_no_card_numbers_in_any_tool_output():
    """Full order flow serialized: nothing shaped like a card number may appear."""
    catalog, ctx, pos = _env()
    cart = Cart(cart_id="s1", restaurant_id=ctx.restaurant_id,
                idempotency_key=uuid.uuid4().hex)
    blobs: list[str] = []
    calls = [
        ("search_menu", {"query": "burrito"}),
        ("add_item", {"item_ref": "B1", "quantity": 1, "modifier_ids": ["burrito_rice_beans"]}),
        ("add_item", {"item_ref": "D3", "quantity": 1, "variation_id": "large"}),
        ("get_cart", {}),
        ("submit_order", {"customer_name": "T", "customer_phone": "555-123-4567",
                          "confirmed": True, "idempotency_key": cart.idempotency_key}),
    ]
    for name, args in calls:
        result = tools.dispatch(name, cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                                arguments=args)
        blobs.append(result.message)
        blobs.append(json.dumps(result.data, default=str))
    haystack = "\n".join(blobs)
    assert not CARD_RE.search(haystack), f"card-like digits leaked: {CARD_RE.search(haystack).group(0)}"


def _owner_client(secret: str) -> TestClient:
    catalog, ctx, pos = _env()
    app = FastAPI()
    app.include_router(
        build_owner_router(
            OwnerDeps(
                catalog=catalog, pos=pos,
                cart_store=InMemoryCartStore(), order_store=InMemoryOrderStore(),
                ctx=ctx, owner_secret=secret,
            )
        )
    )
    return TestClient(app)


def test_owner_endpoints_reject_missing_secret():
    client = _owner_client("s3cret")
    assert client.get("/owner").status_code == 401
    assert client.get("/owner/menu").status_code == 401
    assert client.post("/owner/menu/availability",
                       json={"item_ref": "T1", "available": False}).status_code == 401


def test_owner_endpoints_accept_header_or_query_key():
    client = _owner_client("s3cret")
    assert client.get("/owner", headers={"X-Owner-Secret": "s3cret"}).status_code == 200
    assert client.get("/owner/menu?key=s3cret").status_code == 200
    assert client.get("/owner/menu", headers={"X-Owner-Secret": "wrong"}).status_code == 401


def test_owner_can_86_and_restore_and_backup_order():
    client = _owner_client("s3cret")
    headers = {"X-Owner-Secret": "s3cret"}
    menu = client.get("/owner/menu", headers=headers).json()
    assert len(menu["items"]) == 25
    assert client.post("/owner/menu/availability", headers=headers,
                       json={"item_ref": "T1", "available": False}).json()["available"] is False
    blocked = client.post("/owner/backup/submit", headers=headers, json={
        "lines": [{"item_ref": "T1", "quantity": 1}],
        "customer_name": "Staff", "customer_phone": "555-000-0000",
    })
    assert blocked.status_code == 400
    client.post("/owner/menu/availability", headers=headers,
                json={"item_ref": "T1", "available": True})
    fired = client.post("/owner/backup/submit", headers=headers, json={
        "lines": [{"item_ref": "T1", "quantity": 2}],
        "customer_name": "Walk-in", "customer_phone": "555-000-0001",
    })
    assert fired.status_code == 200
    order = fired.json()["order"]
    assert order["totals"]["total"] > 0
    orders = client.get("/owner/orders", headers=headers).json()["orders"]
    assert any(o["order_number"] == order["order_number"] for o in orders)


def test_owner_backup_rejects_empty_ticket():
    client = _owner_client("s3cret")
    response = client.post("/owner/backup/submit",
                           headers={"X-Owner-Secret": "s3cret"},
                           json={"lines": [], "customer_name": "X", "customer_phone": "1"})
    assert response.status_code == 400


def test_voice_secret_rotation_accepts_previous(monkeypatch):
    monkeypatch.setattr(api_main.settings, "voice_secret", "new-secret")
    monkeypatch.setattr(api_main.settings, "voice_secret_previous", "old-secret")
    api_main.require_secret("new-secret")  # no raise
    api_main.require_secret("old-secret")  # no raise
    try:
        api_main.require_secret("wrong")
    except Exception as exc:  # noqa: BLE001 -- asserting the 401 shape
        assert getattr(exc, "status_code", None) == 401
    else:
        raise AssertionError("expected HTTPException for a wrong secret")
