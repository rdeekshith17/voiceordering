from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from voiceorder.core.backup import BackupScreen
from voiceorder.core.cart import CallState, Cart
from voiceorder.core.ports import PaymentStep, PosCapabilities, PosOrder, Totals
from voiceorder.core.tools import OrderTools, ToolError
from voiceorder.pos_adapters.fake import FakePos, FakePosTimeout

# Each test name below maps to a row in the build plan's section 8 table.


@dataclass
class FlakyPos:
    """Fails once, then succeeds -- for exercising the retry path deterministically."""

    catalog: object
    attempts: int = 0
    _orders: dict = field(default_factory=dict)
    capabilities: PosCapabilities = field(
        default_factory=lambda: PosCapabilities(unpaid_orders_visible=False, payment_link=True)
    )

    def sync_catalog(self, restaurant_id):
        return self.catalog

    def quote(self, cart):
        subtotal = sum(line.line_total_cents for line in cart.lines)
        return Totals(subtotal_cents=subtotal, tax_cents=0, total_cents=subtotal)

    def submit(self, cart, idempotency_key):
        self.attempts += 1
        if self.attempts == 1:
            raise FakePosTimeout("first attempt times out")
        order = PosOrder(order_id="flaky-1", pickup_time="15 minutes", raw={})
        self._orders[idempotency_key] = order
        return order

    def payment_step(self, order):
        return PaymentStep(kind="link", detail=f"https://pay.example.test/{order.order_id}")


def _tools_with(pos, backup=None, call_id="c1") -> OrderTools:
    return OrderTools(cart=Cart(call_id=call_id), catalog=pos.catalog, pos=pos, backup=backup)


def test_two_search_misses_suggest_transfer(catalog):
    tools = _tools_with(FakePos(profile="square_like", catalog=catalog))
    first = tools.search_menu("pepperoni pizza")
    assert first["suggest_transfer"] is False
    second = tools.search_menu("pepperoni pizza")
    assert second["suggest_transfer"] is True


def test_a_real_match_resets_the_miss_counter(catalog):
    tools = _tools_with(FakePos(profile="square_like", catalog=catalog))
    tools.search_menu("pepperoni pizza")
    tools.search_menu("horchata")
    assert tools.cart.search_misses == 0


def test_transfer_call_logs_to_backup_and_sets_state(catalog):
    backup = BackupScreen()
    pos = FakePos(profile="square_like", catalog=catalog)
    tools = OrderTools(
        cart=Cart(call_id="c1"),
        catalog=catalog,
        pos=pos,
        backup=backup,
        transfer_number="+15557654321",
    )
    tools.add_item(item_ref="churros")
    result = tools.transfer_call(reason="caller asked for a person")

    assert result["transferred"] is True
    assert result["transfer_number"] == "+15557654321"
    assert tools.cart.state == CallState.TRANSFERRED
    entries = backup.list_entries(kind="transferred")
    assert len(entries) == 1
    assert "caller asked for a person" in entries[0].detail


def test_submit_retries_once_then_succeeds(catalog):
    pos = FlakyPos(catalog=catalog)
    tools = _tools_with(pos)
    tools.add_item(item_ref="churros")
    tools.get_cart()

    result = tools.submit_order(customer_name="Alex", confirmed=True)

    assert result["status"] == "confirmed"
    assert pos.attempts == 2


def test_submit_still_failing_after_retry_falls_back_to_backup_screen(catalog):
    backup = BackupScreen()
    pos = FakePos(profile="square_like", catalog=catalog, failure_mode="down")
    tools = _tools_with(pos, backup=backup)
    tools.add_item(item_ref="churros")
    tools.get_cart()

    result = tools.submit_order(customer_name="Alex", confirmed=True)

    assert result["status"] == "pending_confirmation"
    assert "saved your order" in result["message"]
    failed = backup.list_entries(kind="failed")
    assert len(failed) == 1
    assert tools.cart.state == CallState.SUBMITTED


def test_square_like_submit_logs_unpaid_to_backup_screen(catalog):
    backup = BackupScreen()
    pos = FakePos(profile="square_like", catalog=catalog)
    tools = _tools_with(pos, backup=backup)
    tools.add_item(item_ref="churros")
    tools.get_cart()

    tools.submit_order(customer_name="Alex", confirmed=True)

    unpaid = backup.list_entries(kind="unpaid")
    assert len(unpaid) == 1


def test_pay_at_pickup_profiles_do_not_log_unpaid(catalog):
    backup = BackupScreen()
    pos = FakePos(profile="clover_like", catalog=catalog)
    tools = _tools_with(pos, backup=backup)
    tools.add_item(item_ref="churros")
    tools.get_cart()

    tools.submit_order(customer_name="Alex", confirmed=True)

    assert backup.list_entries(kind="unpaid") == []


def test_hangup_mid_order_never_reaches_the_pos(catalog):
    backup = BackupScreen()
    pos = FakePos(profile="square_like", catalog=catalog)
    tools = _tools_with(pos, backup=backup)
    tools.add_item(item_ref="burrito-steak", quantity=2)
    tools.update_item(line_id=1, quantity=1)
    # the call just ends here -- no get_cart, no submit_order

    assert pos._orders == {}
    assert backup.list_entries() == []


def test_add_item_has_no_way_to_pass_a_custom_price(catalog):
    """Prompt injection defense (section 8): prices only ever come from the
    catalog -- there's no price/discount parameter for a caller to talk the
    agent into using."""
    tools = _tools_with(FakePos(profile="square_like", catalog=catalog))
    with pytest.raises(TypeError):
        tools.add_item(item_ref="churros", price_cents=0)
