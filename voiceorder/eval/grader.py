"""Grader: the final cart must equal the expected cart, exactly.

Lines are compared as a multiset keyed by (item_ref, variation, modifiers,
note) with summed quantities, so it doesn't matter whether the agent merged
two identical lines or kept them separate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.cart import Cart, CartState

MUTATING_TOOLS = {"add_item", "update_item", "remove_item"}


@dataclass
class GradeResult:
    passed: bool
    failures: list[str] = field(default_factory=list)


def _line_key(line: dict) -> tuple:
    return (
        line["item_ref"],
        line.get("variation_id") or "",
        tuple(sorted(line.get("modifier_ids") or [])),
        (line.get("note") or "").strip().lower(),
    )


def normalize_cart_lines(lines: list[dict]) -> dict[tuple, int]:
    totals: dict[tuple, int] = {}
    for line in lines:
        key = _line_key(line)
        totals[key] = totals.get(key, 0) + int(line.get("quantity", 1))
    return totals


def _read_back_rule_holds(transcript: list[dict]) -> bool:
    """A get_cart with no cart mutations between it and submit_order."""
    seq: list[str] = []
    for turn in transcript:
        for call in turn.get("tool_calls", []):
            seq.append(call["name"])
    if "submit_order" not in seq or "get_cart" not in seq:
        return False
    last_read_back = max(i for i, name in enumerate(seq) if name == "get_cart")
    submit_at = seq.index("submit_order")
    if last_read_back > submit_at:
        return False
    between = seq[last_read_back + 1 : submit_at]
    return not any(name in MUTATING_TOOLS for name in between)


def grade(script: dict, cart: Cart, transcript: list[dict]) -> GradeResult:
    failures: list[str] = []

    expected = normalize_cart_lines(script.get("expected_cart", []))
    actual = normalize_cart_lines([line.to_dict() for line in cart.lines])
    if actual != expected:
        failures.append(
            f"cart mismatch: expected {sorted(expected.items())}, "
            f"got {sorted(actual.items())}"
        )

    if script.get("expected_submit"):
        if cart.state != CartState.SUBMITTED:
            failures.append(f"expected a submitted order, cart is {cart.state.value}")
        elif not _read_back_rule_holds(transcript):
            failures.append("submit_order was not preceded by a clean read-back")
    elif cart.state == CartState.SUBMITTED:
        failures.append("order was submitted but the script expects no submit")

    if script.get("expected_transfer"):
        if not cart.transferred:
            failures.append("expected a transfer to staff, but none happened")
    elif cart.transferred:
        failures.append("call was transferred but the script expects no transfer")

    expected_total = script.get("expected_total")
    if expected_total is not None and cart.last_order:
        actual_total = cart.last_order["totals"]["total"]
        if abs(actual_total - expected_total) > 0.01:
            failures.append(
                f"total mismatch: expected ${expected_total:.2f}, got ${actual_total:.2f}"
            )

    return GradeResult(passed=not failures, failures=failures)
