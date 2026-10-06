#!/usr/bin/env python3
"""Cron runner for the VoiceOrderAI menu-sync agent.

Re-runs the vendor catalog sync for every tenant whose POS check passes
and prints per-tenant results as JSON. Tenants whose POS check fails are
skipped quietly (the support agent owns POS-down alerting).
Exit 0 always.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from voiceorder.agents.menusync import run_menu_sync  # noqa: E402
from voiceorder.api.main import tenant_pos_adapter, tenant_store  # noqa: E402


def _pos_ok(tenant) -> bool:
    """Live POS check: the tenant is syncable only if a ping succeeds."""
    try:
        tenant_pos_adapter(tenant).ping()
        return True
    except Exception:
        return False


def main() -> None:
    results = run_menu_sync(tenant_store, check_pos=_pos_ok)
    print(json.dumps({"results": results}))


if __name__ == "__main__":
    main()
