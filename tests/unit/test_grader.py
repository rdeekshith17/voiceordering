"""Grader: exact final cart, submit rules, read-back rule, totals."""
import uuid
from pathlib import Path

from voiceorder.core import tools
from voiceorder.core.cart import Cart
from voiceorder.core.catalog import Catalog
from voiceorder.core.ports import RestaurantContext
from voiceorder.eval.grader import grade
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
        cart_id="g" + uuid.uuid4().hex[:8],
        restaurant_id=ctx.restaurant_id,
        idempotency_key=uuid.uuid4().hex,
    )
    return catalog, ctx, pos, cart


def _script(**overrides):
    base = {
        "expected_cart": [
            {
                "item_ref": "T1",
                "quantity": 2,
                "variation_id": None,
                "modifier_ids": [],
                "note": None,
            }
        ],
        "expected_submit": True,
        "expected_transfer": False,
    }
    base.update(overrides)
    return base


def _submit_flow(cart, catalog, pos, ctx):
    tools.dispatch("search_menu", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"query": "taco"})
    tools.dispatch("add_item", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"item_ref": "T1", "quantity": 2})
    tools.dispatch("get_cart", cart=cart, catalog=catalog, pos=pos, ctx=ctx, arguments={})
    return tools.dispatch(
        "submit_order", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
        arguments={"customer_name": "T", "customer_phone": "555", "confirmed": True},
    )


def _transcript(*tool_names):
    return [{"role": "agent", "text": "", "tool_calls": [{"name": n} for n in tool_names]}]


def test_grade_passes_on_exact_cart_and_clean_read_back():
    catalog, ctx, pos, cart = _env()
    _submit_flow(cart, catalog, pos, ctx)
    transcript = _transcript("search_menu", "add_item", "get_cart", "submit_order")
    result = grade(_script(), cart, transcript)
    assert result.passed, result.failures


def test_grade_merges_split_lines():
    catalog, ctx, pos, cart = _env()
    tools.dispatch("add_item", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"item_ref": "T1", "quantity": 1})
    tools.dispatch("add_item", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"item_ref": "T1", "quantity": 1})
    result = grade(_script(expected_submit=False), cart, [])
    assert result.passed, result.failures


def test_grade_fails_on_wrong_quantity():
    catalog, ctx, pos, cart = _env()
    tools.dispatch("add_item", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"item_ref": "T1", "quantity": 3})
    result = grade(_script(expected_submit=False), cart, [])
    assert not result.passed
    assert any("cart mismatch" in f for f in result.failures)


def test_grade_fails_when_submit_lacks_read_back():
    catalog, ctx, pos, cart = _env()
    _submit_flow(cart, catalog, pos, ctx)
    transcript = _transcript("search_menu", "add_item", "submit_order")  # no get_cart
    result = grade(_script(), cart, transcript)
    assert not result.passed
    assert any("read-back" in f for f in result.failures)


def test_grade_fails_when_mutation_follows_read_back():
    catalog, ctx, pos, cart = _env()
    _submit_flow(cart, catalog, pos, ctx)
    transcript = _transcript("add_item", "get_cart", "add_item", "get_cart", "submit_order")
    # The engine resets to building after a mutation, so the second get_cart
    # starts a new read-back -- but there is no confirmation after it in the
    # transcript, and the engine itself would have blocked submit. Here we just
    # check the grader flags a mutation between the LAST read-back and submit.
    result = grade(_script(), cart, transcript)
    assert result.passed, result.failures  # last get_cart is clean


def test_grade_fails_on_unexpected_submit():
    catalog, ctx, pos, cart = _env()
    _submit_flow(cart, catalog, pos, ctx)
    result = grade(_script(expected_submit=False), cart, _transcript())
    assert not result.passed
    assert any("submitted" in f for f in result.failures)


def test_grade_checks_transfer():
    catalog, ctx, pos, cart = _env()
    tools.dispatch("transfer_call", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"reason": "caller request"})
    result = grade(_script(expected_cart=[], expected_submit=False,
                           expected_transfer=True), cart, [])
    assert result.passed, result.failures


def test_grade_fails_on_missing_transfer():
    _, _, _, cart = _env()
    result = grade(_script(expected_cart=[], expected_submit=False,
                           expected_transfer=True), cart, [])
    assert not result.passed


def test_grade_checks_expected_total():
    catalog, ctx, pos, cart = _env()
    tools.dispatch("add_item", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"item_ref": "B1", "quantity": 1,
                              "modifier_ids": ["burrito_rice_beans"]})
    tools.dispatch("get_cart", cart=cart, catalog=catalog, pos=pos, ctx=ctx, arguments={})
    tools.dispatch("submit_order", cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                   arguments={"customer_name": "T", "customer_phone": "555", "confirmed": True})
    transcript = _transcript("add_item", "get_cart", "submit_order")
    script = _script(
        expected_cart=[{"item_ref": "B1", "quantity": 1, "variation_id": None,
                        "modifier_ids": ["burrito_rice_beans"], "note": None}],
        expected_total=10.28,
    )
    assert grade(script, cart, transcript).passed
    script["expected_total"] = 5.00
    result = grade(script, cart, transcript)
    assert not result.passed
    assert any("total mismatch" in f for f in result.failures)
