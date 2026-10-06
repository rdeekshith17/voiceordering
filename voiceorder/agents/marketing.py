"""MarketingAgent: weekly promo/offer draft generation per tenant.

Template-driven and fully deterministic — no LLM, no network, no
credentials. Draft copy is seeded by (tenant_id, ISO week) so a run always
produces the same drafts for the same week, and a weekly re-run is
idempotent (tenants with drafts from the last 7 days are skipped).

Sources: the tenant's real menu (via an injected get_catalog callable) and
their top-selling items from order history (store.top_selling_items).

Drafts are stored in `marketing_drafts` with status='draft' and are NEVER
sent anywhere — sending is a future, explicitly human-approved step.

Optional LLM hook: pass llm_rewrite(text) -> text to polish the body copy
(e.g. with the Anthropic client once a key is wired). The default path
(llm_rewrite=None) works with zero credentials and is what the tests cover.
"""
from __future__ import annotations

import hashlib
import logging
import random
import time
from typing import Any, Callable

from ..tenants.store import Tenant, TenantStore

log = logging.getLogger("voiceorder.agents.marketing")

CHANNELS = ("sms", "social", "in-store")
POS_PROVIDERS = ("square", "toast", "clover")
DRAFTS_PER_TENANT = 3
WEEK_SECONDS = 7 * 86400

LlmRewrite = Callable[[str], str]
GetCatalog = Callable[[Tenant], Any]
CheckPos = Callable[[Tenant], bool]


def _week_key(now: float) -> str:
    return time.strftime("%G-W%V", time.gmtime(now))


def _rng(tenant_id: str, week_key: str) -> random.Random:
    seed = int(hashlib.sha256(f"{tenant_id}:{week_key}".encode()).hexdigest(), 16)
    return random.Random(seed)


def _default_check_pos(store: TenantStore, tenant: Tenant) -> bool:
    provider = tenant.setting("pos_profile", "")
    return provider in POS_PROVIDERS and store.has_secret(tenant.id, provider)


def _menu_names(get_catalog: GetCatalog | None, tenant: Tenant) -> list[str]:
    if get_catalog is None:
        return []
    try:
        catalog = get_catalog(tenant)
    except Exception:
        return []
    if catalog is None:
        return []
    try:
        items = catalog.all_items()
    except AttributeError:
        return []
    return [i.name for i in items if getattr(i, "available", True) and i.name]


def generate_drafts(
    tenant: Tenant,
    menu_names: list[str],
    top_sellers: list[dict],
    week_key: str,
    llm_rewrite: LlmRewrite | None = None,
) -> list[dict]:
    """Pure, deterministic draft generation. Returns [{title, body, channel}]."""
    restaurant = tenant.setting("restaurant_name", "") or tenant.name
    phone = tenant.setting("phone_number", "") or tenant.phone_number
    mins = tenant.setting("pickup_minutes", "") or "20"

    # Hero items: best sellers first, then menu variety, deduped, stable order.
    heroes: list[str] = []
    for s in top_sellers:
        if s.get("name") and s["name"] not in heroes:
            heroes.append(s["name"])
    rng = _rng(tenant.id, week_key)
    rest = [n for n in menu_names if n not in heroes]
    rng.shuffle(rest)
    heroes.extend(rest)
    heroes = heroes[: max(DRAFTS_PER_TENANT, 1)] or ["today's special"]

    orders_of = {s["name"]: s.get("qty", 0) for s in top_sellers}

    templates = [
        ("sms",
         "Order ahead: {item} tonight?",
         "Craving {item}? {restaurant} has it ready — call {phone} to order "
         "ahead, pickup in ~{mins} min."),
        ("social",
         "This week's favorite at {restaurant}",
         "🔥 {item} is flying out the door — {n} orders this month and counting. "
         "Tag someone who owes you dinner 👇"),
        ("in-store",
         "Staff pick",
         "Don't skip the {item} — it's one of our most-ordered dishes. "
         "Ask your server about it today!"),
    ]

    drafts = []
    for i, (channel, title_t, body_t) in enumerate(templates[:DRAFTS_PER_TENANT]):
        item = heroes[i % len(heroes)]
        n = orders_of.get(item, 0)
        title = title_t.format(item=item, restaurant=restaurant)
        body = body_t.format(item=item, restaurant=restaurant, phone=phone,
                             mins=mins, n=n)
        if llm_rewrite is not None:
            try:
                body = llm_rewrite(body)
            except Exception as exc:
                log.warning("marketing: llm_rewrite failed, keeping template: %s", exc)
        drafts.append({"title": title, "body": body, "channel": channel})
    return drafts


def run_marketing(
    store: TenantStore,
    get_catalog: GetCatalog | None = None,
    check_pos: CheckPos | None = None,
    llm_rewrite: LlmRewrite | None = None,
    now: float | None = None,
    per_tenant: int = DRAFTS_PER_TENANT,
) -> list[dict]:
    """Weekly run. Returns the drafts saved this run (usually 3 per tenant)."""
    now = now if now is not None else time.time()
    week_key = _week_key(now)
    pos_ok = check_pos or (lambda t: _default_check_pos(store, t))
    saved: list[dict] = []

    for tenant in store.list_tenants():
        if not pos_ok(tenant):
            continue
        # Weekly idempotency: don't re-generate for tenants drafted recently.
        if store.drafts_since(tenant.id, now - WEEK_SECONDS):
            continue
        menu = _menu_names(get_catalog, tenant)
        sellers = store.top_selling_items(tenant.id, days=30, limit=5, now=now)
        if not menu and not sellers:
            log.info("marketing: tenant %s has no menu/order data, skipped", tenant.id)
            continue
        for d in generate_drafts(tenant, menu, sellers, week_key, llm_rewrite)[:per_tenant]:
            saved.append(store.save_draft(tenant.id, d["title"], d["body"],
                                          d["channel"], now=now))
    return saved
