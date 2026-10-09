#!/usr/bin/env python3
"""Create a platform super admin account.

Platform accounts have no web signup on purpose. Run this where the app's
database is reachable, e.g. locally with the production DATABASE_URL:

    DATABASE_URL=postgresql://... python scripts/create_platform_user.py ops@example.com super_admin

The password is prompted for and never echoed or logged.
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voiceorder.tenants import rbac  # noqa: E402
from voiceorder.tenants.store import TenantStore  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[2] not in rbac.PLATFORM_ROLES:
        print(f"usage: {sys.argv[0]} EMAIL {{{'|'.join(rbac.PLATFORM_ROLES)}}}")
        return 2
    target = os.environ.get("DATABASE_URL", "").strip() or os.environ.get(
        "VOICEORDER_DB", str(Path(__file__).resolve().parent.parent / "data" / "voiceorder.db"))
    password = getpass.getpass("Password (12+ characters): ")
    if password != getpass.getpass("Repeat password: "):
        print("passwords don't match")
        return 1
    store = TenantStore(target)
    try:
        user = store.create_platform_user(sys.argv[1], password, sys.argv[2])
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    store.audit("cli", user.id, None, "platform_user.created", f"platform_user:{user.id}",
                None, {"email": user.email, "role": user.role})
    print(f"created {user.role} {user.email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
