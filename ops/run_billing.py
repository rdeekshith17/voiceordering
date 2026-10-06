#!/usr/bin/env python3
"""Cron runner for the VoiceOrderAI billing agent.

Rolls up yesterday's per-tenant usage (calls, talk minutes, TTS chars, SMS)
into usage_daily and prints a JSON summary. Exit 0 always; idempotent.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from voiceorder.agents.billing import rollup_day  # noqa: E402
from voiceorder.api.main import tenant_store  # noqa: E402


def main() -> None:
    rows = rollup_day(tenant_store)
    print(json.dumps({
        "day": rows[0]["date"] if rows else None,
        "tenants_rolled_up": len(rows),
    }))


if __name__ == "__main__":
    main()
