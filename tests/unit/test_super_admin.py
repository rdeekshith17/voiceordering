"""PR 4: Super Admin dashboard — separate login, every restaurant, audited changes."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voiceorder.admin import admin as admin_mod
from voiceorder.admin.admin import AdminDeps, build_admin_router
from voiceorder.api.storage import InMemoryOrderStore
from voiceorder.portal.portal import PortalDeps, build_portal_router
from voiceorder.tenants import crypto
from voiceorder.tenants.store import TenantStore

PW = "a-long-admin-password"


@pytest.fixture()
def env(tmp_path):
    admin_mod._FAILED_LOGINS.clear()
    store = TenantStore(tmp_path / "a.db")
    app = FastAPI()
    app.include_router(build_admin_router(AdminDeps(tenants=store)))
    app.include_router(build_portal_router(PortalDeps(
        tenants=store, order_store=InMemoryOrderStore(),
        build_adapter=lambda t, o: None, get_catalog=lambda t: None)))
    store.create_platform_user("ops@voiceorder.ai", PW, "super_admin")
    hh = store.create_tenant("Hyderabad House", "+15622680097")
    store.create_user(hh.id, "owner@hh.test", "password123")
    store.set_settings(hh.id, {"pos_profile": "square", "timezone": "America/Chicago"})
    store.set_secret(hh.id, "square", {"access_token": "EAAA-SECRET-TOKEN", "location_id": "L1"})
    return app, store, hh


def admin_client(app):
    c = TestClient(app, follow_redirects=False)
    r = c.post("/admin/login", data={"email": "ops@voiceorder.ai", "password": PW})
    assert r.status_code == 302 and r.headers["location"] == "/admin/"
    return c


def owner_client(app):
    c = TestClient(app, follow_redirects=False)
    c.post("/portal/login", data={"email": "owner@hh.test", "password": "password123"})
    return c


# --- access ------------------------------------------------------------------------------
def test_admin_pages_load_and_never_show_pos_secrets(env):
    app, store, hh = env
    c = admin_client(app)
    for path in ("/admin/", "/admin/restaurants", f"/admin/restaurants/{hh.id}",
                 "/admin/operations", "/admin/rollout", "/admin/usage", "/admin/audit"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert "EAAA-SECRET-TOKEN" not in r.text, path
    detail = c.get(f"/admin/restaurants/{hh.id}").text
    assert "Hyderabad House" in detail and "access_token, location_id; values hidden" in detail
    assert c.get("/admin/api/health").json()["restaurants"] == 1


def test_restaurant_logins_and_admin_logins_never_mix(env):
    app, store, hh = env
    owner = owner_client(app)
    assert owner.get("/admin/").headers["location"] == "/admin/login"
    assert owner.post("/admin/api/emergency", json={"off": True}).status_code == 401
    assert store.platform_emergency() is False
    # A restaurant login can't log in at /admin, and an admin can't log in to the portal.
    r = TestClient(app, follow_redirects=False).post(
        "/admin/login", data={"email": "owner@hh.test", "password": "password123"})
    assert r.status_code == 401
    r = TestClient(app, follow_redirects=False).post(
        "/portal/login", data={"email": "ops@voiceorder.ai", "password": PW})
    assert r.status_code == 401
    # A correctly signed portal session can't pass as an admin one (separate signing key).
    forged = crypto.make_session_cookie({"pid": store.verify_platform_user("ops@voiceorder.ai", PW).id,
                                         "kind": "platform"}, purpose="portal")
    c = TestClient(app, follow_redirects=False)
    c.cookies.set("vo_admin", forged, path="/admin")
    assert c.get("/admin/").headers["location"] == "/admin/login"


def test_failed_logins_are_audited_and_locked_out(env):
    app, store, hh = env
    c = TestClient(app, follow_redirects=False)
    for _ in range(5):
        assert c.post("/admin/login", data={"email": "ops@voiceorder.ai", "password": "nope"}).status_code == 401
    locked = c.post("/admin/login", data={"email": "ops@voiceorder.ai", "password": PW})
    assert locked.status_code == 429  # even the right password, until the window passes
    actions = [e["action"] for e in store.list_audit(None, 50)]
    assert actions.count("admin.login_failed") == 5 and "admin.login_locked" in actions


# --- managing restaurants -------------------------------------------------------------------
def test_create_restaurant_with_its_first_admin(env):
    app, store, hh = env
    c = admin_client(app)
    r = c.post("/admin/api/restaurants", json={
        "name": "Taco Palace", "phone": "+1 214 555 0000", "timezone": "America/Chicago",
        "admin_email": "boss@taco.test", "admin_password": "password123"})
    assert r.json()["ok"]
    t = store.get_tenant(r.json()["id"])
    assert t.setting("timezone") == "America/Chicago" and store.list_users(t.id)[0].role == "admin"
    owner = TestClient(app, follow_redirects=False)
    assert owner.post("/portal/login", data={"email": "boss@taco.test", "password": "password123"}
                      ).headers["location"] == "/portal/"
    dup = c.post("/admin/api/restaurants", json={"name": "Copy", "phone": "+15622680097",
                                                 "admin_email": "x@x.test", "admin_password": "password123"})
    assert dup.status_code == 400 and "already registered" in dup.json()["error"]
    assert "admin.restaurant_created" in [e["action"] for e in store.list_audit(t.id)]


def test_staff_logins_password_reset_and_roles(env):
    app, store, hh = env
    c = admin_client(app)
    assert c.post(f"/admin/api/restaurants/{hh.id}/users",
                  json={"email": "cook@hh.test", "role": "kitchen", "password": "password123"}).json()["ok"]
    cook = next(u for u in store.list_users(hh.id) if u.email == "cook@hh.test")
    assert c.post(f"/admin/api/restaurants/{hh.id}/password",
                  json={"user_id": cook.id, "password": "brand-new-pass"}).json()["ok"]
    assert store.verify_user("cook@hh.test", "brand-new-pass") is not None
    assert store.verify_user("cook@hh.test", "password123") is None
    owner = next(u for u in store.list_users(hh.id) if u.email == "owner@hh.test")
    last_admin = c.post(f"/admin/api/restaurants/{hh.id}/users/{owner.id}/role", json={"role": "kitchen"})
    assert last_admin.status_code == 400  # a restaurant always keeps one admin
    other = store.create_tenant("Elsewhere")
    assert c.post(f"/admin/api/restaurants/{other.id}/password",
                  json={"user_id": cook.id, "password": "brand-new-pass2"}).status_code == 404


def test_flags_kill_switch_and_emergency_stop(env):
    app, store, hh = env
    c = admin_client(app)
    c.post(f"/admin/api/restaurants/{hh.id}/flags", json={"flag": "hitl_enabled", "enabled": True})
    assert store.flag_enabled(hh.id, "hitl_enabled")
    c.post("/admin/api/flags/global", json={"flag": "hitl_enabled", "enabled": False})
    assert not store.flag_enabled(hh.id, "hitl_enabled")
    assert "killed" in c.get("/admin/rollout").text
    c.post("/admin/api/flags/global", json={"flag": "hitl_enabled", "enabled": True})
    assert store.flag_enabled(hh.id, "hitl_enabled")
    assert c.post(f"/admin/api/restaurants/{hh.id}/flags", json={"flag": "bogus", "enabled": True}).status_code == 400
    c.post("/admin/api/emergency", json={"off": True})
    assert store.platform_emergency() and "AI emergency stop ON" in c.get("/admin/").text
    c.post("/admin/api/emergency", json={"off": False})
    actions = {e["action"] for e in store.list_audit(None, 100)}
    assert {"flag.set", "voice_routing.emergency_off", "voice_routing.emergency_cleared"} <= actions


def test_audit_filter_and_suspend(env):
    app, store, hh = env
    c = admin_client(app)
    assert c.post(f"/admin/api/restaurants/{hh.id}/status", json={"status": "suspended"}).json()["ok"]
    assert store.get_tenant(hh.id).status == "suspended"
    assert c.post(f"/admin/api/restaurants/{hh.id}/status", json={"status": "gone"}).status_code == 400
    page = c.get(f"/admin/audit?tenant={hh.id}&action=tenant.").text
    assert "tenant.status" in page and "flag.set" not in page
    assert "suspended" in c.get("/admin/restaurants?status=suspended").text


# --- calls to a suspended restaurant ------------------------------------------------------------
def test_suspended_restaurant_gets_a_closed_message_not_another_restaurant(monkeypatch):
    from voiceorder.api import main as api_main

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    store = api_main.tenant_store
    other = store.get_tenant_by_number("+12145550199", include_inactive=True) or \
        store.create_tenant("Suspended Grill", "+12145550199")
    store.set_tenant_status(other.id, "suspended", actor="test")
    try:
        r = TestClient(api_main.app).post("/twilio/voice", data={
            "CallSid": "CAsusp1", "From": "+14155550123", "To": "+12145550199"})
        assert "not taking calls right now" in r.text and "<Hangup />" in r.text
        assert "Suspended Grill" in r.text and "<Gather" not in r.text
        assert store.get_transcript(other.id, "CAsusp1")["meta"]["reason"] == "Restaurant suspended"
    finally:
        store.set_tenant_status(other.id, "active", actor="test")


def test_bad_webhook_signatures_are_audited_but_rate_limited(monkeypatch):
    from voiceorder.api import main as api_main

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret-token")
    api_main._signature_failure_logged.clear()
    client = TestClient(api_main.app)
    before = len([e for e in api_main.tenant_store.list_audit(None, 500) if e["action"] == "webhook.signature_failed"])
    for _ in range(5):
        assert client.post("/twilio/status", data={"CallSid": "CAx", "CallStatus": "completed"}).status_code == 403
    after = len([e for e in api_main.tenant_store.list_audit(None, 500) if e["action"] == "webhook.signature_failed"])
    assert after == before + 1
