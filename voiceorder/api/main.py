from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

from ..agent.tool_schemas import TOOL_SCHEMAS
from ..core.backup import BackupScreen, BackupStore
from ..core.cart import Cart
from ..core.catalog import Catalog
from ..core.tools import OrderTools, ToolError
from ..pos_adapters.fake import FakePos
from ..storage.cart_store import CartStore, InMemoryCartStore

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"
ALLOWED_TOOLS = {t["name"] for t in TOOL_SCHEMAS}


@dataclass
class RestaurantConfig:
    id: str
    name: str
    called_number: str
    pos_profile: str
    catalog_path: Path
    hours: str = "not set"
    pickup_lead_minutes: int = 20
    max_quantity_per_line: int = 20
    max_total_cents: int = 50_000
    transfer_number: str | None = None
    voice_platform: str = "elevenlabs"  # Phase 5 picks the adapter from this


# Restaurant registry is hardcoded for the pilot; Phase 7 replaces this with
# self-serve onboarding backed by Postgres.
RESTAURANTS: dict[str, RestaurantConfig] = {
    "+15551234567": RestaurantConfig(
        id="taqueria-demo",
        name="Taqueria Demo",
        called_number="+15551234567",
        pos_profile="square_like",
        catalog_path=FIXTURES_DIR / "menu_taqueria.json",
        hours="11am-9pm daily",
        pickup_lead_minutes=20,
        transfer_number="+15557654321",
    ),
}
RESTAURANTS_BY_ID: dict[str, RestaurantConfig] = {r.id: r for r in RESTAURANTS.values()}


def _build_backup_store(restaurant_id: str) -> BackupStore:
    dsn = os.environ.get("VOICEORDER_DATABASE_URL")
    if dsn:
        from ..storage.postgres_backup import PostgresBackupStore

        return PostgresBackupStore(dsn=dsn, restaurant_id=restaurant_id)
    return BackupScreen()


def _build_cart_store() -> CartStore:
    redis_url = os.environ.get("VOICEORDER_REDIS_URL")
    if redis_url:
        from ..storage.cart_store import RedisCartStore

        return RedisCartStore.from_url(redis_url)
    return InMemoryCartStore()


# Each of these is a restaurant/process-scoped resource -- shared across every
# call, exactly like a real POS and a real backup database would be. Only the
# cart (one per in-progress call) moves through the swappable CartStore.
_catalogs: dict[str, Catalog] = {
    r.id: Catalog.from_json_file(str(r.catalog_path)) for r in RESTAURANTS.values()
}
_pos_by_restaurant: dict[str, FakePos] = {
    r.id: FakePos(
        profile=r.pos_profile,
        catalog=_catalogs[r.id],
        pickup_time_text=f"{r.pickup_lead_minutes} minutes",
    )
    for r in RESTAURANTS.values()
}
_backup_screens: dict[str, BackupStore] = {
    r.id: _build_backup_store(r.id) for r in RESTAURANTS.values()
}
_cart_store: CartStore = _build_cart_store()


def _restaurant_for_number(called_number: str) -> RestaurantConfig:
    restaurant = RESTAURANTS.get(called_number)
    if restaurant is None:
        raise HTTPException(
            status_code=404, detail=f"no restaurant configured for {called_number}"
        )
    return restaurant


def _build_tools(restaurant: RestaurantConfig, cart: Cart) -> OrderTools:
    return OrderTools(
        cart=cart,
        catalog=_catalogs[restaurant.id],
        pos=_pos_by_restaurant[restaurant.id],
        max_quantity_per_line=restaurant.max_quantity_per_line,
        max_total_cents=restaurant.max_total_cents,
        backup=_backup_screens[restaurant.id],
        transfer_number=restaurant.transfer_number,
    )


def _load_tools(call_id: str) -> tuple[str, OrderTools]:
    loaded = _cart_store.load(call_id)
    if loaded is None:
        raise HTTPException(status_code=404, detail=f"no active call: {call_id}")
    restaurant_id, cart = loaded
    restaurant = RESTAURANTS_BY_ID[restaurant_id]
    return restaurant_id, _build_tools(restaurant, cart)


def _require_webhook_secret(authorization: str | None = Header(default=None)) -> None:
    """Section 9: every tool/webhook endpoint must check a secret. Enforced only
    once VOICEORDER_WEBHOOK_SECRET is set -- Phase 5 sets it from the voice
    platform's own webhook-auth config; local/dev runs stay open until then."""
    secret = os.environ.get("VOICEORDER_WEBHOOK_SECRET")
    if not secret:
        return
    if authorization != f"Bearer {secret}":
        raise HTTPException(status_code=401, detail="missing or invalid webhook secret")


app = FastAPI(title="VoiceOrderAI")


@app.post("/call/start", dependencies=[Depends(_require_webhook_secret)])
def call_start(payload: dict[str, Any]) -> dict[str, Any]:
    call_id = payload["call_id"]
    restaurant = _restaurant_for_number(payload["called_number"])
    _cart_store.save(call_id, restaurant.id, Cart(call_id=call_id))
    return {
        "restaurant_id": restaurant.id,
        "hours": restaurant.hours,
        "greeting": f"Thanks for calling {restaurant.name}, what can I get started for you?",
    }


@app.post("/tools/{tool_name}", dependencies=[Depends(_require_webhook_secret)])
def call_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    call_id = payload.get("call_id")
    if not call_id:
        raise HTTPException(status_code=400, detail="call_id is required")
    if tool_name not in ALLOWED_TOOLS:
        raise HTTPException(status_code=404, detail=f"unknown tool: {tool_name}")

    restaurant_id, tools = _load_tools(call_id)
    try:
        result = getattr(tools, tool_name)(**payload.get("arguments", {}))
    except ToolError as e:
        raise HTTPException(status_code=400, detail=e.message) from e

    _cart_store.save(call_id, restaurant_id, tools.cart)
    return result


@app.post("/call/end", dependencies=[Depends(_require_webhook_secret)])
def call_end(payload: dict[str, Any]) -> dict[str, Any]:
    _cart_store.delete(payload["call_id"])
    return {"ok": True}


@app.get("/backup-screen/{restaurant_id}")
def backup_screen(restaurant_id: str) -> dict[str, Any]:
    screen = _backup_screens.get(restaurant_id)
    if screen is None:
        raise HTTPException(status_code=404, detail=f"unknown restaurant: {restaurant_id}")
    return {
        "entries": [
            {
                "call_id": e.call_id,
                "kind": e.kind,
                "detail": e.detail,
                "created_at": e.created_at.isoformat(),
            }
            for e in screen.list_entries()
        ]
    }
