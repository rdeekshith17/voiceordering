"""Storage ports: live carts and persisted orders.

SQLite is the default backend: one file under the data directory, so carts
and orders survive process restarts with no extra services to run. Pass a
postgres:// URL instead of a path (DATABASE_URL in the API) to keep them in
Postgres, which survives redeploys on hosts with throwaway disks. Set
STORAGE_BACKEND=memory to use the volatile in-memory stores (tests, demos).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Protocol

from .. import db as dbmod
from ..core.cart import Cart


class CartStore(Protocol):
    def create(self, restaurant_id: str) -> Cart: ...
    def get(self, cart_id: str) -> Cart | None: ...
    def save(self, cart: Cart) -> None: ...
    def delete(self, cart_id: str) -> None: ...


class InMemoryCartStore:
    def __init__(self) -> None:
        self._carts: dict[str, dict] = {}

    def create(self, restaurant_id: str) -> Cart:
        cart = Cart(
            cart_id=uuid.uuid4().hex[:12],
            restaurant_id=restaurant_id,
            idempotency_key=uuid.uuid4().hex,
        )
        self.save(cart)
        return cart

    def get(self, cart_id: str) -> Cart | None:
        raw = self._carts.get(cart_id)
        return Cart.from_dict(raw) if raw else None

    def save(self, cart: Cart) -> None:
        self._carts[cart.cart_id] = cart.to_dict()

    def delete(self, cart_id: str) -> None:
        self._carts.pop(cart_id, None)


class OrderStore(Protocol):
    def save(self, order: dict) -> dict: ...
    def get(self, order_id: str) -> dict | None: ...
    def list_recent(self, restaurant_id: str, limit: int = 50) -> list[dict]: ...


class InMemoryOrderStore:
    def __init__(self) -> None:
        self._orders: dict[str, dict] = {}

    def save(self, order: dict) -> dict:
        record = dict(order)
        record.setdefault("order_id", uuid.uuid4().hex[:12])
        record["saved_at"] = time.time()
        self._orders[record["order_id"]] = record
        return record

    def get(self, order_id: str) -> dict | None:
        return self._orders.get(order_id)

    def list_recent(self, restaurant_id: str, limit: int = 50) -> list[dict]:
        matches = [
            o for o in self._orders.values()
            if o.get("restaurant_id") == restaurant_id
        ]
        matches.sort(key=lambda o: o.get("saved_at", 0), reverse=True)
        return matches[:limit]

    def list_by_tenant(self, tenant_id: str, limit: int = 50) -> list[dict]:
        matches = [
            o for o in self._orders.values() if o.get("tenant_id") == tenant_id
        ]
        matches.sort(key=lambda o: o.get("saved_at", 0), reverse=True)
        return matches[:limit]


def _connect(target: str | Path) -> dbmod.Database:
    conn = dbmod.Database(target)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS carts ("
        " cart_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);"
        "CREATE TABLE IF NOT EXISTS orders ("
        " order_id TEXT PRIMARY KEY, restaurant_id TEXT NOT NULL,"
        " payload TEXT NOT NULL, saved_at REAL NOT NULL,"
        " tenant_id TEXT NOT NULL DEFAULT '');"
        "CREATE INDEX IF NOT EXISTS idx_orders_restaurant "
        "ON orders (restaurant_id, saved_at DESC);"
    )
    # Migration for databases created before multi-tenancy.
    if "tenant_id" not in conn.columns("orders"):
        conn.execute(
            "ALTER TABLE orders ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_tenant "
        "ON orders (tenant_id, saved_at DESC)"
    )
    conn.commit()
    return conn


class SqliteCartStore:
    """Live carts in SQLite or Postgres. Same interface as InMemoryCartStore,
    but the data survives restarts. One connection guarded by a lock; FastAPI
    runs the endpoints in threads."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = _connect(db_path)
        self._lock = threading.Lock()

    def create(self, restaurant_id: str) -> Cart:
        cart = Cart(
            cart_id=uuid.uuid4().hex[:12],
            restaurant_id=restaurant_id,
            idempotency_key=uuid.uuid4().hex,
        )
        self.save(cart)
        return cart

    def get(self, cart_id: str) -> Cart | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM carts WHERE cart_id = ?", (cart_id,)
            ).fetchone()
        return Cart.from_dict(json.loads(row[0])) if row else None

    def save(self, cart: Cart) -> None:
        with self._lock:
            self._conn.upsert("carts",
                              {"cart_id": cart.cart_id, "payload": json.dumps(cart.to_dict()),
                               "updated_at": time.time()},
                              key=("cart_id",))
            self._conn.commit()

    def delete(self, cart_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM carts WHERE cart_id = ?", (cart_id,))
            self._conn.commit()


class SqliteOrderStore:
    """Persisted orders in SQLite or Postgres. list_recent is served by an
    index on (restaurant_id, saved_at)."""

    def __init__(self, db_path: str | Path) -> None:
        self._conn = _connect(db_path)
        self._lock = threading.Lock()

    def save(self, order: dict) -> dict:
        record = dict(order)
        # The POS may hand back no order_id (None); it is the primary key.
        if not record.get("order_id"):
            record["order_id"] = uuid.uuid4().hex[:12]
        record["saved_at"] = time.time()
        with self._lock:
            self._conn.upsert("orders",
                              {"order_id": record["order_id"],
                               "restaurant_id": record.get("restaurant_id") or "",
                               "payload": json.dumps(record),
                               "saved_at": record["saved_at"],
                               "tenant_id": record.get("tenant_id") or ""},
                              key=("order_id",))
            self._conn.commit()
        return record

    def get(self, order_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_recent(self, restaurant_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM orders WHERE restaurant_id = ?"
                " ORDER BY saved_at DESC LIMIT ?",
                (restaurant_id, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def list_by_tenant(self, tenant_id: str, limit: int = 50) -> list[dict]:
        """Tenant portal: newest orders belonging to one tenant."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM orders WHERE tenant_id = ?"
                " ORDER BY saved_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]
