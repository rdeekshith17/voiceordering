"""Tests for the portal Support / Marketing / Usage pages.

Follows the portal test pattern in test_multitenant.py: FastAPI app +
build_portal_router with a fake adapter, signup to get a session.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voiceorder.api.storage import InMemoryOrderStore
from voiceorder.portal.portal import PortalDeps, build_portal_router
from voiceorder.tenants.store import TenantStore


class _FakeAdapter:
    def ping(self):
        return {"ok": True, "location_name": "Test Location"}


def _portal_client(tmp_path):
    store = TenantStore(tmp_path / "portal.db")
    deps = PortalDeps(
        tenants=store,
        order_store=InMemoryOrderStore(),
        build_adapter=lambda tenant, override: _FakeAdapter(),
        get_catalog=lambda tenant: None,
    )
    app = FastAPI()
    app.include_router(build_portal_router(deps))
    return TestClient(app, follow_redirects=False), store


def _login_client(tmp_path):
    client, store = _portal_client(tmp_path)
    r = client.post("/portal/signup", data={
        "restaurant": "Taco Palace", "phone": "+15551234567",
        "email": "owner@example.com", "password": "s3cur3pass"})
    assert r.status_code == 302
    tenant = store.get_tenant_by_name("Taco Palace")
    return client, store, tenant


# --- pages --------------------------------------------------------------------
@pytest.mark.parametrize("path,key", [
    ("/portal/support", "support"),
    ("/portal/marketing", "marketing"),
    ("/portal/usage", "usage"),
])
def test_new_pages_require_login_and_render(tmp_path, path, key):
    client, _ = _portal_client(tmp_path)
    r = client.get(path)
    assert r.status_code == 302 and r.headers["location"] == "/portal/login"
    r = client.get(f"/portal/api/{'tickets' if key == 'support' else key}")
    assert r.status_code == 401

    client, _, _ = _login_client(tmp_path)
    r = client.get(path)
    assert r.status_code == 200
    for link in ("/portal/support", "/portal/marketing", "/portal/usage"):
        assert link in r.text  # nav links present


def test_support_page_lists_tickets_and_ack_flow(tmp_path):
    client, store, tenant = _login_client(tmp_path)
    t = store.open_ticket(tenant.id, "pos_down", "critical",
                          "Square connection down", "ping failed")
    assert t
    r = client.get("/portal/support")
    assert r.status_code == 200
    assert "Square connection down" in r.text
    assert "Acknowledge" in r.text
    # ack via the form endpoint -> redirect back to the support page
    r = client.post(f"/portal/api/tickets/{t['id']}/ack")
    assert r.status_code == 302 and r.headers["location"] == "/portal/support"
    assert store.get_ticket(t["id"])["status"] == "acked"
    # acking someone else's ticket 404s
    r = client.post("/portal/api/tickets/does-not-exist/ack")
    assert r.status_code == 404


def test_support_page_shows_platform_tickets_without_ack(tmp_path):
    client, store, tenant = _login_client(tmp_path)
    store.open_ticket(None, "platform_down", "critical",
                      "Platform unreachable", "tunnel down")
    r = client.get("/portal/support")
    assert "Platform unreachable" in r.text
    assert "Platform" in r.text


def test_marketing_page_lists_drafts_with_channel_badges(tmp_path):
    client, store, tenant = _login_client(tmp_path)
    store.save_draft(tenant.id, "Order ahead", "Call us!", "sms")
    store.save_draft(tenant.id, "Staff pick", "Ask your server", "in-store")
    r = client.get("/portal/marketing")
    assert r.status_code == 200
    assert "Order ahead" in r.text and "sms" in r.text
    assert "in-store" in r.text


def test_usage_page_lists_daily_rows(tmp_path):
    client, store, tenant = _login_client(tmp_path)
    store.upsert_usage(tenant.id, "2026-09-28", 4, 12.5, 900, 2)
    r = client.get("/portal/usage")
    assert r.status_code == 200
    assert "2026-09-28" in r.text
    assert ">4<" in r.text


# --- JSON APIs -----------------------------------------------------------------
def test_portal_json_apis(tmp_path):
    client, store, tenant = _login_client(tmp_path)
    store.open_ticket(tenant.id, "call_failures", "warning", "1 failed call", "")
    store.save_draft(tenant.id, "Promo", "Body", "social")
    store.upsert_usage(tenant.id, "2026-09-28", 1, 2.0, 100, 0)
    for path, key in [("/portal/api/tickets", "tickets"),
                      ("/portal/api/marketing", "drafts"),
                      ("/portal/api/usage", "usage")]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert len(r.json()[key]) == 1, path
