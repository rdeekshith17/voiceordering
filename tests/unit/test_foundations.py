"""PR 1 foundations: migrations, roles, platform users, feature flags,
audit log, and background-job leases."""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voiceorder.api.storage import InMemoryOrderStore
from voiceorder.db import Database
from voiceorder.migrations import MIGRATIONS, applied_versions, run_migrations
from voiceorder.portal.portal import PortalDeps, build_portal_router
from voiceorder.tenants import rbac
from voiceorder.tenants.store import TenantStore


# --- migrations -----------------------------------------------------------------
def test_fresh_database_gets_every_migration_once(tmp_path):
    store = TenantStore(tmp_path / "fresh.db")
    db = store._conn
    assert applied_versions(db) == {v for v, _ in MIGRATIONS}
    assert run_migrations(db) == []  # re-run is a no-op
    for table in ("platform_users", "tenant_feature_flags", "audit_logs", "job_runs"):
        assert db.columns(table), table


def test_existing_database_is_upgraded_and_old_users_become_admins(tmp_path):
    path = tmp_path / "legacy.db"
    old = sqlite3.connect(path)  # the pre-PR 1 users table: no role column
    old.execute("CREATE TABLE tenant_users (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,"
                " email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, created_at REAL NOT NULL)")
    old.execute("INSERT INTO tenant_users VALUES ('u1', 't1', 'old@example.com', 'x', 1)")
    old.commit()
    old.close()
    store = TenantStore(path)
    assert store.get_user("u1").role == "admin"
    assert "001_user_roles" in applied_versions(store._conn)


# --- roles in the portal --------------------------------------------------------------
def _portal(tmp_path):
    store = TenantStore(tmp_path / "p.db")
    app = FastAPI()
    app.include_router(build_portal_router(PortalDeps(
        tenants=store, order_store=InMemoryOrderStore(),
        build_adapter=lambda t, o: None, get_catalog=lambda t: None)))
    owner = TestClient(app, follow_redirects=False)
    owner.post("/portal/signup", data={"restaurant": "Hyderabad House", "phone": "+15622680097",
                                       "email": "owner@example.com", "password": "password123"})
    tenant = store.get_tenant_by_name("Hyderabad House")
    return app, store, tenant, owner


def _login(app, email, password="password123"):
    client = TestClient(app, follow_redirects=False)
    r = client.post("/portal/login", data={"email": email, "password": password})
    return client, r


def test_admin_keeps_full_access_and_sees_every_page(tmp_path):
    app, store, tenant, owner = _portal(tmp_path)
    page = owner.get("/portal/").text
    for link in ("/portal/settings", "/portal/pos", "/portal/statistics", "/portal/support"):
        assert link in page
    assert owner.post("/portal/api/settings", json={"pickup_minutes": "25"}).json()["ok"]


def test_kitchen_staff_only_reach_kitchen_and_orders(tmp_path):
    app, store, tenant, _ = _portal(tmp_path)
    store.create_user(tenant.id, "cook@example.com", "password123", role="kitchen")
    cook, r = _login(app, "cook@example.com")
    assert r.headers["location"] == "/portal/kitchen"
    orders = cook.get("/portal/orders")
    assert orders.status_code == 200 and cook.get("/portal/kitchen").status_code == 200
    assert "/portal/settings" not in orders.text and "/portal/pos" not in orders.text
    for page in ("/portal/", "/portal/settings", "/portal/pos", "/portal/customers"):
        assert cook.get(page).headers["location"] == "/portal/kitchen", page
    assert cook.post("/portal/api/settings", json={"pickup_minutes": "5"}).status_code == 403
    assert cook.get("/portal/api/pos").status_code == 403
    assert store.get_tenant(tenant.id).setting("pickup_minutes", "") != "5"


def test_only_admin_and_kitchen_roles_exist(tmp_path):
    app, store, tenant, _ = _portal(tmp_path)
    assert rbac.ROLES == ("admin", "kitchen") and store.list_users(tenant.id)[0].role == "admin"
    for old_name in ("owner", "manager", "super_admin"):
        with pytest.raises(ValueError):
            store.create_user(tenant.id, f"{old_name}@example.com", "password123", role=old_name)
    # A row written with an old role name still works, as an admin.
    assert rbac.can("owner", "pos.edit") and rbac.can("manager", "settings.edit")


