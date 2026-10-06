"""Phase 4 gate: failure modes never lose or duplicate an order."""
import uuid
from pathlib import Path

import pytest

from voiceorder.core import tools
from voiceorder.core.cart import Cart, CartState
from voiceorder.core.catalog import Catalog
from voiceorder.core.ports import RestaurantContext
from voiceorder.pos_adapters.fake import FakePos

FIXTURE = Path(__file__).resolve().parents[2] / "voiceorder" / "fixtures" / "menu_taqueria.json"
MODES = ["ok", "slow", "flaky"]


def _env(mode: str = "ok", profile: str = "square_like"):
    catalog = Catalog.from_json(FIXTURE)
    ctx = RestaurantContext(
        restaurant_id="taqueria-demo",
        restaurant_name="Taqueria Demo",
        pos_profile=profile,
        voice_platform="text",
        transfer_number="+15550134200",
    )
    pos = FakePos(profile=profile, catalog=catalog, mode=mode)
    return catalog, ctx, pos


def _cart(ctx: RestaurantContext) -> Cart:
    return Cart(
        cart_id="f" + uuid.uuid4().hex[:8],
        restaurant_id=ctx.restaurant_id,
        idempotency_key=uuid.uuid4().hex,
    )


def _ready_cart(catalog, pos, ctx, cart=None):
    """Cart with 2 chicken tacos, read back, ready to submit."""
    cart = cart or _cart(ctx)
    assert tools.dispatch(
        "add_item", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
        arguments={"item_ref": "T1", "quantity": 2},
    ).ok
    tools.dispatch("get_cart", cart=cart, catalog=catalog, pos=pos, ctx=ctx, arguments={})
    assert cart.state == CartState.READ_BACK
    return cart


def _submit(cart, catalog, pos, ctx, key=None):
    return tools.dispatch(
        "submit_order", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
        arguments={
            "customer_name": "T",
            "customer_phone": "555-123-4567",
            "confirmed": True,
            "idempotency_key": key or cart.idempotency_key,
        },
    )


@pytest.mark.parametrize("mode", MODES)
def test_submit_succeeds_across_modes(mode):
    catalog, ctx, pos = _env("ok")  # build the cart while the POS is healthy
    cart = _ready_cart(catalog, pos, ctx)
    pos.mode = mode  # then the failure hits mid-call
    result = _submit(cart, catalog, pos, ctx)
    # flaky fails exactly once; the retry is part of the test
    if not result.ok:
        assert pos.mode == "flaky"
        result = _submit(cart, catalog, pos, ctx)
    assert result.ok, result.error_code
    assert cart.state == CartState.SUBMITTED
    assert result.data["order"]["totals"]["total"] > 0


def test_double_submit_same_cart_makes_one_order():
    catalog, ctx, pos = _env()
    cart = _ready_cart(catalog, pos, ctx)
    first = _submit(cart, catalog, pos, ctx)
    second = _submit(cart, catalog, pos, ctx)
    assert first.ok and second.ok
    assert first.data["order"]["order_id"] == second.data["order"]["order_id"]
    assert len(pos._orders) == 1


def test_retry_with_new_cart_same_key_makes_one_pos_order():
    """A webhook retried with a fresh cart object but the same key: one order."""
    catalog, ctx, pos = _env()
    key = uuid.uuid4().hex
    cart1 = _ready_cart(catalog, pos, ctx)
    cart2 = _ready_cart(catalog, pos, ctx)
    first = _submit(cart1, catalog, pos, ctx, key=key)
    second = _submit(cart2, catalog, pos, ctx, key=key)
    assert first.ok and second.ok
    assert first.data["order"]["order_id"] == second.data["order"]["order_id"]
    assert len(pos._orders) == 1


def test_pos_down_parks_order_then_recover_succeeds():
    catalog, ctx, pos = _env("ok")
    cart = _ready_cart(catalog, pos, ctx)
    pos.mode = "down"
    failed = _submit(cart, catalog, pos, ctx)
    assert failed.ok is False
    assert failed.error_code == "pos_unavailable"
    assert failed.data["backup"] is True
    assert cart.state == CartState.READ_BACK  # still submittable
    pos.mode = "ok"
    retried = _submit(cart, catalog, pos, ctx)
    assert retried.ok, retried.error_code
    assert len(pos._orders) == 1


def test_sold_out_mid_call_blocks_submit_gracefully():
    catalog, ctx, pos = _env("ok")
    cart = _ready_cart(catalog, pos, ctx)
    catalog.set_available("T1", False)  # 86'd after the cart was built
    result = _submit(cart, catalog, pos, ctx)
    assert result.ok is False
    assert result.error_code == "pos_unavailable"
    assert cart.state == CartState.READ_BACK


def test_runtime_86_blocks_add_item_until_restored():
    catalog, ctx, pos = _env("ok")
    cart = _cart(ctx)
    catalog.set_available("T1", False)
    blocked = tools.dispatch(
        "add_item", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
        arguments={"item_ref": "T1", "quantity": 1},
    )
    assert blocked.ok is False
    assert blocked.error_code == "sold_out"
    catalog.set_available("T1", True)
    allowed = tools.dispatch(
        "add_item", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
        arguments={"item_ref": "T1", "quantity": 1},
    )
    assert allowed.ok


def test_set_available_rejects_unknown_ref():
    catalog, _, _ = _env()
    assert catalog.set_available("NOPE", False) is False


@pytest.mark.parametrize("profile", ["square_like", "clover_like", "toast_like"])
def test_failure_modes_hold_on_all_profiles(profile):
    catalog, ctx, pos = _env("ok", profile)
    cart = _ready_cart(catalog, pos, ctx)
    pos.mode = "flaky"
    result = _submit(cart, catalog, pos, ctx)
    if not result.ok:
        result = _submit(cart, catalog, pos, ctx)
    assert result.ok, (profile, result.error_code)
