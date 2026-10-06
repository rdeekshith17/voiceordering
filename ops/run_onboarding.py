#!/usr/bin/env python3
"""Cron runner for the VoiceOrderAI onboarding agent.

Runs one stalled-signup detection pass and prints newly opened tickets
as JSON.
Exit 0 always; an empty new_tickets list means all quiet.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from voiceorder.agents.onboarding import run_onboarding_check  # noqa: E402
from voiceorder.api.main import tenant_store  # noqa: E402


def main() -> None:
    new_tickets = run_onboarding_check(tenant_store)
    print(json.dumps({
        "new_tickets": [
            {"id": t["id"], "tenant_id": t["tenant_id"], "kind": t["kind"],
             "severity": t["severity"], "title": t["title"]}
            for t in new_tickets
        ],
    }))


if __name__ == "__main__":
    main()
