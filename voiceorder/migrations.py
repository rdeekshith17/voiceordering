"""Versioned schema changes, applied once per database at startup.

The original tables are still created with CREATE TABLE IF NOT EXISTS in the
stores; everything added after that goes here as a numbered step. Each
applied version is recorded in `schema_migrations`, so a step runs exactly
once per database. Steps must be additive and safe to re-run partially
(guard ALTERs with `db.columns()`), because Postgres runs in autocommit mode
and a crash mid-step leaves earlier statements applied. Never edit or
reorder a released step; add a new one.

On Postgres an advisory lock serialises concurrent app instances.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from .db import Database

log = logging.getLogger("voiceorder.migrations")

# Arbitrary constant identifying this app's migration lock in pg_advisory_lock.
_PG_LOCK_ID = 74_210_311


def _m001_user_roles(db: Database) -> None:
    # Existing portal users keep full rights: they become owners.
    if "role" not in db.columns("tenant_users"):
        db.execute("ALTER TABLE tenant_users ADD COLUMN role TEXT NOT NULL DEFAULT 'owner'")


def _m002_platform_users(db: Database) -> None:
    # Platform staff (super admin / support) are a separate identity from
    # restaurant logins: a tenant owner can never gain platform rights.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS platform_users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'support',
          active INTEGER NOT NULL DEFAULT 1,
          created_at REAL NOT NULL
        );
    """)


def _m003_feature_flags(db: Database) -> None:
    # tenant_id '*' holds platform-wide switches (a disabled '*' row is a kill switch).
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tenant_feature_flags (
          tenant_id TEXT NOT NULL,
          flag TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0,
          updated_at REAL NOT NULL,
          updated_by TEXT NOT NULL DEFAULT '',
          PRIMARY KEY (tenant_id, flag)
        );
    """)


def _m004_audit_logs(db: Database) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS audit_logs (
          id TEXT PRIMARY KEY,
          at REAL NOT NULL,
          actor_type TEXT NOT NULL,
          actor_id TEXT NOT NULL DEFAULT '',
          tenant_id TEXT,
          action TEXT NOT NULL,
          resource TEXT NOT NULL DEFAULT '',
          before_json TEXT NOT NULL DEFAULT '{}',
          after_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_logs (tenant_id, at DESC);
    """)


def _m005_job_runs(db: Database) -> None:
    # One row per background job: a lease so only one instance runs it at a time.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS job_runs (
          job TEXT PRIMARY KEY,
          lease_until REAL NOT NULL DEFAULT 0,
          last_started_at REAL NOT NULL DEFAULT 0,
          last_finished_at REAL NOT NULL DEFAULT 0,
          last_status TEXT NOT NULL DEFAULT '',
          last_detail TEXT NOT NULL DEFAULT ''
        );
    """)


MIGRATIONS: list[tuple[str, Callable[[Database], None]]] = [
    ("001_user_roles", _m001_user_roles),
    ("002_platform_users", _m002_platform_users),
    ("003_feature_flags", _m003_feature_flags),
    ("004_audit_logs", _m004_audit_logs),
    ("005_job_runs", _m005_job_runs),
]


def applied_versions(db: Database) -> set[str]:
    return {r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}


def run_migrations(db: Database) -> list[str]:
    """Apply pending steps in order. Returns the versions applied now."""
    db.executescript(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at REAL NOT NULL);"
    )
    if db.pg:
        db.execute("SELECT pg_advisory_lock(?)", (_PG_LOCK_ID,))
    try:
        done = applied_versions(db)
        applied = []
        for version, step in MIGRATIONS:
            if version in done:
                continue
            step(db)
            db.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                       (version, time.time()))
            db.commit()
            applied.append(version)
            log.info("migration applied: %s", version)
        return applied
    finally:
        if db.pg:
            db.execute("SELECT pg_advisory_unlock(?)", (_PG_LOCK_ID,))
