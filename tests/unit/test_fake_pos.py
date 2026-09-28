from __future__ import annotations

import pytest

from voiceorder.core.cart import Cart
from voiceorder.core.tools import OrderTools, ToolError
from voiceorder.pos_adapters.fake import PROFILES, FakePos, FakePosError, FakePosTimeout


@pytest.mark.parametrize("profile", list(PROFILES))
def test_quote_computes_tax(catalog, profile):
    pos = FakePos(profile=profile, catalog=catalog)
    cart = Cart(call_id="c1")
    cart.add_line(item_id="coke", name="Coke", quantity=2, unit_price_cents=250)
    totals = pos.quote(cart)
    assert totals.subtotal_cents == 500
    assert totals.total_cents == totals.subtotal_cents + totals.tax_cents


def test_submit_is_idempotent(catalog):
    pos = FakePos(profile="square_like", catalog=catalog)
    cart = Cart(call_id="c1")
    cart.add_line(item_id="coke", name="Coke", quantity=1, unit_price_cents=250)
    first = pos.submit(cart, idempotency_key="key-1")
    second = pos.submit(cart, idempotency_key="key-1")
    assert first.order_id == second.order_id


def test_down_failure_mode_raises(catalog):
    pos = FakePos(profile="square_like", catalog=catalog, failure_mode="down")
    cart = Cart(call_id="c1")
    with pytest.raises(FakePosError):
        pos.submit(cart, idempotency_key="key-1")


def test_slow_failure_mode_raises_timeout(catalog):
    pos = FakePos(profile="square_like", catalog=catalog, failure_mode="slow")
    cart = Cart(call_id="c1")
    with pytest.raises(FakePosTimeout):
        pos.submit(cart, idempotency_key="key-1")


def test_mark_sold_out_makes_item_unavailable(catalog):
    pos = FakePos(profile="square_like", catalog=catalog)
    pos.mark_sold_out("coke")
    tools = OrderTools(cart=Cart(call_id="c1"), catalog=catalog, pos=pos)
    with pytest.raises(ToolError):
        tools.add_item(item_ref="coke")


def test_square_like_uses_payment_link(catalog):
    pos = FakePos(profile="square_like", catalog=catalog)
    order = pos.submit(Cart(call_id="c1"), idempotency_key="key-1")
    assert pos.payment_step(order).kind == "link"


@pytest.mark.parametrize("profile", ["clover_like", "toast_like"])
def test_clover_and_toast_pay_at_pickup(catalog, profile):
    pos = FakePos(profile=profile, catalog=catalog)
    order = pos.submit(Cart(call_id="c1"), idempotency_key="key-1")
    assert pos.payment_step(order).kind == "pay_at_pickup"
