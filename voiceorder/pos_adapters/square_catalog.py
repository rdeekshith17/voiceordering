"""Sync the restaurant's real Square catalog into the app's menu format.

The app takes orders against the local catalog (refs like M1, modifier
groups, price deltas); Square stays the order/payment backend. This module
pulls the merchant's Square catalog (items, variations, modifier lists)
and converts it to the same fixture shape Catalog.from_json reads, so the
voice agent knows the restaurant's real dishes instead of the demo menu.

Sync runs at startup when POS_PROFILE=square (unless SQUARE_MENU_SYNC=0);
the converted menu is cached to data/square_catalog.json so a Square
outage or restart doesn't lose the menu.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path

from .. import net

log = logging.getLogger("voiceorder.pos.square_catalog")

SQUARE_VERSION = "2024-12-18"
_BASE_URLS = {
    "sandbox": "https://connect.squareupsandbox.com",
    "production": "https://connect.squareup.com",
}


def fetch_catalog_objects(access_token: str, environment: str = "sandbox") -> list[dict]:
    """Paginated fetch of ITEM and MODIFIER_LIST catalog objects."""
    base = _BASE_URLS[environment]
    objects: list[dict] = []
    cursor: str | None = None
    while True:
        path = "/v2/catalog/list?types=ITEM%2CMODIFIER_LIST"
        if cursor:
            path += f"&cursor={cursor}"
        req = urllib.request.Request(
            base + path,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Square-Version": SQUARE_VERSION,
            },
            method="GET",
        )
        with net.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read() or b"{}")
        objects.extend(payload.get("objects", []))
        cursor = payload.get("cursor")
        if not cursor:
            break
    return objects


def _dollars(money: dict | None) -> float:
    if not money or money.get("amount") is None:
        return 0.0
    return round(money["amount"] / 100, 2)


def square_catalog_to_menu(objects: list[dict]) -> dict:
    """Convert Square catalog objects to the app's menu fixture shape."""
    mod_lists: dict[str, dict] = {}
    items: list[dict] = []
    for obj in objects:
        if obj.get("type") == "MODIFIER_LIST":
            mod_lists[obj["id"]] = obj.get("modifier_list_data", {})
        elif obj.get("type") == "ITEM":
            items.append(obj)

    menu_items: list[dict] = []
    n = 0
    for obj in items:
        data = obj.get("item_data", {})
        name = data.get("name", "").strip()
        if not name:
            continue
        variations = [
            v
            for v in data.get("variations", [])
            if (v.get("item_variation_data") or {}).get("sellable", True)
        ]
        if not variations:
            continue  # nothing orderable
        n += 1
        prices = [
            (_dollars((v.get("item_variation_data") or {}).get("price_money")), v)
            for v in variations
        ]
        prices.sort(key=lambda pv: pv[0])
        base_price = prices[0][0]
        entry: dict = {
            "ref": f"M{n}",
            "name": name,
            "description": (
                data.get("description_plaintext") or data.get("description") or ""
            ).strip(),
            "category": _category_name(data),
            "base_price": base_price,
            "aliases": [name.lower()] if name.lower() != name else [],
            "variations": [],
            "modifier_groups": [],
        }
        if len(prices) > 1:
            entry["variations"] = [
                {
                    "id": v.get("id", f"var-{i}"),
                    "name": (v.get("item_variation_data") or {}).get("name")
                    or f"Option {i + 1}",
                    "price_delta": round(price - base_price, 2),
                }
                for i, (price, v) in enumerate(prices)
            ]
        for info in data.get("modifier_list_info", []):
            if not info.get("enabled", True):
                continue
            ml = mod_lists.get(info.get("modifier_list_id", ""))
            if not ml:
                continue
            options = []
            for mod in ml.get("modifiers", []):
                md = mod.get("modifier_data", {})
                if not md.get("name"):
                    continue
                options.append(
                    {
                        "id": mod.get("id", ""),
                        "name": md["name"],
                        "price_delta": _dollars(md.get("price_money")),
                    }
                )
            if not options:
                continue
            entry["modifier_groups"].append(
                {
                    "id": info["modifier_list_id"],
                    "name": ml.get("name") or "Options",
                    "required": int(info.get("min_selected_modifiers") or 0) >= 1,
                    "options": options,
                }
            )
        menu_items.append(entry)
    return {"currency": "USD", "items": menu_items}


def _category_name(data: dict) -> str:
    for cat in data.get("categories") or []:
        name = (cat or {}).get("name")
        if name:
            return str(name)
    return "Menu"


def sync_square_menu(
    access_token: str, environment: str, cache_path: str | Path
) -> dict:
    """Fetch the Square catalog, convert it, and cache it locally.

    Returns the menu dict. On any failure, falls back to the cache file;
    raises if there is no cache either.
    """
    cache = Path(cache_path)
    try:
        objects = fetch_catalog_objects(access_token, environment)
        menu = square_catalog_to_menu(objects)
        if not menu["items"]:
            raise ValueError("Square catalog returned no orderable items")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(menu, indent=1))
        log.info(
            "Square menu synced: %d items -> %s", len(menu["items"]), cache
        )
        return menu
    except Exception as exc:
        log.warning("Square catalog sync failed: %s", exc)
        if cache.exists():
            log.info("Falling back to cached Square menu at %s", cache)
            return json.loads(cache.read_text())
        raise
