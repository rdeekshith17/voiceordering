"""Tool tests, run against every fake POS profile.

Phase 1 gate: all seven tools behave on square_like, clover_like, and toast_like.
"""
from __future__ import annotations

import pytest

from voiceorder.core import tools
from voiceorder.core.cart import CartState
from voiceorder.core.ports import PosError
from voiceorder.pos_adapters.fake import FakePos


def run(pos, cart, catalog, ctx, name, **arguments):
    return tools.dispatch(
        name, cart=cart, catalog=catalog, pos=pos, ctx=ctx, arguments=arguments
    )


def add_burrito(pos, cart, catalog, ctx, ref="B1", qty=2):
    return run(
        pos, cart, catalog, ctx, "add_item",
        item_ref=ref, quantity=qty, modifier_ids=["burrito_rice_beans"],
    )


def test_full_order_flow(pos, cart, catalog, ctx):
    assert run(pos, cart, catalog, ctx, "search_menu", query="chicken burrito").ok
    result = add_burrito(pos, cart, catalog, ctx)
    assert result.ok, result.message
    line_id = result.data["line_id"]

    result = run(pos, cart, catalog, ctx, "update_item", line_id=line_id, quantity=1)
    assert result.ok

    result = run(pos, cart, catalog, ctx, "get_cart")
    assert result.ok
    assert cart.state == CartState.READ_BACK
    assert "total" in result.message.lower() or "$" in result.message
    total = result.data["totals"]["total"]
    assert total == round(9.50 * 1.0825, 2)

    result = run(
        pos, cart, catalog, ctx, "submit_order",
        customer_name="Deekshith", customer_phone="+15551234567", confirmed=True,
    )
    assert result.ok, result.message
    order = result.data["order"]
    assert order["order_number"]
    assert order["pickup_time"]
    if pos.profile == "square_like":
        assert order["payment"]["kind"] == "link"
        assert order["payment"]["url"]
        assert order["status"] == "pending_payment"
    else:
        assert order["payment"]["kind"] == "pay_at_pickup"
        assert order["status"] == "received"
    assert cart.state == CartState.SUBMITTED


def test_submit_requires_read_back_and_yes(pos, cart, catalog, ctx):
    add_burrito(pos, cart, catalog, ctx)
    result = run(
        pos, cart, catalog, ctx, "submit_order",
        customer_name="Deekshith", customer_phone="+15551234567", confirmed=True,
    )
    assert not result.ok
    assert result.error_code == "read_back_required"

    run(pos, cart, catalog, ctx, "get_cart")
    result = run(
        pos, cart, catalog, ctx, "submit_order",
        customer_name="Deekshith", customer_phone="+15551234567", confirmed=False,
    )
    assert not result.ok
    assert result.error_code == "not_confirmed"


def test_submit_needs_customer_details(pos, cart, catalog, ctx):
    add_burrito(pos, cart, catalog, ctx)
    run(pos, cart, catalog, ctx, "get_cart")
    result = run(pos, cart, catalog, ctx, "submit_order", confirmed=True)
    assert not result.ok
    assert result.error_code == "missing_customer"


def test_unknown_item(pos, cart, catalog, ctx):
    result = run(pos, cart, catalog, ctx, "add_item", item_ref="ZZ9", quantity=1)
    assert not result.ok
    assert result.error_code == "unknown_item"


def test_required_modifier_group_enforced(pos, cart, catalog, ctx):
    # Burritos require a Rice & Beans choice.
    result = run(pos, cart, catalog, ctx, "add_item", item_ref="B1", quantity=1)
    assert not result.ok
    assert result.error_code == "invalid_selection"
    assert "Rice & Beans" in result.message


def test_sold_out_item_refused(cart, catalog, ctx):
    sold_out_pos = FakePos(profile="square_like", catalog=catalog)
    sold_out_pos.mark_sold_out("B1")
    result = run(
        sold_out_pos, cart, catalog, ctx, "add_item",
        item_ref="B1", quantity=1, modifier_ids=["burrito_rice_beans"],
    )
    assert not result.ok
    assert result.error_code == "sold_out"


def test_pos_down_lands_on_backup_screen(cart, catalog, ctx):
    down_pos = FakePos(profile="square_like", catalog=catalog, mode="down")
    add_burrito(down_pos, cart, catalog, ctx)  # add fails closed on availability
    assert cart.is_empty()
    # Pretend the cart was built before the outage, then quote/submit fail.
    cart.add_line(item_ref="B1", item_name="Chicken Burrito", quantity=1, unit_price=9.50)
    result = run(down_pos, cart, catalog, ctx, "get_cart")
    assert not result.ok
    assert result.error_code == "pos_unavailable"


def test_idempotent_submit(pos, cart, catalog, ctx):
    add_burrito(pos, cart, catalog, ctx)
    run(pos, cart, catalog, ctx, "get_cart")
    args = dict(
        customer_name="Deekshith", customer_phone="+15551234567", confirmed=True,
        idempotency_key="same-key-123",
    )
    first = run(pos, cart, catalog, ctx, "submit_order", **args)
    second = run(pos, cart, catalog, ctx, "submit_order", **args)
    assert first.ok and second.ok
    assert first.data["order"]["order_id"] == second.data["order"]["order_id"]


def test_change_after_read_back_requires_new_read_back(pos, cart, catalog, ctx):
    add_burrito(pos, cart, catalog, ctx)
    run(pos, cart, catalog, ctx, "get_cart")
    line_id = cart.lines[0].line_id
    run(pos, cart, catalog, ctx, "update_item", line_id=line_id, quantity=3)
    assert cart.state == CartState.BUILDING
    result = run(
        pos, cart, catalog, ctx, "submit_order",
        customer_name="Deekshith", customer_phone="+15551234567", confirmed=True,
    )
    assert not result.ok
    assert result.error_code == "read_back_required"


def test_swap_item_on_line(pos, cart, catalog, ctx):
    result = add_burrito(pos, cart, catalog, ctx)
    line_id = result.data["line_id"]
    # "actually make one of them steak"
    result = run(
        pos, cart, catalog, ctx, "update_item",
        line_id=line_id, item_ref="B2", modifier_ids=["burrito_rice_beans"],
    )
    assert result.ok, result.message
    assert cart.lines[0].item_ref == "B2"
    assert cart.lines[0].unit_price == 10.50


def test_toast_requires_quote_before_submit(cart, catalog, ctx):
    toast = FakePos(profile="toast_like", catalog=catalog)
    cart.add_line(item_ref="B1", item_name="Chicken Burrito", quantity=1, unit_price=9.50)
    with pytest.raises(PosError):
        toast.submit(cart, "key-no-quote")
    toast.quote(cart)  # the supported path
    order = toast.submit(cart, "key-no-quote")
    assert order.status == "received"


def test_clover_rejects_unlinked_modifier(cart, catalog, ctx):
    clover = FakePos(profile="clover_like", catalog=catalog)
    line = cart.add_line(
        item_ref="B1", item_name="Chicken Burrito", quantity=1, unit_price=9.50,
        modifier_ids=["ques_guac"],  # a quesadilla modifier smuggled onto a burrito
    )
    assert line is not None
    with pytest.raises(PosError):
        clover.submit(cart, "key-bad-mod")
