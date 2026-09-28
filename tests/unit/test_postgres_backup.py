from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="needs TEST_DATABASE_URL pointing at a real Postgres instance",
)


@pytest.fixture
def store():
    from voiceorder.storage.postgres_backup import PostgresBackupStore

    return PostgresBackupStore(
        dsn=os.environ["TEST_DATABASE_URL"], restaurant_id=f"test-{uuid.uuid4()}"
    )


def test_record_then_list_round_trips(store):
    store.record("call-1", "unpaid", "order awaiting payment")
    entries = store.list_entries()
    assert len(entries) == 1
    assert entries[0].call_id == "call-1"
    assert entries[0].kind == "unpaid"
    assert entries[0].created_at is not None


def test_list_entries_filters_by_kind_and_is_scoped_to_restaurant(store):
    store.record("call-1", "unpaid", "a")
    store.record("call-2", "failed", "b")

    unpaid = store.list_entries(kind="unpaid")
    assert [e.call_id for e in unpaid] == ["call-1"]

    other_restaurant_store = type(store)(dsn=store.dsn, restaurant_id="a-different-restaurant")
    assert other_restaurant_store.list_entries() == []


def test_entries_are_ordered_by_creation_time(store):
    store.record("call-1", "unpaid", "first")
    store.record("call-1", "unpaid", "second")
    entries = store.list_entries()
    assert [e.detail for e in entries] == ["first", "second"]
