"""Unit tests for the Square catalog sync -- no live HTTP, no credentials."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from voiceorder.pos_adapters import square_catalog
from voiceorder.pos_adapters.square_catalog import (
    square_catalog_to_menu,
    sync_square_menu,
)


def _item(obj_id, name, variations, mod_info=(), description=""):
    return {
        "type": "ITEM",
        "id": obj_id,
        "item_data": {
            "name": name,
            "description_plaintext": description,
            "variations": [
                {
                    "type": "ITEM_VARIATION",
                    "id": f"{obj_id}-v{i}",
                    "item_variation_data": {
                        "name": vname,
                        "price_money": {"amount": cents, "currency": "USD"},
                        "sellable": True,
                    },
                }
                for i, (vname, cents) in enumerate(variations)
            ],
            "modifier_list_info": list(mod_info),
        },
    }


def _mod_list(obj_id, name, options):
    return {
        "type": "MODIFIER_LIST",
        "id": obj_id,
        "modifier_list_data": {
            "name": name,
            "modifiers": [
                {
                    "type": "MODIFIER",
                    "id": f"{obj_id}-m{i}",
                    "modifier_data": {
                        "name": oname,
                        "price_money": {"amount": cents, "currency": "USD"},
                    },
                }
                for i, (oname, cents) in enumerate(options)
            ],
        },
    }


def test_single_variation_becomes_base_price():
    objects = [_item("i1", "Chicken Biryani", [("Regular", 1699)])]
    menu = square_catalog_to_menu(objects)
    (entry,) = menu["items"]
    assert entry["ref"] == "M1"
    assert entry["name"] == "Chicken Biryani"
    assert entry["base_price"] == 16.99
    assert entry["variations"] == []
    assert "chicken biryani" in entry["aliases"]


def test_multiple_variations_become_size_options():
    objects = [
        _item("i2", "Chicken Curry", [("Small", 1299), ("Large", 1599)])
    ]
    menu = square_catalog_to_menu(objects)
    (entry,) = menu["items"]
    assert entry["base_price"] == 12.99
    assert [(v["name"], v["price_delta"]) for v in entry["variations"]] == [
        ("Small", 0.0),
        ("Large", 3.0),
    ]


def test_modifier_lists_become_modifier_groups():
    objects = [
        _item(
            "i3",
            "Masala Dosa",
            [("Regular", 1199)],
            mod_info=[
                {
                    "modifier_list_id": "ml1",
                    "min_selected_modifiers": 1,
                    "max_selected_modifiers": 1,
                    "enabled": True,
                }
            ],
        ),
        _mod_list("ml1", "Chutney", [("Coconut", 0), ("Tomato", 50)]),
    ]
    menu = square_catalog_to_menu(objects)
    (entry,) = menu["items"]
    (group,) = entry["modifier_groups"]
    assert group["name"] == "Chutney"
    assert group["required"] is True
    assert [(o["name"], o["price_delta"]) for o in group["options"]] == [
        ("Coconut", 0.0),
        ("Tomato", 0.5),
    ]


def test_items_without_sellable_variations_are_skipped():
    objects = [
        _item("i4", "Ghost Dish", []),
        _item("i5", "Real Dish", [("Regular", 999)]),
    ]
    menu = square_catalog_to_menu(objects)
    assert [e["name"] for e in menu["items"]] == ["Real Dish"]
    assert menu["items"][0]["ref"] == "M1"


def test_sync_writes_cache_and_falls_back(tmp_path):
    objects = [_item("i1", "Chicken Biryani", [("Regular", 1699)])]
    cache = tmp_path / "square_catalog.json"

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self._payload).encode()

    with patch(
        "voiceorder.pos_adapters.square_catalog.net.urlopen",
        return_value=_Resp({"objects": objects}),
    ):
        menu = sync_square_menu("tok", "sandbox", cache)
    assert menu["items"][0]["name"] == "Chicken Biryani"
    assert cache.exists()

    # Square down: the cache keeps the menu alive.
    with patch(
        "voiceorder.pos_adapters.square_catalog.net.urlopen",
        side_effect=OSError("down"),
    ):
        menu2 = sync_square_menu("tok", "sandbox", cache)
    assert menu2["items"][0]["name"] == "Chicken Biryani"

    # Square down and no cache: the failure surfaces.
    with patch(
        "voiceorder.pos_adapters.square_catalog.net.urlopen",
        side_effect=OSError("down"),
    ):
        try:
            sync_square_menu("tok", "sandbox", tmp_path / "missing.json")
        except OSError:
            pass
        else:
            raise AssertionError("expected OSError")


def test_from_dict_round_trip():
    from voiceorder.core.catalog import Catalog

    objects = [_item("i1", "Chicken Biryani", [("Regular", 1699)])]
    catalog = Catalog.from_dict(square_catalog_to_menu(objects))
    item = catalog.get("M1")
    assert item is not None and item.name == "Chicken Biryani"
    assert item.base_price == 16.99
    assert catalog.is_available("M1")
