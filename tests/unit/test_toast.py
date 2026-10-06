"""Unit tests for the real Toast adapter -- no live HTTP, no credentials.

Every Toast call is mocked at urllib.request.urlopen, so this suite burns
zero API credits and runs offline.
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from voiceorder.core.cart import Cart
from voiceorder.core.ports import PosError
from voiceorder.pos_adapters.fake import PROFILES, FakePos
from voiceorder.pos_adapters.toast import ToastPosAdapter


LOGIN_JSON = {"token": {"accessToken": "toast-tok-1"}}
MENUS_JSON = [
    {
        "guid": "menu-1",
        "name": "Main",
        "menuGroups": [
            {
                "guid": "g1",
                "name": "Tacos",
                "menuItems": [
                    {
                        "guid": "mi-t1",
                        "name": "Chicken Taco",
                        "price": 3.25,
                        "modifierGroups": [
                            {
                                "guid": "mg1",
                                "name": "Extras",
                                "modifierOptions": [
                                    {"guid": "mo-ec", "name": "Extra Cheese", "price": 0.5}
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
]
ORDER_JSON = {"guid": "toast-order-guid-123456", "entityType": "Order"}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _adapter(catalog, **kw):
    kw.setdefault("environment", "sandbox")
    kw.setdefault("takeout_dining_guid", "dine-takeout-1")
    return ToastPosAdapter(
        client_id="cid",
        client_secret="csecret",
        restaurant_guid="rest-guid-1",
        catalog=catalog,
        **kw,
    )


def _cart_with_tacos(catalog) -> Cart:
    cart = Cart(cart_id="c1", restaurant_id="r1", idempotency_key="k1")
    item = catalog.get("T1")
    cart.add_line(
        item_ref="T1",
        item_name=item.name,  # "Chicken Taco" -- matches synced Toast menu name
        quantity=2,
        unit_price=item.base_price,
        modifier_ids=["taco_cheese"],
        modifier_names=["Extra Cheese"],
    )
    cart.customer_name = "Deekshith"
    cart.customer_phone = "+12832298041"
    return cart


def _patch_urlopen(responses):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        next_resp = responses.pop(0)
        if isinstance(next_resp, Exception):
            raise next_resp
        return _FakeResponse(next_resp)

    return patch("voiceorder.net.urlopen", fake_urlopen), calls


def _http_error(payload: dict, code: int = 400):
    return urllib.error.HTTPError(
        url="https://ws-sandbox-api.toasttab.com/orders/v2/orders",
        code=code,
        msg="Bad Request",
        hdrs={},
        fp=io.BytesIO(json.dumps(payload).encode()),
    )


# -- basics ---------------------------------------------------------------

def test_capabilities_match_toast_like():
    assert ToastPosAdapter.capabilities == PROFILES["toast_like"]


def test_missing_credentials_rejected(catalog):
    with pytest.raises(ValueError):
        ToastPosAdapter(client_id="", client_secret="s", restaurant_guid="g", catalog=catalog)
    with pytest.raises(ValueError):
        ToastPosAdapter(client_id="c", client_secret="s", restaurant_guid="", catalog=catalog)
    with pytest.raises(ValueError):
        ToastPosAdapter(client_id="c", client_secret="s", restaurant_guid="g",
                         environment="nope", catalog=catalog)


def test_quote_math_matches_fake(catalog):
    adapter = _adapter(catalog)
    fake = FakePos(profile="toast_like", catalog=catalog)
    cart = _cart_with_tacos(catalog)
    assert adapter.quote(cart) == fake.quote(cart)


def test_submit_requires_quote_first(catalog):
    """Mirrors the toast_like fake: no quote, no order."""
    adapter = _adapter(catalog)
    adapter._item_guids = {"chicken taco": "mi-t1"}  # menu aligned, quote missing
    cart = _cart_with_tacos(catalog)
    patcher, calls = _patch_urlopen([LOGIN_JSON, ORDER_JSON])
    with patcher, pytest.raises(PosError, match="price quote"):
        adapter.submit(cart, "idem-1")
    assert calls == []


# -- auth + menu sync -------------------------------------------------------

def test_login_and_menu_sync(catalog, ctx):
    adapter = _adapter(catalog)
    patcher, calls = _patch_urlopen([LOGIN_JSON, MENUS_JSON])
    with patcher:
        returned = adapter.sync_catalog(ctx)

    assert returned is catalog
    login_req, menus_req = calls
    assert login_req.full_url.endswith("/authentication/v1/authentication/login")
    login_body = json.loads(login_req.data.decode())
    assert login_body == {
        "clientId": "cid",
        "clientSecret": "csecret",
        "userAccessType": "TOAST_MACHINE_CLIENT",
    }
    # Authenticated menu call carries the token + restaurant header
    assert menus_req.get_header("Authorization") == "Bearer toast-tok-1"
    assert menus_req.get_header("Toast-restaurant-external-id") == "rest-guid-1"
    assert adapter._item_guids["chicken taco"] == "mi-t1"
    assert adapter._modifier_guids["chicken taco"]["extra cheese"] == "mo-ec"


def test_login_failure_maps_to_pos_error(catalog, ctx):
    adapter = _adapter(catalog)
    patcher, _ = _patch_urlopen([_http_error({"message": "bad client"}, 401)])
    with patcher, pytest.raises(PosError, match="login failed"):
        adapter.sync_catalog(ctx)


# -- submit flow ------------------------------------------------------------

def _synced_adapter(catalog):
    adapter = _adapter(catalog)
    patcher, _ = _patch_urlopen([LOGIN_JSON, MENUS_JSON])
    with patcher:
        adapter.sync_catalog(None)
    return adapter


def test_submit_full_flow(catalog):
    adapter = _synced_adapter(catalog)
    cart = _cart_with_tacos(catalog)
    adapter.quote(cart)
    patcher, calls = _patch_urlopen([ORDER_JSON])
    with patcher:
        order = adapter.submit(cart, "idem-9")

    assert len(calls) == 1
    (req,) = calls
    assert req.full_url == "https://ws-sandbox-api.toasttab.com/orders/v2/orders"
    body = json.loads(req.data.decode())
    assert body["entityType"] == "Order"
    assert body["externalId"] == "idem-9"
    assert body["diningOption"] == {"guid": "dine-takeout-1"}
    assert body["promisedDate"]
    (check,) = body["checks"]
    assert check["customer"]["phone"] == "+12832298041"
    assert check["customer"]["firstName"] == "Deekshith"
    assert check["payments"] == []
    (sel,) = check["selections"]
    assert sel["entityType"] == "MenuItemSelection"
    assert sel["item"] == {"guid": "mi-t1"}
    assert sel["quantity"] == 2
    assert sel["modifiers"] == [
        {"entityType": "ModifierSelection", "item": {"guid": "mo-ec"}, "quantity": 1}
    ]

    # toast_like semantics: visible to staff, unpaid, pay at pickup
    assert order.order_id == "toast-order-guid-123456"
    assert order.status == "received"
    assert len(order.order_number) == 6
    step = adapter.payment_step(order)
    assert step.kind == "pay_at_pickup"
    assert step.url is None


def test_unmapped_modifier_folded_into_instructions(catalog):
    adapter = _synced_adapter(catalog)
    cart = Cart(cart_id="c2", restaurant_id="r1", idempotency_key="k2")
    cart.add_line(
        item_ref="T1", item_name="Chicken Taco", quantity=1, unit_price=3.25,
        modifier_ids=["taco_no_onions"], modifier_names=["No Onions"],
    )
    adapter.quote(cart)
    patcher, calls = _patch_urlopen([ORDER_JSON])
    with patcher:
        adapter.submit(cart, "idem-2")
    (sel,) = json.loads(calls[0].data.decode())["checks"][0]["selections"]
    assert sel["modifiers"] == []
    assert "No Onions" in sel["specialInstructions"]


def test_unmapped_item_raises(catalog):
    adapter = _synced_adapter(catalog)
    cart = Cart(cart_id="c3", restaurant_id="r1", idempotency_key="k3")
    cart.add_line(item_ref="T1", item_name="Mystery Burrito", quantity=1, unit_price=9.0)
    adapter.quote(cart)
    patcher, calls = _patch_urlopen([ORDER_JSON])
    with patcher, pytest.raises(PosError, match="not mapped to a Toast menu item"):
        adapter.submit(cart, "idem-3")
    assert calls == []


def test_submit_idempotent_retry_makes_no_new_calls(catalog):
    adapter = _synced_adapter(catalog)
    cart = _cart_with_tacos(catalog)
    adapter.quote(cart)
    patcher, calls = _patch_urlopen([ORDER_JSON])
    with patcher:
        first = adapter.submit(cart, "idem-4")
        second = adapter.submit(cart, "idem-4")
    assert first is second
    assert len(calls) == 1


def test_expired_token_relogins_once_and_retries(catalog):
    adapter = _synced_adapter(catalog)
    cart = _cart_with_tacos(catalog)
    adapter.quote(cart)
    patcher, calls = _patch_urlopen(
        [_http_error({"message": "expired"}, 401), LOGIN_JSON, ORDER_JSON]
    )
    with patcher:
        order = adapter.submit(cart, "idem-5")
    assert order.order_id == "toast-order-guid-123456"
    urls = [c.full_url for c in calls]
    assert urls[0].endswith("/orders/v2/orders")
    assert urls[1].endswith("/authentication/v1/authentication/login")
    assert urls[2].endswith("/orders/v2/orders")


def test_no_dining_guid_omits_dining_option(catalog):
    adapter = _synced_adapter(catalog)
    adapter._takeout_dining_guid = ""
    cart = _cart_with_tacos(catalog)
    adapter.quote(cart)
    patcher, calls = _patch_urlopen([ORDER_JSON])
    with patcher:
        adapter.submit(cart, "idem-6")
    body = json.loads(calls[0].data.decode())
    assert "diningOption" not in body


def test_production_host(catalog):
    adapter = _adapter(catalog, environment="production")
    patcher, calls = _patch_urlopen([LOGIN_JSON, MENUS_JSON])
    with patcher:
        adapter.sync_catalog(None)
    assert calls[0].full_url.startswith("https://ws-api.toasttab.com/authentication")
