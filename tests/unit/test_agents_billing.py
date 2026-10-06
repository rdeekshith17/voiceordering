"""Tests for the BillingAgent: rollup math, zero-activity tenants, idempotency.

Usage is derived from call_transcripts (see billing.FIELD MAPPING in the
module docstring). No network, no credentials.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from voiceorder.agents import billing
from voiceorder.tenants.store import TenantStore


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "agents.db"


@pytest.fixture()
def store(db_path):
    return TenantStore(db_path)


@pytest.fixture()
def tenant(store):
    t = store.create_tenant("Taco Palace", "+15551234567")
    return store.get_tenant(t.id)


def _backdate(db_path, call_sid, started_ts, updated_ts):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE call_transcripts SET started_at = ?, updated_at = ?"
                 " WHERE call_sid = ?", (started_ts, updated_ts, call_sid))
    conn.commit()
    conn.close()


def _day_ts(day, hour=12, minute=0):
    return datetime(*[int(x) for x in day.split("-")], hour, minute,
                    tzinfo=timezone.utc).timestamp()


# --- rollup math ---------------------------------------------------------------
def test_rollup_math(store, tenant, db_path):
    day = "2026-09-28"
    # call 1: 120 s, replies of 11 + 5 chars
    store.start_call("c1", tenant.id, "+15550001111", "+15551234567")
    store.append_turn("c1", "one biryani", "hello world", [])
    store.append_turn("c1", "yes", "great", [])
    _backdate(db_path, "c1", _day_ts(day, 12), _day_ts(day, 12, 2))
    # call 2: 60 s, one 3-char reply
    store.start_call("c2", tenant.id, "+15550002222", "+15551234567")
    store.append_turn("c2", "bye", "bye", [])
    _backdate(db_path, "c2", _day_ts(day, 13), _day_ts(day, 13, 1))

    rows = billing.rollup_day(store, day=day)
    assert len(rows) == 1
    row = rows[0]
    assert row["tenant_id"] == tenant.id and row["date"] == day
    assert row["calls"] == 2
    assert row["talk_minutes"] == pytest.approx(3.0)
    assert row["tts_chars"] == len("hello world") + len("great") + len("bye")
    assert row["sms_sent"] == 0

    persisted = store.get_usage(tenant.id)
    assert len(persisted) == 1 and persisted[0]["calls"] == 2


def test_zero_activity_tenant_gets_zero_row(store, tenant):
    quiet = store.create_tenant("Quiet Diner", "+15559990000")
    rows = billing.rollup_day(store, day="2026-09-28")
    by_tenant = {r["tenant_id"]: r for r in rows}
    assert set(by_tenant) == {tenant.id, quiet.id}
    z = by_tenant[quiet.id]
    assert (z["calls"], z["talk_minutes"], z["tts_chars"], z["sms_sent"]) == (0, 0.0, 0, 0)


def test_rollup_is_idempotent(store, tenant, db_path):
    day = "2026-09-28"
    store.start_call("c1", tenant.id, "+15550001111", "+15551234567")
    _backdate(db_path, "c1", _day_ts(day, 12), _day_ts(day, 12, 5))
    billing.rollup_day(store, day=day)
    billing.rollup_day(store, day=day)
    assert len(store.get_usage(tenant.id)) == 1


def test_calls_outside_the_day_are_excluded(store, tenant, db_path):
    store.start_call("c1", tenant.id, "+15550001111", "+15551234567")
    _backdate(db_path, "c1", _day_ts("2026-09-27", 23, 59),
              _day_ts("2026-09-28", 0, 1))
    rows = billing.rollup_day(store, day="2026-09-28")
    assert rows[0]["calls"] == 0  # started the previous UTC day


def test_invalid_day_raises(store):
    with pytest.raises(ValueError):
        billing.rollup_day(store, day="not-a-date")


def test_inactive_tenants_are_skipped(store, tenant):
    with store._lock:
        store._conn.execute("UPDATE tenants SET status = 'archived' WHERE id = ?",
                            (tenant.id,))
        store._conn.commit()
    rows = billing.rollup_day(store, day="2026-09-28")
    assert rows == []
