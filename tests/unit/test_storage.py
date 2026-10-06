"""Tests for durable storage (SQLite) and the TTS rate limiter."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import voiceorder.api.main as api_main
from voiceorder.api.storage import SqliteCartStore, SqliteOrderStore


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.db"


def test_sqlite_cart_round_trip(db_path, ctx):
    store = SqliteCartStore(db_path)
    cart = store.create(ctx.restaurant_id)
    cart.customer_name = "Deekshith"
    store.save(cart)
    loaded = store.get(cart.cart_id)
    assert loaded is not None
    assert loaded.customer_name == "Deekshith"
    assert loaded.cart_id == cart.cart_id


def test_sqlite_cart_missing_returns_none(db_path, ctx):
    store = SqliteCartStore(db_path)
    assert store.get("nope") is None


def test_sqlite_cart_delete(db_path, ctx):
    store = SqliteCartStore(db_path)
    cart = store.create(ctx.restaurant_id)
    store.delete(cart.cart_id)
    assert store.get(cart.cart_id) is None


def test_sqlite_orders_list_recent(db_path):
    store = SqliteOrderStore(db_path)
    store.save({"order_id": "o1", "restaurant_id": "r1", "order_number": "1"})
    store.save({"order_id": "o2", "restaurant_id": "r1", "order_number": "2"})
    store.save({"order_id": "o3", "restaurant_id": "other", "order_number": "3"})
    recent = store.list_recent("r1", limit=10)
    assert [o["order_id"] for o in recent] == ["o2", "o1"]
    assert store.get("o2")["order_number"] == "2"


def test_sqlite_stores_share_one_file(db_path, ctx):
    carts = SqliteCartStore(db_path)
    orders = SqliteOrderStore(db_path)
    cart = carts.create(ctx.restaurant_id)
    orders.save({"order_id": "o9", "restaurant_id": ctx.restaurant_id})
    # Reopen: data survives (this is the restart scenario).
    assert SqliteCartStore(db_path).get(cart.cart_id) is not None
    assert SqliteOrderStore(db_path).get("o9") is not None


@pytest.fixture()
def speak_client(monkeypatch):
    monkeypatch.setattr(
        "voiceorder.voice.tts.synthesize", lambda text, **kw: b"fake-mp3"
    )
    return TestClient(api_main.app)


def test_voice_speak_rate_limited(speak_client, monkeypatch):
    monkeypatch.setattr(api_main.settings, "voice_secret", "")
    monkeypatch.setattr(api_main.settings, "voice_secret_previous", "")
    monkeypatch.setattr(api_main, "_TTS_BUCKET_MAX", 2)
    api_main._tts_buckets.clear()
    for _ in range(2):
        resp = speak_client.post("/voice/speak", json={"text": "hello"})
        assert resp.status_code == 200
    resp = speak_client.post("/voice/speak", json={"text": "hello"})
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "60"
    api_main._tts_buckets.clear()


def test_voice_speak_requires_secret_when_configured(speak_client, monkeypatch):
    monkeypatch.setattr(api_main.settings, "voice_secret", "s3cret")
    api_main._tts_buckets.clear()
    try:
        denied = speak_client.post("/voice/speak", json={"text": "hello"})
        assert denied.status_code == 401
        ok = speak_client.post(
            "/voice/speak",
            json={"text": "hello"},
            headers={"X-Voice-Secret": "s3cret"},
        )
        assert ok.status_code == 200
    finally:
        monkeypatch.setattr(api_main.settings, "voice_secret", "")
        api_main._tts_buckets.clear()
