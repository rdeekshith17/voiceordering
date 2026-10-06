#!/usr/bin/env python3
"""Cron runner for the VoiceOrderAI marketing agent.

Generates weekly promo drafts per tenant (drafts only, nothing is sent)
and prints a JSON summary. Exit 0 always.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from voiceorder.agents.marketing import run_marketing  # noqa: E402
from voiceorder.api.main import tenant_catalog, tenant_store  # noqa: E402


def main() -> None:
    drafts = run_marketing(tenant_store, get_catalog=tenant_catalog)
    print(json.dumps({
        "drafts_created": len(drafts),
        "tenants": sorted({d["tenant_id"] for d in drafts}),
    }))


if __name__ == "__main__":
    main()
