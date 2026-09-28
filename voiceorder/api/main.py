from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from ..core.cart import Cart
from ..core.catalog import Catalog
from ..core.tools import OrderTools, ToolError
from ..pos_adapters.fake import FakePos

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


@dataclass
class RestaurantConfig:
    id: str
    name: str
    called_number: str
    pos_profile: str
    catalog_path: Path


# Restaurant registry is hardcoded for the pilot; Phase 7 replaces this with
# self-serve onboarding backed by Postgres.
RESTAURANTS: dict[str, RestaurantConfig] = {
    "+15551234567": RestaurantConfig(
        id="taqueria-demo",
        name="Taqueria Demo",
        called_number="+15551234567",
        pos_profile="square_like",
        catalog_path=FIXTURES_DIR / "menu_taqueria.json",
    ),
}


@dataclass
class CallSession:
    tools: OrderTools
    restaurant: RestaurantConfig


# In-memory for the fakes-only phase; Phase 1's checklist calls for Redis here
# once the app talks to real vendors.
_sessions: dict[str, CallSession] = {}


def _restaurant_for_number(called_number: str) -> RestaurantConfig:
    restaurant = RESTAURANTS.get(called_number)
    if restaurant is None:
        raise HTTPException(
            status_code=404, detail=f"no restaurant configured for {called_number}"
        )
    return restaurant


def _session(call_id: str) -> CallSession:
    session = _sessions.get(call_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no active call: {call_id}")
    return session


app = FastAPI(title="VoiceOrderAI")


@app.post("/call/start")
def call_start(payload: dict[str, Any]) -> dict[str, Any]:
    call_id = payload["call_id"]
    restaurant = _restaurant_for_number(payload["called_number"])
    catalog = Catalog.from_json_file(str(restaurant.catalog_path))
    pos = FakePos(profile=restaurant.pos_profile, catalog=catalog)
    cart = Cart(call_id=call_id)
    _sessions[call_id] = CallSession(
        tools=OrderTools(cart=cart, catalog=catalog, pos=pos), restaurant=restaurant
    )
    return {
        "restaurant_id": restaurant.id,
        "greeting": f"Thanks for calling {restaurant.name}, what can I get started for you?",
    }


@app.post("/tools/{tool_name}")
def call_tool(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    call_id = payload.get("call_id")
    if not call_id:
        raise HTTPException(status_code=400, detail="call_id is required")
    session = _session(call_id)
    method = getattr(session.tools, tool_name, None)
    if method is None or tool_name.startswith("_"):
        raise HTTPException(status_code=404, detail=f"unknown tool: {tool_name}")
    try:
        return method(**payload.get("arguments", {}))
    except ToolError as e:
        raise HTTPException(status_code=400, detail=e.message) from e


@app.post("/call/end")
def call_end(payload: dict[str, Any]) -> dict[str, Any]:
    _sessions.pop(payload["call_id"], None)
    return {"ok": True}
