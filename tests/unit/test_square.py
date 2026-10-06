"""Unit tests for the real Square adapter -- no live HTTP, no credentials.

Every Square call is mocked at voiceorder.net.urlopen, so this suite burns
zero API credits and runs offline.
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from voiceorder.core.cart import Cart
from voiceorder.core.catalog import Catalog
from voiceorder.pos_adapters.fake import FakePos
from voiceorder.pos_adapters.square import SquarePosAdapter, _cents
from voiceorder.core.ports import PosError


# The Orders API creates the order (OPEN, visible in the dashboard) and the
# Invoices API mints the payment page. submit() makes four HTTP calls when
# the customer already exists in Square:
#   POST /orders -> POST /customers/search -> POST /invoices -> POST /invoices/{id}/publish
ORDER_JSON = {
    "order": {
        "id": "sq-ord-abc123def456",
        "location_id": "L123",
        "state": "OPEN",
        "total_money": {"amount": 706, "currency": "USD"},
        "total_tax_money": {"amount": 54, "currency": "USD"},
    }
}

CUSTOMER_SEARCH_JSON = {
    "customers": [{"id": "cust-1", "given_name": "Deekshith"}]
}

INVOICE_JSON = {
    "invoice": {"id": "inv-1", "version": 1, "status": "DRAFT"}
}

INVOICE_PUBLISH_JSON = {
    "invoice": {
        "id": "inv-1",
        "version": 2,
        "status": "UNPAID",
        "public_url": "https://squareup.com/pay/inv-abc123",
    }
}


class _FakeResponse:
    """Minimal urlopen context manager returning canned JSON."""

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
    return SquarePosAdapter(
        access_token="tok_sandbox_test",
        location_id="L123",
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
        modifier_names=["Extra Cheese"],
    )
    cart.customer_name = "Deekshith"
    cart.customer_phone = "+12832298041"
    return cart


def _patch_urlopen(responses):
    """responses: list of payloads or exceptions, consumed in order."""
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        next_resp = responses.pop(0)
        if isinstance(next_resp, Exception):
            raise next_resp
        return _FakeResponse(next_resp)

    return patch("voiceorder.net.urlopen", fake_urlopen), calls


# -- basics ---------------------------------------------------------------

def test_capabilities_reflect_visible_unpaid_orders():
    # Unlike the square_like fake (payment links with DRAFT orders), the
    # real adapter creates OPEN orders via the Orders API, so staff see
    # unpaid orders in the dashboard; payment still goes through a link.
    assert SquarePosAdapter.capabilities.unpaid_orders_visible is True
    assert SquarePosAdapter.capabilities.payment_links is True
    assert SquarePosAdapter.capabilities.pay_at_pickup is False


def test_ping_hits_location_url_once(catalog):
    # Regression: ping() built "/v2/locations/..." while _base already ends
    # in /v2, producing /v2/v2/... -> Square 404.
    adapter = _adapter(catalog)
    patcher, calls = _patch_urlopen([{"location": {"name": "Testaurant",
                                                  "status": "ACTIVE"}}])
    with patcher:
        detail = adapter.ping()
    assert detail["ok"] is True and detail["location_name"] == "Testaurant"
    assert len(calls) == 1
    assert calls[0].full_url == ("https://connect.squareupsandbox.com/v2"
                                 "/locations/L123")


def test_missing_credentials_rejected(catalog):
    with pytest.raises(ValueError):
        SquarePosAdapter(access_token="", location_id="L", catalog=catalog)
    with pytest.raises(ValueError):
        SquarePosAdapter(access_token="t", location_id="", catalog=catalog)
    with pytest.raises(ValueError):
        SquarePosAdapter(access_token="t", location_id="L", environment="nope", catalog=catalog)


def test_quote_math_matches_fake(catalog):
    adapter = _adapter(catalog)
    fake = FakePos(profile="square_like", catalog=catalog)
    cart = _cart_with_tacos(catalog)
    assert adapter.quote(cart) == fake.quote(cart)
    assert adapter.quote(cart).total == round(2 * 3.25 * 1.0825, 2)


def test_is_available_uses_local_catalog(catalog):
    adapter = _adapter(catalog)
    assert adapter.is_available("T1") is True
    assert adapter.is_available("NOPE") is False


# -- submit flow ------------------------------------------------------------

def test_submit_builds_square_order_and_invoice_link(catalog):
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    patcher, calls = _patch_urlopen(
        [ORDER_JSON, CUSTOMER_SEARCH_JSON, INVOICE_JSON, INVOICE_PUBLISH_JSON]
    )
    with patcher:
        order = adapter.submit(cart, "idem-1")

    # Four HTTP calls: order (OPEN), customer lookup, invoice, publish.
    assert len(calls) == 4
    order_req, search_req, invoice_req, publish_req = calls
    assert order_req.full_url.endswith("/orders")
    assert order_req.get_header("Authorization") == "Bearer tok_sandbox_test"
    assert order_req.get_header("Square-version") == "2024-12-18"
    body = json.loads(order_req.data.decode())
    assert body["idempotency_key"] == "idem-1"
    sq_order = body["order"]
    assert sq_order["location_id"] == "L123"
    (line,) = sq_order["line_items"]
    assert line["name"] == "Chicken Taco"
    assert line["quantity"] == "2"
    assert line["base_price_money"] == {"amount": 325, "currency": "USD"}
    assert "Extra Cheese" in line["note"]
    assert sq_order["fulfillments"][0]["pickup_details"]["recipient"] == {
        "display_name": "Deekshith"
    }
    # Explicit sales tax so the invoice charges tax, not just subtotal
    (tax,) = sq_order["taxes"]
    assert tax["name"] == "Sales Tax"
    assert tax["scope"] == "ORDER"

    # Customer lookup uses the caller's phone number
    search_body = json.loads(search_req.data.decode())
    assert search_body["query"]["filter"]["phone_number"]["exact"] == "+12832298041"

    # Invoice is share-manually: Square hosts the page, we text the URL
    invoice_body = json.loads(invoice_req.data.decode())
    assert invoice_body["invoice"]["order_id"] == "sq-ord-abc123def456"
    assert invoice_body["invoice"]["primary_recipient"] == {"customer_id": "cust-1"}
    assert invoice_body["invoice"]["delivery_method"] == "SHARE_MANUALLY"
    assert publish_req.full_url.endswith("/invoices/inv-1/publish")

    # PosOrder: pending payment, Square totals
    assert order.order_id == "sq-ord-abc123def456"
    assert order.status == "pending_payment"
    assert order.totals.total == 7.06
    assert order.totals.tax == 0.54
    assert order.totals.subtotal == 6.52
    assert len(order.order_number) == 6

    # payment_step serves the cached invoice URL without another HTTP call
    with patcher:  # responses exhausted; any call would raise IndexError
        step = adapter.payment_step(order)
    assert step.kind == "link"
    assert step.url == "https://squareup.com/pay/inv-abc123"
    assert len(calls) == 4


def test_submit_creates_customer_when_phone_unknown(catalog):
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    patcher, calls = _patch_urlopen(
        [
            ORDER_JSON,
            {"customers": []},  # no match: adapter creates the customer
            {"customer": {"id": "cust-2"}},
            INVOICE_JSON,
            INVOICE_PUBLISH_JSON,
        ]
    )
    with patcher:
        order = adapter.submit(cart, "idem-cust")
    assert len(calls) == 5
    create_body = json.loads(calls[2].data.decode())
    assert create_body["given_name"] == "Deekshith"
    assert create_body["phone_number"] == "+12832298041"
    step = adapter.payment_step(order)
    assert step.url == "https://squareup.com/pay/inv-abc123"


def test_submit_survives_invoice_failure_with_pickup_fallback(catalog):
    # The order is OPEN and visible even if invoicing fails; payment_step
    # then raises so the caller falls back to pay-at-pickup.
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    err = _http_error(
        {"errors": [{"code": "FORBIDDEN", "detail": "missing INVOICES_WRITE"}]}
    )
    patcher, calls = _patch_urlopen([ORDER_JSON, CUSTOMER_SEARCH_JSON, err])
    with patcher:
        order = adapter.submit(cart, "idem-invfail")
    assert order.order_id == "sq-ord-abc123def456"
    with pytest.raises(PosError, match="payment link is not available"):
        adapter.payment_step(order)


def test_payments_not_enabled_disables_invoice_probe(catalog):
    # When Square says the account can't take payments, the adapter stops
    # burning invoice API calls on later orders (pay-at-pickup covers it).
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    err = _http_error(
        {
            "errors": [
                {
                    "code": "BAD_REQUEST",
                    "detail": "This account has not been enabled to take payments",
                }
            ]
        }
    )
    patcher, calls = _patch_urlopen([ORDER_JSON, CUSTOMER_SEARCH_JSON, err])
    with patcher:
        adapter.submit(cart, "idem-noinv")
    assert len(calls) == 3
    # Next order: order call only, no customer/invoice calls.
    patcher2, calls2 = _patch_urlopen([ORDER_JSON])
    with patcher2:
        order2 = adapter.submit(cart, "idem-noinv-2")
    assert len(calls2) == 1
    assert order2.order_id == "sq-ord-abc123def456"


def _submit_responses():
    return [ORDER_JSON, CUSTOMER_SEARCH_JSON, INVOICE_JSON, INVOICE_PUBLISH_JSON]


def test_submit_idempotent_retry_makes_no_new_calls(catalog):
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    patcher, calls = _patch_urlopen(_submit_responses())
    with patcher:
        first = adapter.submit(cart, "idem-9")
        second = adapter.submit(cart, "idem-9")
    assert first is second
    assert len(calls) == 4  # retry was free


def test_submit_sold_out_item_never_hits_network(catalog):
    adapter = _adapter(catalog)
    cart = Cart(cart_id="c2", restaurant_id="r1", idempotency_key="k2")
    cart.add_line(item_ref="NOPE", item_name="Ghost", quantity=1, unit_price=1.0)
    patcher, calls = _patch_urlopen(_submit_responses())
    with patcher, pytest.raises(PosError, match="sold out"):
        adapter.submit(cart, "idem-x")
    assert calls == []


def test_payment_step_without_cached_link_raises(catalog):
    # The invoice URL is minted at submit time and kept in-process; a
    # recreated adapter (empty cache) cannot rebuild it: it must fail
    # loudly instead of inventing one.
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    patcher, calls = _patch_urlopen(_submit_responses())
    with patcher:
        order = adapter.submit(cart, "idem-2")

    fresh = _adapter(catalog)  # process restart: caches are empty
    with pytest.raises(PosError, match="payment link is not available"):
        fresh.payment_step(order)


# -- error mapping ----------------------------------------------------------

def _http_error(payload: dict, code: int = 400):
    return urllib.error.HTTPError(
        url="https://connect.squareupsandbox.com/v2/orders",
        code=code,
        msg="Bad Request",
        hdrs={},
        fp=io.BytesIO(json.dumps(payload).encode()),
    )


def test_square_api_error_maps_to_pos_error(catalog):
    adapter = _adapter(catalog)
    cart = _cart_with_tacos(catalog)
    err = _http_error(
        {"errors": [{"code": "BAD_REQUEST", "detail": "Invalid location id"}]}
    )
    patcher, _ = _patch_urlopen([err])
    with patcher, pytest.raises(PosError, match="BAD_REQUEST.*Invalid location id"):
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
    patcher, calls = _patch_urlopen(_submit_responses())
    with patcher:
        adapter.submit(cart, "idem-prod")
    assert calls[0].full_url.startswith("https://connect.squareup.com/v2/orders")


def test_cents_rounding():
    assert _cents(3.25) == 325
    assert _cents(3.255) == 326  # half-cent rounds up, never short-changes tax