def test_role_changes_are_tenant_scoped_and_keep_an_admin(tmp_path):
    app, store, tenant, _ = _portal(tmp_path)
    admin = store.list_users(tenant.id)[0]
    with pytest.raises(ValueError):
        store.set_user_role(tenant.id, admin.id, "kitchen")  # last admin
    cook = store.create_user(tenant.id, "cook@example.com", "password123", role="kitchen")
    assert store.set_user_role(tenant.id, cook.id, "admin")
    assert store.set_user_role(tenant.id, admin.id, "kitchen")  # another admin exists now
    other = store.create_tenant("Taco Palace")
    assert store.set_user_role(other.id, cook.id, "kitchen") is False


# --- platform users -----------------------------------------------------------------------
def test_platform_users_are_separate_from_restaurant_logins(tmp_path):
    app, store, tenant, _ = _portal(tmp_path)
    admin = store.create_platform_user("ops@voiceorder.ai", "a-long-password!", "super_admin")
    assert store.verify_platform_user("ops@voiceorder.ai", "a-long-password!").role == "super_admin"
    assert store.verify_platform_user("owner@example.com", "password123") is None
    assert store.verify_user("ops@voiceorder.ai", "a-long-password!") is None
    assert store.get_platform_user(admin.id).email == "ops@voiceorder.ai"
    with pytest.raises(ValueError):
        store.create_platform_user("short@x.com", "short-password-ok!", "support")  # no such role
    assert rbac.PLATFORM_ROLES == ("super_admin",) and rbac.platform_can("super_admin", "flags.edit")
    assert not rbac.platform_can("admin", "tenants.view")  # restaurant roles have no platform rights


# --- feature flags ------------------------------------------------------------------------------
def test_flags_default_off_and_both_kill_switches_work(tmp_path, monkeypatch):
    store = TenantStore(tmp_path / "f.db")
    t = store.create_tenant("Hyderabad House")
    assert store.list_flags(t.id) == {f: False for f in rbac.KNOWN_FLAGS}
    assert store.flag_enabled(t.id, "hitl_enabled") is False
    store.set_flag(t.id, "hitl_enabled", True, actor="u1")
    assert store.flag_enabled(t.id, "hitl_enabled") is True
    store.set_flag("*", "hitl_enabled", False, actor="ops")  # platform kill switch
    assert store.flag_enabled(t.id, "hitl_enabled") is False
    store.set_flag("*", "hitl_enabled", True, actor="ops")
    monkeypatch.setenv("DISABLED_FEATURES", "hitl_enabled")
    assert store.flag_enabled(t.id, "hitl_enabled") is False
    monkeypatch.delenv("DISABLED_FEATURES")
    assert store.flag_enabled(t.id, "hitl_enabled") is True
    with pytest.raises(ValueError):
        store.set_flag(t.id, "made_up_flag", True)
    events = [e for e in store.list_audit(t.id) if e["action"] == "flag.set"]
    assert events and events[-1]["after"] == {"enabled": True}


# --- audit log ------------------------------------------------------------------------------------
def test_settings_and_pos_changes_are_audited_without_secrets(tmp_path):
    app, store, tenant, owner = _portal(tmp_path)
    owner.post("/portal/api/settings", json={"pickup_minutes": "30", "timezone": "America/Chicago"})
    owner.post("/portal/api/pos", json={"provider": "square", "values": {
        "access_token": "EAAA-super-secret-token", "location_id": "L123", "environment": "sandbox"}})
    events = {e["action"]: e for e in store.list_audit(tenant.id)}
    assert {"account.signup", "settings.update", "pos.credentials_saved"} <= set(events)
    assert events["settings.update"]["after"]["pickup_minutes"] == "30"
    pos = events["pos.credentials_saved"]
    assert pos["after"]["access_token"] == "[redacted]" and pos["after"]["location_id"] == "L123"
    assert "EAAA-super-secret-token" not in json.dumps(store.list_audit())


# --- job leases ---------------------------------------------------------------------------------------
def test_job_lease_runs_once_per_interval(tmp_path):
    store = TenantStore(tmp_path / "j.db")
    assert store.try_start_job("billing", lease_seconds=600, min_interval=3600, now=1000)
    assert not store.try_start_job("billing", 600, 3600, now=1001)  # leased
    store.finish_job("billing", "ok", now=1100)
    assert not store.try_start_job("billing", 600, 3600, now=2000)  # not due yet
    assert store.try_start_job("billing", 600, 3600, now=1100 + 3600)
    # A crashed run (never finished) can be retaken once its lease expires.
    assert not store.try_start_job("billing", 600, 3600, now=4800)
    assert store.try_start_job("billing", 600, 3600, now=4700 + 601 + 3600)
    assert store.job_status()[0]["job"] == "billing"


def test_database_wrapper_reports_columns_for_missing_tables(tmp_path):
    assert Database(tmp_path / "x.db").columns("nope") == []
