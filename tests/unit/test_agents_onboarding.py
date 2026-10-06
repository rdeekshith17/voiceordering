"""Tests for the OnboardingAgent: stalled detection, dedupe, auto-resolve.

Staleness is simulated with the run's `now` parameter (no DB poking).
"""
from __future__ import annotations

import pytest

from voiceorder.agents import onboarding
from voiceorder.tenants.store import TenantStore

DAY = 86400


@pytest.fixture()
def store(tmp_path):
    return TenantStore(tmp_path / "onboard.db")


def _aged(store, days, with_pos=False):
    t = store.create_tenant("Sleepy Diner", "+15550009999")
    store.set_settings(t.id, {"pos_profile": "square"})
    if with_pos:
        store.set_secret(t.id, "square", {"access_token": "sq0atp-test",
                                          "location_id": "L1"})
    created = store.tenant_created_at(t.id)
    return t.id, created + days * DAY


def _open(store, tenant_id):
    return [t for t in store.list_tickets(tenant_id, include_platform=True)
            if t["kind"] == "onboarding_stalled"
            and t["status"] in ("open", "acked")]


def test_stalled_no_pos_ticket(store):
    tid, now = _aged(store, 4)
    new = onboarding.run_onboarding_check(store, now=now)
    assert len(new) == 1
    t = new[0]
    assert t["kind"] == "onboarding_stalled"
    assert t["severity"] == "info"
    assert t["tenant_id"] == tid
    assert "not connected" in t["detail"]
    # daily rerun does not spam
    assert onboarding.run_onboarding_check(store, now=now + DAY) == []
    assert len(_open(store, tid)) == 1


def test_stalled_no_provider_and_no_activity(store):
    t = store.create_tenant("No POS At All", "+15550008888")
    created = store.tenant_created_at(t.id)
    new = onboarding.run_onboarding_check(store, now=created + 10 * DAY)
    assert len(new) == 1
    assert "no POS provider configured" in new[0]["detail"]


def test_fresh_tenant_left_alone(store):
    tid, now = _aged(store, 1)
    assert onboarding.run_onboarding_check(store, now=now) == []


def test_active_tenant_not_stalled(store):
    tid, now = _aged(store, 30, with_pos=True)
    store.start_call("live1", tid, "+15550001111", "+15550009999")
    store.end_call("live1", "completed")
    assert onboarding.run_onboarding_check(store, now=now) == []


def test_recovery_resolves(store):
    tid, now = _aged(store, 4)
    assert len(onboarding.run_onboarding_check(store, now=now)) == 1
    # tenant connects POS and takes a call -> ticket resolves
    store.set_secret(tid, "square", {"access_token": "sq0atp-test",
                                     "location_id": "L1"})
    store.start_call("live2", tid, "+15550001111", "+15550009999")
    store.end_call("live2", "completed")
    assert onboarding.run_onboarding_check(store, now=now + DAY) == []
    done = [t for t in store.list_tickets(tid)
            if t["kind"] == "onboarding_stalled"]
    assert done and done[0]["status"] == "resolved"


def test_custom_check_pos_honored(store):
    tid, now = _aged(store, 4, with_pos=True)
    store.start_call("live3", tid, "+15550001111", "+15550009999")
    store.end_call("live3", "completed")
    # live ping says broken even though creds exist -> stalled
    new = onboarding.run_onboarding_check(
        store, check_pos=lambda t: False, now=now)
    assert len(new) == 1 and "not connected" in new[0]["detail"]
    # ping passes -> fully onboarded, ticket resolves
    assert onboarding.run_onboarding_check(
        store, check_pos=lambda t: True, now=now + DAY) == []
    done = [t for t in store.list_tickets(tid)
            if t["kind"] == "onboarding_stalled"]
    assert done and done[0]["status"] == "resolved"
