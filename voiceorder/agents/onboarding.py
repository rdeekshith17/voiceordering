"""OnboardingAgent: detect tenants that signed up but never got going.

A tenant is "stalled" when it was created more than STALL_AFTER_DAYS ago
and any of these hold:

- no POS provider configured in settings, or
- no POS credentials saved for the configured provider (check_pos hook
  also accepts a live ping, so "saved but broken" counts as stalled), or
- zero calls and zero orders to date.

One ticket per tenant (kind='onboarding_stalled', severity='info'):
open_ticket dedupes, so the daily run never spams. When the tenant
recovers (POS works and they have activity), the ticket auto-resolves —
that's the "they made it" signal.

Scheduling is owned by ops (cron). This module only exposes
run_onboarding_check().
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from ..tenants.store import Tenant, TenantStore

log = logging.getLogger("voiceorder.agents.onboarding")

STALL_AFTER_DAYS = 3
POS_PROVIDERS = ("square", "toast", "clover")

CheckPos = Callable[[Tenant], bool]


def _default_check_pos(store: TenantStore, tenant: Tenant) -> bool:
    provider = tenant.setting("pos_profile", "")
    return provider in POS_PROVIDERS and store.has_secret(tenant.id, provider)


def _stall_reasons(store: TenantStore, tenant: Tenant,
                   pos_ok: Callable[[Tenant], bool]) -> list[str]:
    reasons: list[str] = []
    provider = tenant.setting("pos_profile", "")
    if provider not in POS_PROVIDERS:
        reasons.append("no POS provider configured")
    elif not pos_ok(tenant):
        reasons.append(f"{provider} not connected (no saved credentials or ping failed)")
    if not store.calls_in_window(tenant.id, 0) and store.order_count(tenant.id) == 0:
        reasons.append("zero calls and zero orders so far")
    return reasons


def run_onboarding_check(
    store: TenantStore,
    check_pos: CheckPos | None = None,
    now: float | None = None,
) -> list[dict]:
    """Daily run. Returns newly opened onboarding tickets (usually none)."""
    now = now if now is not None else time.time()
    cutoff = now - STALL_AFTER_DAYS * 86400
    pos_ok = check_pos or (lambda t: _default_check_pos(store, t))
    new_tickets: list[dict] = []

    for tenant in store.list_tenants():
        created = store.tenant_created_at(tenant.id)
        if created is None or created > cutoff:
            continue  # brand-new; give them time
        reasons = _stall_reasons(store, tenant, pos_ok)
        if not reasons:
            store.resolve_ticket(tenant.id, "onboarding_stalled", now=now)
            continue
        t = store.open_ticket(
            tenant.id, "onboarding_stalled", "info",
            "Onboarding stalled",
            f"Signed up over {STALL_AFTER_DAYS} days ago but:\n"
            + "\n".join(f"- {r}" for r in reasons), now=now)
        if t:
            log.info("onboarding: stalled ticket for tenant %s: %s",
                     tenant.id, reasons)
            new_tickets.append(t)
    return new_tickets
