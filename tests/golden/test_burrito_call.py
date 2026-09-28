from __future__ import annotations

import pytest

from voiceorder.pos_adapters.fake import PROFILES

from .harness import ExpectedLine, Step, assert_final_cart, run_script

# The example golden script from the build plan (section 7):
#   two chicken burritos
#   actually make one of them steak
#   no onions on the steak one
#   add a large horchata... is it dairy-free?
#   never mind, make it a Coke
#   yes, that's right
BURRITO_CALL_STEPS = [
    Step("add_item", {"item_ref": "burrito-chicken", "quantity": 2}),
    Step("update_item", {"line_id": 1, "quantity": 1}),
    Step("add_item", {"item_ref": "burrito-steak", "quantity": 1}),
    Step("update_item", {"line_id": 2, "modifier_refs": ["no-onions"]}),
    Step("add_item", {"item_ref": "horchata", "variation_ref": "large"}),
    Step("remove_item", {"line_id": 3}),
    Step("add_item", {"item_ref": "coke", "variation_ref": "large"}),
    Step("get_cart", {}),
    Step("submit_order", {"customer_name": "Alex", "confirmed": True}),
]

EXPECTED_CART = [
    ExpectedLine(description="1 x Chicken Burrito", quantity=1),
    ExpectedLine(description="1 x Steak Burrito [No Onions]", quantity=1),
    ExpectedLine(description="1 x Coke (Large)", quantity=1),
]


@pytest.mark.parametrize("profile", list(PROFILES))
@pytest.mark.parametrize("run", range(5))
def test_burrito_call_ends_with_expected_cart(catalog, profile, run):
    tools = run_script(catalog, profile, BURRITO_CALL_STEPS)
    assert_final_cart(tools, EXPECTED_CART)
    assert len(tools.pos._orders) == 1, "submit_order should reach the POS exactly once"
