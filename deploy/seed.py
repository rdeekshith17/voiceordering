"""Seed the tenant DB on container/host first boot.

Creates the Hyderabad House tenant shell (name + Twilio number, no POS
credentials) when the database has no tenants yet. The owner then claims it
via the portal signup (entering the restaurant's Twilio number) and connects
Square/Toast/Clover through the portal's "Test connection" UI — so no
secrets are ever baked into images or repos.

Safe to run on every boot: it only creates when the tenant table is empty.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running as `python deploy/seed.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SEED_NAME = os.environ.get("SEED_RESTAURANT_NAME", "Hyderabad House")
SEED_PHONE = os.environ.get("SEED_PHONE_NUMBER", "+15622680097")


def main() -> None:
    from voiceorder.tenants.store import TenantStore

    db_path = os.environ.get("DATABASE_URL", "").strip() or Path(
        os.environ.get(
            "VOICEORDER_DB",
            str(Path(__file__).resolve().parent.parent / "data" / "voiceorder.db"),
        )
    )
    store = TenantStore(db_path)
    existing = store.get_tenant_by_number(SEED_PHONE) or store.get_tenant_by_name(
        SEED_NAME
    )
    if existing:
        print(f"seed: tenant already present ({existing.name})")
        return
    tenant = store.create_tenant(SEED_NAME, phone_number=SEED_PHONE)
    print(f"seed: created tenant '{tenant.name}' id={tenant.id} phone={SEED_PHONE}")


if __name__ == "__main__":
    main()
