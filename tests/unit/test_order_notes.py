"""Item options and the caller's notes show on the portal's orders and CSV."""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from voiceorder.api.storage import InMemoryOrderStore
from voiceorder.portal.portal import PortalDeps, build_portal_router
from voiceorder.tenants.store import TenantStore


def test_orders_show_options_and_notes_safely(tmp_path):
    store, orders = TenantStore(tmp_path / "n.db"), InMemoryOrderStore()
    app = FastAPI()
    app.include_router(build_portal_router(PortalDeps(
        tenants=store, order_store=orders, build_adapter=lambda t, o: None, get_catalog=lambda t: None)))
    c = TestClient(app, follow_redirects=False)
    c.post("/portal/signup", data={"restaurant": "Hyderabad House", "phone": "+15622680097",
                                   "email": "a@example.com", "password": "password123"})
    t = store.get_tenant_by_name("Hyderabad House")
    orders.save({"order_id": "o1", "restaurant_id": "x", "tenant_id": t.id, "order_number": "AUZ8YY",
                 "status": "pending_payment", "totals": {"total": 18.39}, "saved_at": time.time(),
                 "lines": [{"item_name": "Chicken Biryani", "quantity": 1, "note": "extra spicy, no onions"},
                           {"item_name": "Horchata", "quantity": 2, "variation_name": "Large",
                            "modifier_names": ["No ice"], "note": "<b>hi</b>"}]})
    page = c.get("/portal/orders").text
    assert "Chicken Biryani" in page and "extra spicy, no onions" in page
    assert "Horchata (Large) (×2)" in page and "with No ice · &lt;b&gt;hi&lt;/b&gt;" in page
    assert "<b>hi</b>" not in page                      # caller text can't inject HTML
    assert "extra spicy, no onions" in c.get("/portal/").text  # dashboard too
    csv = c.get("/portal/api/orders.csv").text
    assert "Chicken Biryani [extra spicy, no onions]" in csv
