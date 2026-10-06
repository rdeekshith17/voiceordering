"""Tests for the MenuSyncAgent: success, failure ticket, quiet skip.

Vendor sync is injected; the default path is covered by the adapter tests.
"""
from __future__ import annotations

import pytest

from voiceorder.agents import menusync
from voiceorder.tenants.store import TenantStore


@pytest.fixture()
def store(tmp_path):
    return TenantStore(tmp_path / "menusync.db")


@pytest.fixture()
def tenant(store):
    t = store.create_tenant("Taco Palace", "+15551234567")
    store.set_settings(t.id, {"pos_profile": "square"})
    store.set_secret(t.id, "square", {"access_token": "sq0atp-test",
                                      "location_id": "L1"})
    return store.get_tenant(t.id)


def _open(store, tenant_id):
    return [t for t in store.list_tickets(tenant_id, include_platform=True)
            if t["kind"] == "menu_sync_failed"
            and t["status"] in ("open", "acked")]


def test_success_records_settings(store, tenant):
    results = menusync.run_menu_sync(
        store, sync_catalog=lambda t: 42, check_pos=lambda t: True, now=1000.0)
    assert results == [{"tenant_id": tenant.id, "ok": True, "item_count": 42}]
    s = store.get_settings(tenant.id)
    assert s["menu_sync_last_at"] == "1000.0"
    assert s["menu_sync_item_count"] == "42"
    assert _open(store, tenant.id) == []


def test_failure_opens_ticket_and_dedupes(store, tenant):
    def boom(t):
        raise RuntimeError("sync died: sq0atp-super-secret-token-value-xyz")

    results = menusync.run_menu_sync(
        store, sync_catalog=boom, check_pos=lambda t: True, now=1000.0)
    assert len(results) == 1 and not results[0]["ok"]
    assert results[0]["ticket_id"]
    open1 = _open(store, tenant.id)
    assert len(open1) == 1
    assert open1[0]["severity"] == "warning"
    # token-shaped text never lands in the ticket
    assert "sq0atp-super-secret-token-value-xyz" not in open1[0]["detail"]
    assert "[redacted]" in open1[0]["detail"]
    # second failure: no duplicate ticket
    results2 = menusync.run_menu_sync(
        store, sync_catalog=boom, check_pos=lambda t: True, now=2000.0)
    assert results2[0]["ticket_id"] is None
    assert len(_open(store, tenant.id)) == 1
    # no success settings recorded on failure
    assert "menu_sync_item_count" not in store.get_settings(tenant.id)


def test_bad_pos_skips_quietly(store, tenant):
    calls = []
    results = menusync.run_menu_sync(
        store, sync_catalog=lambda t: calls.append(t) or 1,
        check_pos=lambda t: False)
    assert results == []
    assert calls == []
    assert _open(store, tenant.id) == []
    assert "menu_sync_last_at" not in store.get_settings(tenant.id)


def test_tenant_without_pos_skipped_by_default_check(store, tmp_path):
    from voiceorder.tenants.store import TenantStore
    solo = TenantStore(tmp_path / "menusync-solo.db")
    t = solo.create_tenant("No POS Yet", "+15559876543")
    results = menusync.run_menu_sync(solo, sync_catalog=lambda x: 9)
    assert results == []  # default check_pos: no provider -> skip
    assert solo.list_tickets(t.id) == []
