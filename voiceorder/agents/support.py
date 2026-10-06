"""SupportAgent: background health monitor for the multi-tenant platform.

Each run checks, in order:

(a) platform reachability — GET {PUBLIC_BASE_URL}/health (falls back to
    http://127.0.0.1:8000/health). Unreachable -> platform-wide critical
    ticket (kind='platform_down', tenant_id=None).
(b) per-tenant POS health — for every active tenant with a configured
    square/toast/clover provider and stored credentials, build the adapter
    (injected, same signature as the portal's build_adapter) and call its
    read-only ping(): the exact check behind the portal's "Test connection".
    Never logs secrets; pings are read-only (no charges, no orders).
(c) recent call failures — call_transcripts rows in the last 60 minutes
    with an error status ('failed'/'error') -> warning ticket per tenant.

Findings go into the `tickets` table. Dedupe: never opens a second
open/acked ticket with the same tenant_id+kind. Recovery auto-resolves:
when a check passes again, the matching ticket flips to resolved.

Scheduling is owned by ops (cron). This module only exposes run_support_check().
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.request
from typing import Any, Callable

from .. import net
from ..tenants.store import Tenant, TenantStore

log = logging.getLogger("voiceorder.agents.support")

POS_PROVIDERS = ("square", "toast", "clover")
CALL_ERROR_STATUSES = {"failed", "error"}
CALL_WINDOW_SECONDS = 60 * 60

# Token-shaped substrings are scrubbed before anything hits a ticket/log:
# vendor errors must never carry a credential into stored text.
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("[redacted]", str(text))

PlatformProbe = Callable[[], "tuple[bool, str]"]
BuildAdapter = Callable[[Tenant, dict | None], Any]


def check_platform(public_base_url: str | None = None, timeout: float = 10.0) -> tuple[bool, str]:
    """Probe the public /health endpoint. Returns (ok, detail)."""
    base = (public_base_url or os.environ.get("PUBLIC_BASE_URL")
            or "http://127.0.0.1:8000").rstrip("/")
    url = base + "/health"
    try:
        req = urllib.request.Request(url, method="GET")
        with net.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", 200)
        if code == 200:
            return True, "ok"
        return False, f"HTTP {code}"
    except Exception as exc:  # network down, tunnel dead, DNS, ...
        return False, _redact(str(exc))[:200]


def check_tenant_pos(store: TenantStore, build_adapter: BuildAdapter,
                     tenant: Tenant) -> tuple[bool, str]:
    """Read-only POS ping via the portal's adapter builder. (ok, detail)."""
    provider = tenant.setting("pos_profile", "")
    if provider not in POS_PROVIDERS:
        return True, "no POS provider configured"
    if not store.has_secret(tenant.id, provider):
        return True, "no POS credentials stored yet"
    try:
        adapter = build_adapter(tenant, None)
        detail = adapter.ping() or {}
    except Exception as exc:
        # Vendor error text is redacted: a credential must never land in a ticket.
        return False, f"{provider} ping failed: {_redact(str(exc))[:200]}"
    name = detail.get("location_name") or detail.get("merchant_name") or ""
    return True, f"{provider} ok" + (f" ({name})" if name else "")


def check_recent_call_failures(store: TenantStore, tenant: Tenant,
                               since_ts: float) -> tuple[int, list[str]]:
    """Count errored calls for a tenant since since_ts. Returns (n, call_sids)."""
    rows = store.calls_in_window(tenant.id, since_ts)
    bad = [r for r in rows if (r["status"] or "").lower() in CALL_ERROR_STATUSES]
    return len(bad), [r["call_sid"] for r in bad]


def run_support_check(
    store: TenantStore,
    build_adapter: BuildAdapter | None = None,
    public_base_url: str | None = None,
    platform_probe: PlatformProbe | None = None,
    now: float | None = None,
    call_window_seconds: int = CALL_WINDOW_SECONDS,
) -> list[dict]:
    """Run one monitoring pass. Returns the list of newly opened tickets."""
    now = now if now is not None else time.time()
    probe = platform_probe or (lambda: check_platform(public_base_url))
    new_tickets: list[dict] = []

    # (a) platform reachability -------------------------------------------
    ok, detail = probe()
    if ok:
        store.resolve_ticket(None, "platform_down", now=now)
    else:
        t = store.open_ticket(
            None, "platform_down", "critical",
            "Platform unreachable",
            f"GET /health failed: {detail}", now=now)
        if t:
            log.warning("support: platform_down ticket opened: %s", detail)
            new_tickets.append(t)

    # (b)+(c) per tenant ---------------------------------------------------
    for tenant in store.list_tenants():
        # (b) POS health
        if build_adapter is not None:
            ok, detail = check_tenant_pos(store, build_adapter, tenant)
            provider = tenant.setting("pos_profile", "")
            if ok:
                store.resolve_ticket(tenant.id, "pos_down", now=now)
            else:
                t = store.open_ticket(
                    tenant.id, "pos_down", "critical",
                    f"{provider.title()} connection down",
                    detail, now=now)
                if t:
                    log.warning("support: pos_down for tenant %s: %s", tenant.id, detail)
                    new_tickets.append(t)
        # (c) recent call failures
        n_bad, sids = check_recent_call_failures(
            store, tenant, now - call_window_seconds)
        if n_bad:
            sev = "critical" if n_bad >= 5 else "warning"
            t = store.open_ticket(
                tenant.id, "call_failures", sev,
                f"{n_bad} failed call{'s' if n_bad != 1 else ''} in the last hour",
                "call_sids: " + ", ".join(sids[:10]), now=now)
            if t:
                new_tickets.append(t)
        else:
            store.resolve_ticket(tenant.id, "call_failures", now=now)

    return new_tickets
