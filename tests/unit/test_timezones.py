"""Each restaurant's time zone drives the pickup times callers hear."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

from voiceorder.api import main as api_main
from voiceorder.api.storage import InMemoryOrderStore
from voiceorder.core.ports import local_now
from voiceorder.pos_adapters.fake import FakePos
from voiceorder.portal.portal import PortalDeps, build_portal_router
from voiceorder.tenants.store import TenantStore


def _expected(zone: str, minutes: int = 20) -> set[str]:
    """Pickup string for now (and a minute later, in case the clock ticks)."""
    now = datetime.now(ZoneInfo(zone))
    return {(now + timedelta(minutes=minutes + d)).strftime("%-I:%M %p") for d in (0, 1)}


def _submit(pos, cart):
    pos.quote(cart)
    return pos.submit(cart, idempotency_key="k1")


def test_local_now_uses_zone_and_survives_bad_values():
    assert local_now("Asia/Kolkata").utcoffset() == timedelta(hours=5, minutes=30)
    assert local_now("").tzinfo is None and local_now("Not/AZone").tzinfo is None


def test_same_order_gets_each_restaurants_local_pickup_time(catalog, cart, ctx):
    from voiceorder.core import tools

    tools.add_item(cart, catalog, FakePos(catalog=catalog), ctx, item_ref="T1")
    for zone in ("America/New_York", "America/Los_Angeles", "Asia/Kolkata"):
        cart_copy = type(cart).from_dict(cart.to_dict())
        order = _submit(FakePos(catalog=catalog, timezone=zone), cart_copy)
        assert order.pickup_time in _expected(zone), zone


def test_tenant_adapter_follows_the_tenant_zone_and_rebuilds_on_change():
    store = api_main.tenant_store
    t = store.create_tenant("Zone Test Grill")
    store.set_settings(t.id, {"pos_profile": "square_like", "timezone": "America/Denver"})
    first = api_main.cached_tenant_adapter(store.get_tenant(t.id))
    assert first._timezone == "America/Denver"
    assert api_main.cached_tenant_adapter(store.get_tenant(t.id)) is first  # cached
    store.set_settings(t.id, {"timezone": "Pacific/Honolulu"})
    second = api_main.cached_tenant_adapter(store.get_tenant(t.id))
    assert second is not first and second._timezone == "Pacific/Honolulu"


def _portal(tmp_path):
    store = TenantStore(tmp_path / "p.db")
    app = FastAPI()
    app.include_router(build_portal_router(PortalDeps(
        tenants=store, order_store=InMemoryOrderStore(),
        build_adapter=lambda t, o: None, get_catalog=lambda t: None)))
    return TestClient(app, follow_redirects=False), store


def test_signup_saves_zone_and_settings_shows_it(tmp_path):
    client, store = _portal(tmp_path)
    page = client.get("/portal/signup").text
    assert "name=timezone" in page and "Central Time (Chicago" in page
    client.post("/portal/signup", data={"restaurant": "Aloha Poke", "phone": "+18085550100",
                                        "timezone": "Pacific/Honolulu",
                                        "email": "a@example.com", "password": "password123"})
    t = store.get_tenant_by_name("Aloha Poke")
    assert t.setting("timezone") == "Pacific/Honolulu"
    settings = client.get("/portal/settings").text
    assert "<option value='Pacific/Honolulu' selected>Hawaii (Honolulu)</option>" in settings
    assert "there now" in settings
    bad = client.post("/portal/api/settings", json={"timezone": "Mars/Base"})
    assert bad.status_code == 400
    assert client.post("/portal/api/settings", json={"timezone": "Asia/Dubai"}).json()["ok"]
    assert store.get_tenant(t.id).setting("timezone") == "Asia/Dubai"
