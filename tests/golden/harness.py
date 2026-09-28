from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from voiceorder.core.cart import Cart
from voiceorder.core.catalog import Catalog
from voiceorder.core.tools import OrderTools
from voiceorder.pos_adapters.fake import FakePos


@dataclass
class Step:
    """One tool call a scripted caller would trigger. The real agent (Phase 2)
    decides these from natural language; here they're fixed so the check is
    exact and doesn't depend on an LLM."""

    tool: str
    arguments: dict[str, Any]


@dataclass
class ExpectedLine:
    description: str
    quantity: int


def run_script(catalog: Catalog, profile: str, steps: list[Step]) -> OrderTools:
    pos = FakePos(profile=profile, catalog=catalog)
    tools = OrderTools(cart=Cart(call_id=f"golden-{profile}"), catalog=catalog, pos=pos)
    for step in steps:
        getattr(tools, step.tool)(**step.arguments)
    return tools


def assert_final_cart(tools: OrderTools, expected: list[ExpectedLine]) -> None:
    actual = [
        ExpectedLine(description=line.describe(), quantity=line.quantity)
        for line in tools.cart.lines
    ]
    assert actual == expected, f"expected {expected}, got {actual}"
