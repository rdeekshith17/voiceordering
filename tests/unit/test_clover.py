"""Unit tests for the real Clover adapter -- no live HTTP, no credentials.

Every Clover call is mocked at urllib.request.urlopen, so this suite burns
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
from voiceorder.pos_adapters.clover import CloverPosAdapter
from voiceorder.pos_adapters.fake import PROFILES, FakePos


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _adapter(catalog, **kw):
    kw.setdefault("environment", "sandbox")
    return CloverPosAdapter(
        access_token="clover_tok_test",
        merchant_id="M123",
        catalog=catalog,
        **kw,
    )


def _cart_with_tacos(catalog) -> Cart:
    cart = Cart(cart_id="c1", restaurant_id="r1", idempotency_key="k1")
    item = catalog.get("T1")
    cart.add_line(
        item_ref="T1",
        item_name=item.name,
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


# -- basics ---------------------------------------------------------------

def test_capabilities_match_clover_like():
    assert CloverPosAdapter.capabilities == PROFILES["clover_like"]


def test_missing_credentials_rejected(catalog):
    with pytest.raises(ValueError):
        CloverPosAdapter(access_token="", merchant_id="M", catalog=catalog)
    with pytest.raises(ValueError):
        CloverPosAdapter(access_token="t", merchant_id="", catalog=catalog)
    with pytest.raises(ValueError):
        CloverPosAdapter(access_token="t", merchant_id="M", environment="nope", catalog=catalog)


def test_quote_math_matches_fake(catalog):
    adapter = _adapter(catalog)
    fake = FakePos(profile="clover_like", catalog=catalog)
    cart = _cart_with_tacos(catalog)
    assert adapter.quote(cart) == fake.quote(cart)


# -- submit flow ------------------------------------------------------------

def test_submit_creates_order_and_line_items(catalog):
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    patcher, calls = _patch_urlopen([{"id": "clv-ord-789abc"}, {"id": "LI-1"}])
    with patcher:
        order = adapter.submit(cart, "idem-1")

    assert len(calls) == 2
    order_req, line_req = calls

    assert order_req.full_url == (
        "https://apisandbox.dev.clover.com/v3/merchants/M123/orders"
    )
    assert order_req.get_header("Authorization") == "Bearer clover_tok_test"
    order_body = json.loads(order_req.data.decode())
    assert order_body["state"] == "open"
    assert "Deekshith" in order_body["note"]
    assert "+12832298041" in order_body["note"]

    assert line_req.full_url == (
        "https://apisandbox.dev.clover.com/v3/merchants/M123/orders/clv-ord-789abc/line_items"
    )
    line_body = json.loads(line_req.data.decode())
    assert line_body["name"] == "Chicken Taco"
    assert line_body["price"] == 325  # cents
    assert line_body["unitQty"] == 2
    assert "Extra Cheese" in line_body["note"]

    # clover_like semantics: visible to staff, unpaid, pay at pickup
    assert order.order_id == "clv-ord-789abc"
    assert order.status == "received"
    assert order.totals == adapter.quote(cart)
    assert order.order_number == "789ABC"

    step = adapter.payment_step(order)
    assert step.kind == "pay_at_pickup"
    assert step.url is None


def test_submit_idempotent_retry_makes_no_new_calls(catalog):
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    patcher, calls = _patch_urlopen([{"id": "clv-ord-789abc"}, {"id": "LI-1"}])
    with patcher:
        first = adapter.submit(cart, "idem-7")
        second = adapter.submit(cart, "idem-7")
    assert first is second
    assert len(calls) == 2


def test_unlinked_modifier_fails_before_network(catalog):
    adapter = _adapter(catalog)
    cart = Cart(cart_id="c2", restaurant_id="r1", idempotency_key="k2")
    cart.add_line(
        item_ref="T1",
        item_name="Chicken Taco",
        quantity=1,
        unit_price=3.25,
        modifier_ids=["bogus_mod"],
        modifier_names=["Bogus"],
    )
    patcher, calls = _patch_urlopen([{"id": "clv-ord-789abc"}])
    with patcher, pytest.raises(PosError, match="not linked"):
        adapter.submit(cart, "idem-x")
    assert calls == []


def test_submit_sold_out_item_never_hits_network(catalog):
    adapter = _adapter(catalog)
    cart = Cart(cart_id="c3", restaurant_id="r1", idempotency_key="k3")
    cart.add_line(item_ref="NOPE", item_name="Ghost", quantity=1, unit_price=1.0)
    patcher, calls = _patch_urlopen([{"id": "clv-ord-789abc"}])
    with patcher, pytest.raises(PosError, match="sold out"):
        adapter.submit(cart, "idem-y")
    assert calls == []


# -- error mapping ----------------------------------------------------------

def _http_error(payload: dict, code: int = 400):
    return urllib.error.HTTPError(
        url="https://apisandbox.dev.clover.com/v3/merchants/M123/orders",
        code=code,
        msg="Bad Request",
        hdrs={},
        fp=io.BytesIO(json.dumps(payload).encode()),
    )


def test_clover_api_error_maps_to_pos_error(catalog):
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    patcher, _ = _patch_urlopen([_http_error({"message": "invalid merchant"})])
    with patcher, pytest.raises(PosError, match="invalid merchant"):
        adapter.submit(cart, "idem-err")


def test_network_failure_maps_to_pos_error(catalog):
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    patcher, _ = _patch_urlopen([urllib.error.URLError("boom")])
    with patcher, pytest.raises(PosError, match="unreachable"):
        adapter.submit(cart, "idem-net")


def test_production_base_url(catalog):
    adapter = _adapter(catalog, environment="production")
    cart = _cart_with_tacos(catalog)
    patcher, calls = _patch_urlopen([{"id": "clv-ord-789abc"}, {"id": "LI-1"}])
    with patcher:
        adapter.submit(cart, "idem-prod")
    assert calls[0].full_url.startswith("https://api.clover.com/v3/merchants/M123/orders")
