"""Storage against a real Postgres (the DATABASE_URL path used in the cloud).

Skipped unless TEST_DATABASE_URL points at a throwaway database, e.g.
  TEST_DATABASE_URL=postgresql://user@localhost:5432/voiceorder_test pytest
Every table is dropped before each test.
"""
from __future__ import annotations

import os
import time

import pytest

from voiceorder.api.storage import SqliteCartStore, SqliteOrderStore
from voiceorder.core.cart import Cart
from voiceorder.tenants.store import TenantStore

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

_TABLES = ["carts", "orders", "tenants", "tenant_settings", "tenant_secrets",
           "tenant_users", "call_transcripts", "tickets", "marketing_drafts", "usage_daily",
           "customers", "schema_migrations", "platform_users", "tenant_feature_flags",
           "audit_logs", "job_runs"]


@pytest.fixture(autouse=True)
def clean_db():
    import psycopg

    with psycopg.connect(URL, autocommit=True) as conn:
        for t in _TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    yield


def _stores():
    # Same order as the API: cart/order tables first, then tenancy.
    return SqliteCartStore(URL), SqliteOrderStore(URL), TenantStore(URL)


def test_data_survives_a_fresh_connection():
    """The whole point: a redeploy is a new process with new connections."""
    carts, orders, tenants = _stores()
    t = tenants.create_tenant("Hyderabad House", "+1 (562) 268-0097")
    tenants.set_settings(t.id, {"timezone": "America/Chicago", "pos_profile": "square"})
    tenants.set_secret(t.id, "square", {"access_token": "tok", "location_id": "L1"})
    user = tenants.create_user(t.id, "Owner@Example.com", "password123")
    cart = carts.create("hyderabad-house")
    orders.save({"order_id": None, "restaurant_id": "hyderabad-house", "tenant_id": t.id,
                 "order_number": "1001", "totals": {"total": 25.5},
                 "lines": [{"item_name": "Chicken Biryani", "quantity": 2, "unit_price": 12.75}]})
    tenants.start_call("CA1", t.id, "+14155550123", "+15622680097")
    tenants.append_turn("CA1", "two biryani", "Got it.", [{"name": "add_item", "ok": True}])
    tenants.end_call("CA1", "completed")

    carts2, orders2, tenants2 = _stores()  # "after the redeploy"
    t2 = tenants2.get_tenant_by_number("5622680097")
    assert t2 and t2.id == t.id
    assert t2.setting("timezone") == "America/Chicago"
    assert tenants2.get_secret(t.id, "square")["location_id"] == "L1"
    assert tenants2.verify_user("owner@example.com", "password123").id == user.id
    assert carts2.get(cart.cart_id) is not None
    saved = orders2.list_by_tenant(t.id)
    assert len(saved) == 1 and saved[0]["order_id"]  # None replaced with a real key
    assert isinstance(saved[0]["saved_at"], float)
    assert tenants2.get_transcript(t.id, "CA1")["turns"][0]["reply"] == "Got it."
    assert tenants2.order_count(t.id) == 1
    assert tenants2.top_selling_items(t.id)[0]["name"] == "Chicken Biryani"


def test_upserts_replace_instead_of_duplicating():
    carts, orders, tenants = _stores()
    t = tenants.create_tenant("Taco Palace")
    tenants.set_settings(t.id, {"pickup_minutes": "20"})
    tenants.set_settings(t.id, {"pickup_minutes": "25"})
    assert tenants.get_settings(t.id) == {"pickup_minutes": "25"}
    tenants.upsert_usage(t.id, "2026-10-06", 1, 2.0, 100, 0)
    tenants.upsert_usage(t.id, "2026-10-06", 4, 12.5, 900, 2)
    assert [(u["calls"], u["talk_minutes"]) for u in tenants.get_usage(t.id)] == [(4, 12.5)]
    cart = Cart(cart_id="c1", restaurant_id="r", idempotency_key="k")
    carts.save(cart)
    carts.save(cart)
    carts.delete("c1")
    assert carts.get("c1") is None
    rec = orders.save({"order_id": "o1", "restaurant_id": "r", "tenant_id": t.id})
    orders.save({**rec, "status": "completed"})
    assert [o["status"] for o in orders.list_by_tenant(t.id)] == ["completed"]


def test_tickets_drafts_and_platform_wide_rows():
    _, _, tenants = _stores()
    t = tenants.create_tenant("Taco Palace")
    assert tenants.open_ticket(None, "platform_down", "critical", "Platform unreachable")
    assert tenants.open_ticket(None, "platform_down", "critical", "again") is None  # deduped
    mine = tenants.open_ticket(t.id, "pos_down", "critical", "Square down")
    assert {x["title"] for x in tenants.list_tickets(t.id)} == {"Platform unreachable", "Square down"}
    assert tenants.ack_ticket(t.id, mine["id"])
    assert tenants.resolve_ticket(None, "platform_down")
    assert tenants.resolve_ticket(t.id, "pos_down")
    now = time.time()
    tenants.save_draft(t.id, "Biryani Friday", "Free lassi", "sms", now=now)
    assert tenants.drafts_since(t.id, now - 1) == 1
    assert tenants.list_drafts(t.id)[0]["title"] == "Biryani Friday"
    assert [x.id for x in tenants.list_tenants()] == [t.id]
    assert len(tenants.calls_in_window(None, 0)) == 0


def test_percent_signs_and_quotes_in_values():
    _, _, tenants = _stores()
    t = tenants.create_tenant("100% Tacos")
    tenants.set_settings(t.id, {"note": "50% off; it's '?' time"})
    assert tenants.get_settings(t.id)["note"] == "50% off; it's '?' time"
    assert tenants.get_tenant_by_name("100% tacos").id == t.id


def test_migrations_roles_flags_audit_and_leases_on_postgres():
    from voiceorder.migrations import MIGRATIONS, applied_versions, run_migrations

    _, _, tenants = _stores()
    assert applied_versions(tenants._conn) == {v for v, _ in MIGRATIONS}
    assert run_migrations(tenants._conn) == []
    t = tenants.create_tenant("Hyderabad House")
    cook = tenants.create_user(t.id, "cook@example.com", "password123", role="kitchen")
    assert tenants.verify_user("cook@example.com", "password123").role == "kitchen"
    assert tenants.flag_enabled(t.id, "hitl_enabled") is False
    tenants.set_flag(t.id, "hitl_enabled", True, actor=cook.id)
    assert tenants.flag_enabled(t.id, "hitl_enabled") is True
    tenants.set_flag("*", "hitl_enabled", False, actor="ops")
    assert tenants.flag_enabled(t.id, "hitl_enabled") is False
    tenants.audit("user", cook.id, t.id, "pos.credentials_saved", "pos:square",
                  None, {"access_token": "secret", "location_id": "L1"})
    saved = [e for e in tenants.list_audit(t.id) if e["action"] == "pos.credentials_saved"][0]
    assert saved["after"] == {"access_token": "[redacted]", "location_id": "L1"}
    assert tenants.try_start_job("billing", 600, 3600, now=1000)
    assert not tenants.try_start_job("billing", 600, 3600, now=1001)
    _, _, second_instance = _stores()  # another app instance sees the same lease
    assert not second_instance.try_start_job("billing", 600, 3600, now=1002)
