"""MenuSyncAgent: keep each tenant's menu fresh from their POS.

Runs every 6h. For every tenant whose POS check passes, re-run the
vendor catalog sync and record (menu_sync_last_at, menu_sync_item_count)
in tenant settings.

Reuse, not reimplementation: Square tenants re-run the existing
square_catalog.sync_square_menu (it writes the per-tenant cache and
falls back to cache when the vendor is unreachable). Toast/Clover keep
the app-owned conversational menu — their adapters' sync_catalog only
refreshes the vendor-side GUID index used at order time, so the agent
rebuilds the adapter and calls it.

Safety rules:
- Never sync a tenant whose POS check fails — skip quietly (the support
  agent already owns POS-down alerting via its pos_down ticket).
- A sync exception opens one ticket (kind='menu_sync_failed',
  severity='warning'), deduped by open_ticket; token-shaped text is
  redacted from the detail.
- Recovery is implicit: a later successful run overwrites the failure
  ticket? No — the failure ticket stays open until a human closes it,
  because a silently-self-healing sync failure hides vendor flakiness.
  The fresh menu_sync_last_at setting is the "healthy again" signal.

Scheduling is owned by ops (cron). This module only exposes run_menu_sync().
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Callable

from ..tenants.store import Tenant, TenantStore

log = logging.getLogger("voiceorder.agents.menusync")

POS_PROVIDERS = ("square", "toast", "clover")

_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")

CheckPos = Callable[[Tenant], bool]
SyncCatalog = Callable[[Tenant], int]  # -> item count


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("[redacted]", str(text))


def _default_check_pos(store: TenantStore, tenant: Tenant) -> bool:
    provider = tenant.setting("pos_profile", "")
    return provider in POS_PROVIDERS and store.has_secret(tenant.id, provider)


def _default_sync_catalog(store: TenantStore, tenant: Tenant) -> int:
    """Re-run the vendor catalog sync for a tenant. Returns item count.

    Lazy-imports api.main so importing this module never starts the app.
    """
    from ..api.main import tenant_catalog, tenant_context, tenant_pos_adapter

    provider = tenant.setting("pos_profile", "")
    if provider == "square":
        from ..pos_adapters.square_catalog import sync_square_menu

        creds = store.get_secret(tenant.id, "square")
        cache = Path("data") / "menus" / f"{tenant.id}.json"
        menu = sync_square_menu(
            creds["access_token"], creds.get("environment", "production"), cache
        )
        return len(menu.get("items", []))
    # toast/clover: app owns the menu; adapter.sync_catalog refreshes the
    # vendor-side GUID index. Item count comes from the tenant catalog.
    adapter = tenant_pos_adapter(tenant)
    adapter.sync_catalog(tenant_context(tenant))
    return len(tenant_catalog(tenant).all_items())


def run_menu_sync(
    store: TenantStore,
    sync_catalog: SyncCatalog | None = None,
    check_pos: CheckPos | None = None,
    now: float | None = None,
) -> list[dict]:
    """6h run. Returns per-tenant results: {tenant_id, ok, item_count}."""
    now = now if now is not None else time.time()
    pos_ok = check_pos or (lambda t: _default_check_pos(store, t))
    do_sync = sync_catalog or (lambda t: _default_sync_catalog(store, t))
    results: list[dict] = []

    for tenant in store.list_tenants():
        if not pos_ok(tenant):
            continue  # skip quietly; support's pos_down ticket owns this case
        provider = tenant.setting("pos_profile", "")
        try:
            count = do_sync(tenant)
        except Exception as exc:
            detail = _redact(str(exc))[:300]
            t = store.open_ticket(
                tenant.id, "menu_sync_failed", "warning",
                f"{provider.title()} menu sync failed",
                detail, now=now)
            log.warning("menusync: tenant %s sync failed: %s", tenant.id, detail)
            results.append({"tenant_id": tenant.id, "ok": False,
                            "item_count": 0, "ticket_id": t["id"] if t else None})
            continue
        store.set_settings(tenant.id, {
            "menu_sync_last_at": str(now),
            "menu_sync_item_count": str(count),
        })
        log.info("menusync: tenant %s synced %d items", tenant.id, count)
        results.append({"tenant_id": tenant.id, "ok": True, "item_count": count})
    return results
