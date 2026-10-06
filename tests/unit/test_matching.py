"""Menu matching tests: the agent can only offer items that exist."""
from __future__ import annotations

from voiceorder.core.matching import search_catalog


def top_ref(catalog, query):
    hits = search_catalog(catalog, query)
    return hits[0]["ref"] if hits else None


def test_exact_name_match(catalog):
    assert top_ref(catalog, "Steak Burrito") == "B2"


def test_alias_match(catalog):
    assert top_ref(catalog, "agua de horchata") == "D1"
    assert top_ref(catalog, "coke") == "D3"


def test_partial_and_fuzzy_match(catalog):
    assert top_ref(catalog, "steak burrito") == "B2"
    assert top_ref(catalog, "horchata") == "D1"
    assert top_ref(catalog, "chiken burrito") == "B1"  # misheard caller


def test_no_match_returns_empty(catalog):
    assert search_catalog(catalog, "pepperoni pizza") == []


def test_hits_carry_refs_sizes_and_modifiers(catalog):
    hits = search_catalog(catalog, "horchata")
    assert hits[0]["ref"] == "D1"
    assert {v["id"] for v in hits[0]["variations"]} == {"regular", "large"}
    hits = search_catalog(catalog, "chicken burrito")
    groups = {g["id"]: g for g in hits[0]["modifier_groups"]}
    assert groups["burrito_base"]["required"] is True
