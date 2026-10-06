"""Tests for the SupportAgent: ticket dedupe, auto-resolve, platform + POS + call checks.

No network and no real vendor accounts: the platform probe and the POS
adapter builder are injected fakes. conftest keeps the DB hermetic.
"""
from __future__ import annotations

import pytest

from voiceorder.agents import support
from voiceorder.tenants.store import TenantStore


@pytest.fixture()
def store(tmp_path):
    return TenantStore(tmp_path / "agents.db")


@pytest.fixture()
def tenant(store):
    t = store.create_tenant("Taco Palace", "+15551234567")
    store.set_settings(t.id, {"pos_profile": "square",
                              "restaurant_name": "Taco Palace"})
    store.set_secret(t.id, "square", {"access_token": "sq0atp-test",
                                      "location_id": "L1",
                                      "environment": "production"})
    return store.get_tenant(t.id)


class _OkAdapter:
    def ping(self):
        return {"ok": True, "location_name": "Test Location"}


class _DownAdapter:
    def ping(self):
        raise RuntimeError("connection refused by vendor")


def _run(store, adapter=None, probe=None, **kw):
    build = (lambda t, o: adapter) if adapter is not None else None
    return support.run_support_check(
        store, build_adapter=build,
        platform_probe=probe or (lambda: (True, "ok")), **kw)


def _open(store, tenant_id, kind="pos_down"):
    return [t for t in store.list_tickets(tenant_id or "", include_platform=True)
            if t["kind"] == kind and t["status"] in ("open", "acked")]


# --- POS health ---------------------------------------------------------------
def test_pos_down_opens_ticket_and_dedupes(store, tenant):
    first = _run(store, adapter=_DownAdapter())
    assert len(first) == 1
    assert first[0]["kind"] == "pos_down"
    assert first[0]["severity"] == "critical"
    assert first[0]["tenant_id"] == tenant.id
    # second run: no duplicate open ticket
    assert _run(store, adapter=_DownAdapter()) == []
    assert len(_open(store, tenant.id)) == 1


def test_pos_recovery_auto_resolves(store, tenant):
    _run(store, adapter=_DownAdapter())
    assert len(_open(store, tenant.id)) == 1
    assert _run(store, adapter=_OkAdapter()) == []  # nothing new on recovery
    tickets = [t for t in store.list_tickets(tenant.id) if t["kind"] == "pos_down"]
    assert tickets[0]["status"] == "resolved"
    assert tickets[0]["resolved_at"]


def test_no_pos_configured_means_no_ticket(store):
    t = store.create_tenant("No POS Yet", "+15559876543")
    new = _run(store, adapter=_DownAdapter())
    assert [x for x in new if x["tenant_id"] == t.id and x["kind"] == "pos_down"] == []


def test_pos_failure_detail_never_carries_secrets(store, tenant):
    class _LeakyDown:
        def ping(self):
            raise RuntimeError("bad auth for sq0atp-super-secret-value")
    new = _run(store, adapter=_LeakyDown())
    assert len(new) == 1
    assert "sq0atp-super-secret-value" not in new[0]["detail"]


# --- platform reachability ----------------------------------------------------
def test_platform_down_ticket_and_recovery(store, tenant):
    new = _run(store, adapter=_OkAdapter(),
               probe=lambda: (False, "connection refused"))
    plat = [t for t in new if t["kind"] == "platform_down"]
    assert len(plat) == 1
    assert plat[0]["tenant_id"] is None
    assert plat[0]["severity"] == "critical"
    # dedupe while still down
    assert _run(store, adapter=_OkAdapter(),
                probe=lambda: (False, "connection refused")) == []
    # recovery resolves it
    assert _run(store, adapter=_OkAdapter(),
                probe=lambda: (True, "ok")) == []
    resolved = store.get_ticket(plat[0]["id"])
    assert resolved["status"] == "resolved"


def test_check_platform_uses_configured_base():
    ok, detail = support.check_platform(public_base_url="http://127.0.0.1:9",
                                        timeout=2)
    assert not ok and detail


# --- recent call failures -----------------------------------------------------
def test_call_failures_ticket_and_resolve(store, tenant):
    store.start_call("cs1", tenant.id, "+15550001111", "+15551234567")
    store.end_call("cs1", "failed")
    store.start_call("cs2", tenant.id, "+15550002222", "+15551234567")
    store.end_call("cs2", "failed")
    new = _run(store, adapter=_OkAdapter())
    fails = [t for t in new if t["kind"] == "call_failures"]
    assert len(fails) == 1
    assert fails[0]["severity"] == "warning"
    assert "2 failed calls" in fails[0]["title"]
    # still failing -> deduped
    assert [t for t in _run(store, adapter=_OkAdapter())
            if t["kind"] == "call_failures"] == []
    # calls now fine -> auto-resolve
    store.end_call("cs1", "completed")
    store.end_call("cs2", "completed")
    assert _run(store, adapter=_OkAdapter()) == []
    done = [t for t in store.list_tickets(tenant.id) if t["kind"] == "call_failures"]
    assert done[0]["status"] == "resolved"


def test_many_failures_escalate_to_critical(store, tenant):
    for i in range(5):
        sid = f"cx{i}"
        store.start_call(sid, tenant.id, "+15550001111", "+15551234567")
        store.end_call(sid, "failed")
    new = _run(store, adapter=_OkAdapter())
    fails = [t for t in new if t["kind"] == "call_failures"]
    assert fails and fails[0]["severity"] == "critical"


def test_healthy_run_opens_nothing(store, tenant):
    assert _run(store, adapter=_OkAdapter()) == []
