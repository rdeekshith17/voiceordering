"""transfer_call: hands the call to staff and freezes the cart."""
import uuid
from pathlib import Path

from voiceorder.core import tools
from voiceorder.core.cart import Cart, CartError
from voiceorder.core.catalog import Catalog
from voiceorder.core.ports import RestaurantContext
from voiceorder.pos_adapters.fake import FakePos

FIXTURE = Path(__file__).resolve().parents[2] / "voiceorder" / "fixtures" / "menu_taqueria.json"


def _env():
    catalog = Catalog.from_json(FIXTURE)
    ctx = RestaurantContext(
        restaurant_id="taqueria-demo",
        restaurant_name="Taqueria Demo",
        pos_profile="square_like",
        voice_platform="text",
        transfer_number="+15550134200",
    )
    pos = FakePos(profile="square_like", catalog=catalog)
    cart = Cart(
        cart_id="t" + uuid.uuid4().hex[:8],
        restaurant_id=ctx.restaurant_id,
        idempotency_key=uuid.uuid4().hex,
    )
    return catalog, ctx, pos, cart


def test_transfer_marks_cart_and_returns_number():
    catalog, ctx, pos, cart = _env()
    tools.dispatch("add_item", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"item_ref": "T1", "quantity": 1})
    result = tools.dispatch("transfer_call", cart=cart, catalog=catalog, pos=pos,
                            ctx=ctx, arguments={"reason": "caller asked for a person"})
    assert result.ok
    assert cart.transferred is True
    assert cart.transfer_reason == "caller asked for a person"
    assert result.data["transfer_number"] == "+15550134200"
    assert "T1" in result.data["cart_summary"] or "Chicken Taco" in result.data["cart_summary"]


def test_transfer_freezes_cart_and_blocks_submit():
    catalog, ctx, pos, cart = _env()
    tools.dispatch("transfer_call", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"reason": "too big"})
    item = catalog.get("T1")
    try:
        cart.add_line(
            item_ref=item.ref,
            item_name=item.name,
            quantity=1,
            unit_price=item.base_price,
        )
    except CartError:
        pass
    else:
        raise AssertionError("expected CartError after transfer")
    result = tools.dispatch("submit_order", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                            arguments={"customer_name": "X", "customer_phone": "1",
                                       "confirmed": True})
    assert result.ok is False
    assert result.error_code == "transferred"


def test_double_transfer_is_rejected_gracefully():
    catalog, ctx, pos, cart = _env()
    tools.dispatch("transfer_call", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"reason": "first"})
    second = tools.dispatch("transfer_call", cart=cart, catalog=catalog, pos=pos,
                            ctx=ctx, arguments={"reason": "second"})
    assert second.ok is False
    assert second.error_code == "already_transferred"


def test_transfer_in_tool_names_and_api():
    assert "transfer_call" in tools.TOOL_NAMES
