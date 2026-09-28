from __future__ import annotations

import pytest

from voiceorder.core.cart import Cart, CartError


def test_add_line_assigns_increasing_line_ids():
    cart = Cart(call_id="c1")
    first = cart.add_line(item_id="a", name="A", quantity=1, unit_price_cents=100)
    second = cart.add_line(item_id="b", name="B", quantity=1, unit_price_cents=200)
    assert (first.line_id, second.line_id) == (1, 2)


def test_add_line_marks_needs_readback():
    cart = Cart(call_id="c1")
    assert cart.needs_readback is False
    cart.add_line(item_id="a", name="A", quantity=1, unit_price_cents=100)
    assert cart.needs_readback is True


def test_mark_read_back_clears_flag_until_next_change():
    cart = Cart(call_id="c1")
    line = cart.add_line(item_id="a", name="A", quantity=1, unit_price_cents=100)
    cart.mark_read_back()
    assert cart.needs_readback is False
    cart.update_line(line.line_id, quantity=2)
    assert cart.needs_readback is True


def test_remove_line_by_id():
    cart = Cart(call_id="c1")
    line = cart.add_line(item_id="a", name="A", quantity=1, unit_price_cents=100)
    cart.remove_line(line.line_id)
    assert cart.is_empty()


def test_unknown_line_id_raises():
    cart = Cart(call_id="c1")
    with pytest.raises(CartError):
        cart.line(999)
