from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class ModifierOption:
    id: str
    name: str
    price_delta_cents: int = 0


@dataclass
class ModifierGroup:
    id: str
    name: str
    required: bool = False
    min_select: int = 0
    max_select: int = 1
    options: list[ModifierOption] = field(default_factory=list)

    def option(self, option_id: str) -> ModifierOption | None:
        return next((o for o in self.options if o.id == option_id), None)


@dataclass
class Variation:
    id: str
    name: str
    price_cents: int


@dataclass
class Item:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    variations: list[Variation] = field(default_factory=list)
    modifier_groups: list[ModifierGroup] = field(default_factory=list)
    available: bool = True

    def variation(self, variation_id: str) -> Variation | None:
        return next((v for v in self.variations if v.id == variation_id), None)

    def modifier_group(self, group_id: str) -> ModifierGroup | None:
        return next((g for g in self.modifier_groups if g.id == group_id), None)

    def search_text(self) -> list[str]:
        return [self.name, *self.aliases]


@dataclass
class Catalog:
    items: list[Item]

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)

    @classmethod
    def from_dict(cls, data: dict) -> Catalog:
        definitions = data.get("modifier_group_definitions", {})
        items = []
        for raw in data["items"]:
            variations = [Variation(**v) for v in raw.get("variations", [])]
            groups = []
            for g in raw.get("modifier_groups", []):
                if "$ref" in g:
                    g = definitions[g["$ref"]]
                options = [ModifierOption(**o) for o in g.get("options", [])]
                groups.append(
                    ModifierGroup(
                        id=g["id"],
                        name=g["name"],
                        required=g.get("required", False),
                        min_select=g.get("min_select", 0),
                        max_select=g.get("max_select", 1),
                        options=options,
                    )
                )
            items.append(
                Item(
                    id=raw["id"],
                    name=raw["name"],
                    aliases=raw.get("aliases", []),
                    variations=variations,
                    modifier_groups=groups,
                    available=raw.get("available", True),
                )
            )
        return cls(items=items)

    @classmethod
    def from_json_file(cls, path: str) -> Catalog:
        with open(path) as f:
            return cls.from_dict(json.load(f))
