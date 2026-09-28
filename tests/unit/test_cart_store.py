from __future__ import annotations

import fakeredis
import pytest

from voiceorder.core.cart import Cart
from voiceorder.storage.cart_store import InMemoryCartStore, RedisCartStore


def _stores():
    return [
        InMemoryCartStore(),
        RedisCartStore(client=fakeredis.FakeRedis(decode_responses=True)),
    ]


@pytest.mark.parametrize("store", _stores(), ids=lambda s: type(s).__name__)
def test_save_then_load_round_trips(store):
    cart = Cart(call_id="c1")
    cart.add_line(item_id="churros", name="Churros", quantity=1, unit_price_cents=500)

    store.save("c1", "taqueria-demo", cart)
    loaded = store.load("c1")

    assert loaded is not None
    restaurant_id, restored = loaded
    assert restaurant_id == "taqueria-demo"
    assert [line.describe() for line in restored.lines] == ["1 x Churros"]


@pytest.mark.parametrize("store", _stores(), ids=lambda s: type(s).__name__)
def test_load_missing_call_returns_none(store):
    assert store.load("does-not-exist") is None


@pytest.mark.parametrize("store", _stores(), ids=lambda s: type(s).__name__)
def test_delete_removes_the_cart(store):
    store.save("c1", "taqueria-demo", Cart(call_id="c1"))
    store.delete("c1")
    assert store.load("c1") is None


def test_redis_store_sets_a_ttl():
    client = fakeredis.FakeRedis(decode_responses=True)
    store = RedisCartStore(client=client, ttl_seconds=120)
    store.save("c1", "taqueria-demo", Cart(call_id="c1"))
    assert client.ttl(store._key("c1")) <= 120
