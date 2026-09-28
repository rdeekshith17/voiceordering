from __future__ import annotations

import pytest

from voiceorder.core.cart import Cart
from voiceorder.core.tools import OrderTools, ToolError
from voiceorder.pos_adapters.fake import PROFILES, FakePos

PROFILE_NAMES = list(PROFILES)


def make_tools(catalog, profile: str, call_id: str = "call-1") -> OrderTools:
    pos = FakePos(profile=profile, catalog=catalog)
    return OrderTools(cart=Cart(call_id=call_id), catalog=catalog, pos=pos)


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_add_item_prices_from_catalog(catalog, profile):
    tools = make_tools(catalog, profile)
    result = tools.add_item(item_ref="burrito-chicken", quantity=2)
    assert result["cart"]["lines"][0]["quantity"] == 2
    assert result["cart"]["subtotal_cents"] == 1900


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_add_item_with_size_and_modifiers(catalog, profile):
    tools = make_tools(catalog, profile)
    result = tools.add_item(
        item_ref="horchata", variation_ref="large", modifier_refs=[]
    )
    assert result["cart"]["subtotal_cents"] == 450


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_add_item_unknown_ref_raises(catalog, profile):
    tools = make_tools(catalog, profile)
    with pytest.raises(ToolError):
        tools.add_item(item_ref="pizza-slice")


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_add_item_quantity_over_limit_raises(catalog, profile):
    tools = make_tools(catalog, profile)
    with pytest.raises(ToolError):
        tools.add_item(item_ref="burrito-chicken", quantity=999)


def test_clover_rejects_modifiers_not_linked_to_item(catalog):
    tools = make_tools(catalog, "clover_like")
    with pytest.raises(ToolError):
        tools.add_item(item_ref="water", modifier_refs=["no-onions"])


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_update_item_changes_quantity(catalog, profile):
    tools = make_tools(catalog, profile)
    add_result = tools.add_item(item_ref="burrito-steak", quantity=1)
    line_id = add_result["line_id"]
    update_result = tools.update_item(line_id=line_id, quantity=3)
    assert update_result["cart"]["lines"][0]["quantity"] == 3


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_update_item_changes_size(catalog, profile):
    tools = make_tools(catalog, profile)
    add_result = tools.add_item(item_ref="coke", variation_ref="small")
    line_id = add_result["line_id"]
    update_result = tools.update_item(line_id=line_id, variation_ref="large")
    assert update_result["cart"]["subtotal_cents"] == 325


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_remove_item(catalog, profile):
    tools = make_tools(catalog, profile)
    add_result = tools.add_item(item_ref="churros")
    tools.remove_item(line_id=add_result["line_id"])
    assert tools.cart.is_empty()


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_get_cart_returns_readback_text_and_clears_flag(catalog, profile):
    tools = make_tools(catalog, profile)
    tools.add_item(item_ref="churros")
    result = tools.get_cart()
    assert "readback_text" in result
    assert tools.cart.needs_readback is False


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_submit_order_requires_readback(catalog, profile):
    tools = make_tools(catalog, profile)
    tools.add_item(item_ref="churros")
    with pytest.raises(ToolError):
        tools.submit_order(customer_name="Alex", confirmed=True)


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_submit_order_requires_confirmation(catalog, profile):
    tools = make_tools(catalog, profile)
    tools.add_item(item_ref="churros")
    tools.get_cart()
    with pytest.raises(ToolError):
        tools.submit_order(customer_name="Alex", confirmed=False)


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_submit_order_succeeds_after_readback_and_yes(catalog, profile):
    tools = make_tools(catalog, profile)
    tools.add_item(item_ref="churros")
    tools.get_cart()
    result = tools.submit_order(customer_name="Alex", confirmed=True)
    assert result["status"] == "confirmed"
    assert result["order_id"]
    assert result["payment"]["kind"] in {"link", "pay_at_pickup"}


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_submit_order_rejects_empty_cart(catalog, profile):
    tools = make_tools(catalog, profile)
    with pytest.raises(ToolError):
        tools.submit_order(customer_name="Alex", confirmed=True)


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_large_order_is_rejected_for_transfer(catalog, profile):
    tools = make_tools(catalog, profile)
    tools.max_total_cents = 1000
    tools.add_item(item_ref="burrito-steak", quantity=5)
    tools.get_cart()
    with pytest.raises(ToolError):
        tools.submit_order(customer_name="Alex", confirmed=True)
