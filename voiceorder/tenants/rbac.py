"""Who may do what: restaurant roles, platform roles, and feature flags.

Restaurant roles apply inside one tenant (the session's tenant). Platform
roles belong to a separate identity (platform_users) and are never derived
from a restaurant login. All checks happen server-side.
"""
from __future__ import annotations

import os

# -- restaurant (tenant) roles ----------------------------------------------
ROLES = ("owner", "manager", "kitchen")

PERMISSIONS: dict[str, set[str]] = {
    # Owners can do everything in their restaurant, including staff and POS.
    "owner": {"reports.view", "orders.view", "calls.view", "customers.view",
              "settings.edit", "pos.edit", "support.use", "team.manage",
              "approvals.decide"},
    # Managers run the restaurant day to day but can't change POS credentials or staff.
    "manager": {"reports.view", "orders.view", "calls.view", "customers.view",
                "settings.edit", "support.use", "approvals.decide"},
    # Kitchen staff see orders and (from PR 3) decide kitchen approvals.
    "kitchen": {"orders.view", "approvals.decide"},
}


def can(role: str | None, permission: str) -> bool:
    return permission in PERMISSIONS.get(role or "", set())


def home_page(role: str | None) -> str:
    """Where a user lands after login: the first page their role can open."""
    return "/portal/" if can(role, "reports.view") else "/portal/orders"


# -- platform roles ------------------------------------------------------------
PLATFORM_ROLES = ("super_admin", "support")

PLATFORM_PERMISSIONS: dict[str, set[str]] = {
    "super_admin": {"tenants.view", "tenants.edit", "flags.edit", "audit.view", "diagnostics.view"},
    # Support can look but not change settings or see POS credentials.
    "support": {"tenants.view", "audit.view", "diagnostics.view"},
}


def platform_can(role: str | None, permission: str) -> bool:
    return permission in PLATFORM_PERMISSIONS.get(role or "", set())


# -- feature flags -----------------------------------------------------------
# Every new capability ships behind one of these, default OFF per tenant.
KNOWN_FLAGS = ("voice_schedule_enabled", "hitl_enabled", "multilingual_enabled")


def env_killed(flag: str) -> bool:
    """Emergency kill switch that needs no database: DISABLED_FEATURES=a,b."""
    killed = {f.strip() for f in os.environ.get("DISABLED_FEATURES", "").split(",") if f.strip()}
    return flag in killed or "all" in killed
