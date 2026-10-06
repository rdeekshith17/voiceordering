"""search_menu: fuzzy match of what the caller said against the synced catalog.

Returns item refs -- never free text -- so the agent can only offer items that
actually exist.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .catalog import Catalog, Item


def _normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _score(query: str, candidate: str) -> float:
    q, c = _normalize(query), _normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c or c in q:
        return 0.92
    ratio = SequenceMatcher(None, q, c).ratio()
    q_tokens, c_tokens = set(q.split()), set(c.split())
    overlap = len(q_tokens & c_tokens) / max(len(q_tokens), 1)
    return max(ratio, overlap * 0.95)


def search_catalog(catalog: Catalog, query: str, limit: int = 5) -> list[dict]:
    """Return the top matching catalog items for a caller utterance."""
    scored: list[tuple[float, Item]] = []
    for item in catalog.all_items():
        candidates = [item.name, *item.aliases]
        best = max(_score(query, cand) for cand in candidates)
        if best >= 0.45:
            scored.append((best, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    hits: list[dict] = []
    for _, item in scored[:limit]:
        hits.append(
            {
                "ref": item.ref,
                "name": item.name,
                "price": item.base_price,
                "category": item.category,
                "description": item.description,
                "variations": [
                    {"id": v.id, "name": v.name, "price_delta": v.price_delta}
                    for v in item.variations
                ],
                "modifier_groups": [
                    {
                        "id": g.id,
                        "name": g.name,
                        "required": g.required,
                        "options": [
                            {"id": o.id, "name": o.name, "price_delta": o.price_delta}
                            for o in g.options
                        ],
                    }
                    for g in item.modifier_groups
                ],
            }
        )
    return hits
