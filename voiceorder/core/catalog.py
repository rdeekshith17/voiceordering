"""Menu catalog: items, variations, modifier groups, aliases.

Loaded from a JSON fixture in Phase 1; synced from the POS in Phase 5.
Prices live here -- the LLM prompt never sets a price.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Modifier:
    id: str
    name: str
    price_delta: float = 0.0


@dataclass(frozen=True)
class ModifierGroup:
    id: str
    name: str
    required: bool
    options: list[Modifier] = field(default_factory=list)


@dataclass(frozen=True)
class Variation:
    id: str
    name: str
    price_delta: float = 0.0


@dataclass
class Item:
    ref: str
    name: str
    description: str = ""
    category: str = ""
    base_price: float = 0.0
    aliases: list[str] = field(default_factory=list)
    variations: list[Variation] = field(default_factory=list)
    modifier_groups: list[ModifierGroup] = field(default_factory=list)
    available: bool = True

    def variation(self, variation_id: str) -> Variation | None:
        for v in self.variations:
            if v.id == variation_id:
                return v
        return None

    def modifier(self, modifier_id: str) -> Modifier | None:
        for group in self.modifier_groups:
            for option in group.options:
                if option.id == modifier_id:
                    return option
        return None

    def group_for_modifier(self, modifier_id: str) -> ModifierGroup | None:
        for group in self.modifier_groups:
            if any(o.id == modifier_id for o in group.options):
                return group
        return None


class Catalog:
    def __init__(self, items: list[Item], currency: str = "USD"):
        self._items: dict[str, Item] = {i.ref: i for i in items}
        self.currency = currency
        # Runtime 86/restore overrides (owner app). Wins over the fixture file;
        # never persisted -- the fixture stays the source of truth on restart.
        self._runtime_availability: dict[str, bool] = {}

    def get(self, ref: str) -> Item | None:
        return self._items.get(ref)

    def all_items(self) -> list[Item]:
        return list(self._items.values())

    def is_available(self, ref: str) -> bool:
        """Runtime availability: owner 86/restore wins, else the fixture flag."""
        if ref in self._runtime_availability:
            return self._runtime_availability[ref]
        item = self._items.get(ref)
        return bool(item and item.available)

    def set_available(self, ref: str, available: bool) -> bool:
        """86 (False) or restore (True) an item at runtime. Returns False for unknown refs."""
        if ref not in self._items:
            return False
        self._runtime_availability[ref] = available
        return True

    def availability_overrides(self) -> dict[str, bool]:
        return dict(self._runtime_availability)

    def categories(self) -> list[str]:
        seen: list[str] = []
        for item in self._items.values():
            if item.category and item.category not in seen:
                seen.append(item.category)
        return seen

    @classmethod
    def from_json(cls, path: str | Path) -> "Catalog":
        raw: dict[str, Any] = json.loads(Path(path).read_text())
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Catalog":
        items: list[Item] = []
        for entry in raw.get("items", []):
            items.append(
                Item(
                    ref=entry["ref"],
                    name=entry["name"],
                    description=entry.get("description", ""),
                    category=entry.get("category", ""),
                    base_price=float(entry.get("base_price", 0.0)),
                    aliases=list(entry.get("aliases", [])),
                    variations=[
                        Variation(
                            id=v["id"],
                            name=v["name"],
                            price_delta=float(v.get("price_delta", 0.0)),
                        )
                        for v in entry.get("variations", [])
                    ],
                    modifier_groups=[
                        ModifierGroup(
                            id=g["id"],
                            name=g["name"],
                            required=bool(g.get("required", False)),
                            options=[
                                Modifier(
                                    id=o["id"],
                                    name=o["name"],
                                    price_delta=float(o.get("price_delta", 0.0)),
                                )
                                for o in g.get("options", [])
                            ],
                        )
                        for g in entry.get("modifier_groups", [])
                    ],
                    available=bool(entry.get("available", True)),
                )
            )
        return cls(items, currency=raw.get("currency", "USD"))
