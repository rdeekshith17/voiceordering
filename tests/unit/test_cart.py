"""Cart state machine tests."""
from __future__ import annotations

import pytest

from voiceorder.core.cart import CartError, CartState


def test_new_cart_is_empty(cart):
    assert cart.state == CartState.EMPTY
    assert cart.is_empty()


def test_add_moves_empty_to_building(cart):
    line = cart.add_line(item_ref="B1", item_name="Chicken Burrito", quantity=2, unit_price=9.50)
    assert line.line_id == "L1"
    assert cart.state == CartState.BUILDING
    assert cart.subtotal == 19.00


def test_line_ids_are_stable_and_unique(cart):
    a = cart.add_line(item_ref="B1", item_name="Chicken Burrito", quantity=1, unit_price=9.50)
    b = cart.add_line(item_ref="D3", item_name="Mexican Coke", quantity=1, unit_price=3.00)
    assert a.line_id != b.line_id
    assert cart.find_line(a.line_id) is a


def test_update_and_remove(cart):
    line = cart.add_line(item_ref="B1", item_name="Chicken Burrito", quantity=2, unit_price=9.50)
    cart.update_line(line.line_id, quantity=1)
    assert cart.find_line(line.line_id).quantity == 1
    assert cart.subtotal == 9.50
    cart.remove_line(line.line_id)
    assert cart.is_empty()
    assert cart.state == CartState.EMPTY


def test_remove_unknown_line_raises(cart):
    with pytest.raises(CartError):
        cart.remove_line("L99")


def test_read_back_requires_non_empty_cart(cart):
    with pytest.raises(CartError):
        cart.mark_read_back()


def test_read_back_then_confirm_then_submit(cart):
    cart.add_line(item_ref="B1", item_name="Chicken Burrito", quantity=1, unit_price=9.50)
    cart.mark_read_back()
    assert cart.state == CartState.READ_BACK
    cart.mark_confirmed()
    assert cart.state == CartState.CONFIRMED
    cart.mark_submitted({"order_id": "ord_1"})
    assert cart.state == CartState.SUBMITTED


def test_confirm_requires_read_back(cart):
    cart.add_line(item_ref="B1", item_name="Chicken Burrito", quantity=1, unit_price=9.50)
    with pytest.raises(CartError):
        cart.mark_confirmed()


def test_mutation_after_read_back_resets_state(cart):
    cart.add_line(item_ref="B1", item_name="Chicken Burrito", quantity=1, unit_price=9.50)
    cart.mark_read_back()
    cart.add_line(item_ref="D3", item_name="Mexican Coke", quantity=1, unit_price=3.00)
    # Changed order must be read back again before submit.
    assert cart.state == CartState.BUILDING


def test_cart_is_locked_after_submit(cart):
    cart.add_line(item_ref="B1", item_name="Chicken Burrito", quantity=1, unit_price=9.50)
    cart.mark_read_back()
    cart.mark_confirmed()
    cart.mark_submitted({"order_id": "ord_1"})
    with pytest.raises(CartError):
        cart.add_line(item_ref="D3", item_name="Mexican Coke", quantity=1, unit_price=3.00)


def test_cart_serialization_round_trip(cart):
    cart.add_line(
        item_ref="B2", item_name="Steak Burrito", quantity=1, unit_price=10.50,
        modifier_ids=["burrito_no_onions"], modifier_names=["No Onions"],
        note="extra spicy",
    )
    cart.mark_read_back()
    restored = type(cart).from_dict(cart.to_dict())
    assert restored.state == CartState.READ_BACK
    assert restored.subtotal == cart.subtotal
    assert restored.lines[0].modifier_names == ["No Onions"]
    assert restored.lines[0].note == "extra spicy"
    assert restored.idempotency_key == cart.idempotency_key
