from __future__ import annotations

from voiceorder.core.cart import CallState, Cart


def test_round_trip_preserves_lines_and_state():
    cart = Cart(call_id="c1")
    cart.add_line(
        item_id="burrito-steak",
        name="Steak Burrito",
        quantity=1,
        unit_price_cents=1050,
        modifier_ids=["no-onions"],
        modifier_names=["No Onions"],
    )
    cart.mark_read_back()
    cart.customer_name = "Alex"
    cart.search_misses = 1

    restored = Cart.from_dict(cart.to_dict())

    assert restored.call_id == cart.call_id
    assert restored.state == CallState.TAKING_ORDER
    assert restored.needs_readback is False
    assert restored.customer_name == "Alex"
    assert restored.search_misses == 1
    assert [line.describe() for line in restored.lines] == [
        line.describe() for line in cart.lines
    ]


def test_round_trip_preserves_next_line_id_counter():
    cart = Cart(call_id="c1")
    cart.add_line(item_id="churros", name="Churros", quantity=1, unit_price_cents=500)
    cart.remove_line(1)

    restored = Cart.from_dict(cart.to_dict())
    new_line = restored.add_line(
        item_id="flan", name="Flan", quantity=1, unit_price_cents=450
    )

    assert new_line.line_id == 2
