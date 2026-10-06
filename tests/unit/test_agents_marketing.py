"""Tests for the MarketingAgent: determinism, no-LLM operation, weekly idempotency.

No network, no credentials, no LLM key: the default path must work with
nothing but the tenant store and a catalog callable.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from voiceorder.agents import marketing
from voiceorder.tenants.store import TenantStore


class _Item:
    def __init__(self, name, available=True):
        self.name = name
        self.available = available


class _Catalog:
    def __init__(self, names):
        self._names = names

    def all_items(self):
        return [_Item(n) for n in self._names]


@pytest.fixture()
def store(tmp_path):
    return TenantStore(tmp_path / "agents.db")


@pytest.fixture()
def tenant(store):
    t = store.create_tenant("Taco Palace", "+15551234567")
    store.set_settings(t.id, {"pos_profile": "square",
                              "restaurant_name": "Taco Palace",
                              "phone_number": "+15551234567",
                              "pickup_minutes": "20"})
    store.set_secret(t.id, "square", {"access_token": "x", "location_id": "L1",
                                      "environment": "production"})
    return store.get_tenant(t.id)


def _seed_orders(db_path, tenant_id, lines, when=None):
    when = when if when is not None else time.time()
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY,"
                 " restaurant_id TEXT, payload TEXT, saved_at REAL,"
                 " tenant_id TEXT NOT NULL DEFAULT '')")
    for i, ln in enumerate(lines):
        payload = json.dumps({"order_id": f"o{i}", "tenant_id": tenant_id,
                              "lines": ln})
        conn.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?)",
                     (f"o{i}", "r", payload, when, tenant_id))
    conn.commit()
    conn.close()


# --- determinism --------------------------------------------------------------
def test_generate_drafts_is_deterministic(tenant):
    menu = ["Chicken Biryani", "Lamb Curry", "Samosa"]
    sellers = [{"name": "Chicken Biryani", "qty": 12, "revenue": 203.88}]
    a = marketing.generate_drafts(tenant, menu, sellers, "2026-W40")
    b = marketing.generate_drafts(tenant, menu, sellers, "2026-W40")
    assert a == b
    assert len(a) == 3
    assert {d["channel"] for d in a} == {"sms", "social", "in-store"}
    assert all(d["title"] and d["body"] for d in a)


def test_drafts_use_best_sellers(tenant):
    sellers = [{"name": "Chicken Biryani", "qty": 12, "revenue": 203.88}]
    drafts = marketing.generate_drafts(tenant, ["Samosa"], sellers, "2026-W40")
    assert "Chicken Biryani" in drafts[0]["body"]  # hero item first


def test_drafts_vary_by_week(tenant):
    a = marketing.generate_drafts(tenant, ["A1", "A2", "A3", "A4"], [], "2026-W40")
    b = marketing.generate_drafts(tenant, ["A1", "A2", "A3", "A4"], [], "2026-W41")
    assert a != b  # seeded shuffle differs across weeks


# --- run_marketing: no LLM, no network ----------------------------------------
def test_run_marketing_needs_no_llm_or_network(store, tenant, tmp_path):
    _seed_orders(tmp_path / "agents.db", tenant.id,
                 [[{"item_name": "Chicken Biryani", "quantity": 2,
                    "unit_price": 16.99}]])
    saved = marketing.run_marketing(
        store, get_catalog=lambda t: _Catalog(["Chicken Biryani", "Samosa"]),
        llm_rewrite=None)
    assert len(saved) == 3
    assert all(d["status"] == "draft" for d in saved)
    assert {d["channel"] for d in saved} == {"sms", "social", "in-store"}
    assert all(d["tenant_id"] == tenant.id for d in saved)


def test_run_marketing_weekly_idempotent(store, tenant):
    kw = dict(get_catalog=lambda t: _Catalog(["Samosa"]))
    assert len(marketing.run_marketing(store, **kw)) == 3
    assert marketing.run_marketing(store, **kw) == []  # same week: nothing new


def test_run_marketing_skips_tenant_without_pos(store):
    t = store.create_tenant("No POS", "+15559998888")
    saved = marketing.run_marketing(store,
                                    get_catalog=lambda ten: _Catalog(["Samosa"]))
    assert [d for d in saved if d["tenant_id"] == t.id] == []


def test_run_marketing_skips_tenant_without_menu_data(store, tenant):
    assert marketing.run_marketing(store, get_catalog=lambda t: None) == []


def test_llm_rewrite_hook_is_optional(store, tenant):
    saved = marketing.run_marketing(
        store, get_catalog=lambda t: _Catalog(["Samosa"]),
        llm_rewrite=lambda text: text.upper())
    assert saved and all(d["body"] == d["body"].upper() for d in saved)


def test_llm_rewrite_failure_falls_back_to_template(store, tenant):
    def boom(text):
        raise RuntimeError("llm down")
    saved = marketing.run_marketing(store, get_catalog=lambda t: _Catalog(["Samosa"]),
                                    llm_rewrite=boom)
    assert len(saved) == 3 and all(d["body"] for d in saved)
