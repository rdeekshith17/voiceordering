from __future__ import annotations

from voiceorder.core.matching import search_menu


def test_exact_alias_match(catalog):
    matches = search_menu("horchata", catalog)
    assert matches[0].item.id == "horchata"


def test_fuzzy_typo_match(catalog):
    matches = search_menu("steak buritto", catalog)
    assert matches[0].item.id == "burrito-steak"


def test_no_match_for_off_menu_item(catalog):
    matches = search_menu("pizza slice", catalog)
    assert matches == []


def test_unavailable_items_are_excluded(catalog):
    catalog.item("coke").available = False
    matches = search_menu("coke", catalog)
    assert all(m.item.id != "coke" for m in matches)
