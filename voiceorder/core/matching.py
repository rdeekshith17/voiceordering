from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .catalog import Catalog, Item


@dataclass
class MenuMatch:
    item: Item
    score: float


def _best_score(query: str, candidates: list[str]) -> float:
    query = query.lower().strip()
    best = 0.0
    for candidate in candidates:
        candidate = candidate.lower().strip()
        if not candidate:
            continue
        ratio = SequenceMatcher(None, query, candidate).ratio()
        if candidate in query or query in candidate:
            ratio = max(ratio, 0.85)
        best = max(best, ratio)
    return best


def search_menu(
    query: str, catalog: Catalog, top_k: int = 5, min_score: float = 0.45
) -> list[MenuMatch]:
    scored = []
    for item in catalog.items:
        if not item.available:
            continue
        score = _best_score(query, item.search_text())
        if score >= min_score:
            scored.append(MenuMatch(item=item, score=score))
    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:top_k]
