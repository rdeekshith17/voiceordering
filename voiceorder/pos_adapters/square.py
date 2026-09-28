from __future__ import annotations

from ..core.ports import PosCapabilities


class SquareAdapter:
    """Parked until Phase 5. Square orders only show up once paid, so the real
    implementation submits, then generates a payment link (see build plan section 3)."""

    capabilities = PosCapabilities(unpaid_orders_visible=False, payment_link=True)

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("Square adapter is built in Phase 5")
