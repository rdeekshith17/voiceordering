from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from ..core.cart import Cart


class CartStore(Protocol):
    """The live cart per call (build plan section 3/6). InMemoryCartStore is
    the default -- what every test uses -- RedisCartStore is the real one,
    switched on by setting VOICEORDER_REDIS_URL."""

    def save(self, call_id: str, restaurant_id: str, cart: Cart) -> None: ...
    def load(self, call_id: str) -> tuple[str, Cart] | None: ...
    def delete(self, call_id: str) -> None: ...


@dataclass
class InMemoryCartStore:
    _data: dict[str, tuple[str, Cart]] = field(default_factory=dict)

    def save(self, call_id: str, restaurant_id: str, cart: Cart) -> None:
        self._data[call_id] = (restaurant_id, cart)

    def load(self, call_id: str) -> tuple[str, Cart] | None:
        return self._data.get(call_id)

    def delete(self, call_id: str) -> None:
        self._data.pop(call_id, None)


@dataclass
class RedisCartStore:
    """A call shouldn't outlive the TTL -- if it does, the cart is gone and
    the caller has to start over, same as if the call had simply hung up."""

    client: object  # a redis.Redis (or redis.Redis-compatible, e.g. fakeredis) client
    ttl_seconds: int = 7200
    key_prefix: str = "voiceorder:call:"

    @classmethod
    def from_url(cls, url: str, ttl_seconds: int = 7200) -> RedisCartStore:
        import redis

        return cls(client=redis.Redis.from_url(url, decode_responses=True), ttl_seconds=ttl_seconds)

    def _key(self, call_id: str) -> str:
        return f"{self.key_prefix}{call_id}"

    def save(self, call_id: str, restaurant_id: str, cart: Cart) -> None:
        payload = json.dumps({"restaurant_id": restaurant_id, "cart": cart.to_dict()})
        self.client.set(self._key(call_id), payload, ex=self.ttl_seconds)

    def load(self, call_id: str) -> tuple[str, Cart] | None:
        raw = self.client.get(self._key(call_id))
        if raw is None:
            return None
        data = json.loads(raw)
        return data["restaurant_id"], Cart.from_dict(data["cart"])

    def delete(self, call_id: str) -> None:
        self.client.delete(self._key(call_id))
