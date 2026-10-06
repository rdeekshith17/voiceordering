"""Tests for the FraudWatchdog: multi-signal detection, conservative firing,
phone masking. No network, no real vendor accounts.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from voiceorder.agents import fraud
from voiceorder.tenants.store import TenantStore

N = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc).timestamp()  # 12:00 UTC
NIGHT = datetime(2026, 9, 29, 3, 5, tzinfo=timezone.utc).timestamp()  # 03:05 UTC
DAY = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc).timestamp()  # 14:00 UTC


@pytest.fixture()
def store(tmp_path):
    return TenantStore(tmp_path / "fraud.db")


@pytest.fixture()
def tenant(store):
    t = store.create_tenant("Taco Palace", "+15551234567")
    store.set_settings(t.id, {"pos_profile": "square",
                              "restaurant_name": "Taco Palace"})
    return store.get_tenant(t.id)


def _at(monkeypatch, t):
    monkeypatch.setattr(time, "time", lambda: t)


def _call(store, tenant_id, sid, start, dur, from_number="+15550001111",
          ordered=False):
    store.start_call(sid, tenant_id, from_number, "+15551234567")
    tools = [{"name": "submit_order", "ok": ordered}] if ordered else []
    store.append_turn(sid, "i want two tacos", "coming right up", tools)
    store.end_call(sid, "completed")
    # force the exact timestamps (append_turn/end_call used patched time too)
    with store._lock:  # test-only timestamp shaping
        store._conn.execute(
            "UPDATE call_transcripts SET started_at = ?, updated_at = ?"
            " WHERE call_sid = ?", (start, start + dur, sid))
        store._conn.commit()


def _abusive_run(store, tenant, monkeypatch, n=12):
    _at(monkeypatch, N)
    for i in range(n):
        _call(store, tenant.id, f"bad{i}", NIGHT + i * 120, 700)


def test_multi_signal_fires_warning(store, tenant, monkeypatch):
    _abusive_run(store, tenant, monkeypatch)
    new = fraud.run_fraud_watch(store, now=N)
    assert len(new) == 1
    t = new[0]
    assert t["kind"] == "fraud_suspected"
    assert t["severity"] == "warning"
    assert t["tenant_id"] == tenant.id
    assert "3 signals" in t["title"]
    # phone masked in the ticket detail
    assert "+15550001111" not in t["detail"]
    assert "+15*******11" in t["detail"]


def test_single_weak_signal_does_not_fire(store, tenant, monkeypatch):
    # 5 long no-order calls alone: below the multi-signal bar
    _at(monkeypatch, N)
    for i in range(5):
        _call(store, tenant.id, f"w{i}", DAY + i * 120, 700)
    assert fraud.run_fraud_watch(store, now=N) == []


def test_normal_traffic_is_quiet(store, tenant, monkeypatch):
    _at(monkeypatch, N)
    _call(store, tenant.id, "ok1", DAY, 300, ordered=True)
    _call(store, tenant.id, "ok2", DAY + 3600, 240, from_number="+15550002222")
    _call(store, tenant.id, "ok3", DAY + 7200, 420, from_number="+15550003333")
    assert fraud.run_fraud_watch(store, now=N) == []


def test_strong_single_signal_fires(store, tenant, monkeypatch):
    _at(monkeypatch, N)
    for i in range(30):
        _call(store, tenant.id, f"r{i}", DAY + i * 60, 60,
              from_number="+15557778888")
    new = fraud.run_fraud_watch(store, now=N)
    assert len(new) == 1
    assert new[0]["kind"] == "fraud_suspected"


def test_dedupe_while_abuse_continues(store, tenant, monkeypatch):
    _abusive_run(store, tenant, monkeypatch)
    assert len(fraud.run_fraud_watch(store, now=N)) == 1
    assert fraud.run_fraud_watch(store, now=N) == []


@pytest.mark.parametrize("raw,expected", [
    ("+15551234567", "+15*******67"),
    ("5551234567", "55******67"),
    ("1234", "****"),
    ("", "[unknown]"),
])
def test_mask_number(raw, expected):
    assert fraud.mask_number(raw) == expected
