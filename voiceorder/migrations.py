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
    # Existing portal users keep full rights: they become admins.
    if "role" not in db.columns("tenant_users"):
        db.execute("ALTER TABLE tenant_users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")


def _m002_platform_users(db: Database) -> None:
    # Platform staff (super admins) are a separate identity from
    # restaurant logins: a tenant owner can never gain platform rights.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS platform_users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'super_admin',
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


def _m006_voice_routing(db: Database) -> None:
    # Per-restaurant AI answering policy. tenant_id '*' holds the platform
    # emergency stop. Windows are local wall-clock minutes (0-1439) per weekday
    # (0 = Monday); end <= start means the window runs past midnight.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS voice_routing (
          tenant_id TEXT PRIMARY KEY,
          mode TEXT NOT NULL DEFAULT 'always_on',
          off_action TEXT NOT NULL DEFAULT 'transfer',
          no_answer_action TEXT NOT NULL DEFAULT 'voicemail',
          closed_message TEXT NOT NULL DEFAULT '',
          emergency_off INTEGER NOT NULL DEFAULT 0,
          version INTEGER NOT NULL DEFAULT 1,
          updated_at REAL NOT NULL,
          updated_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS voice_routing_windows (
          tenant_id TEXT NOT NULL,
          day INTEGER NOT NULL,
          start_min INTEGER NOT NULL,
          end_min INTEGER NOT NULL,
          PRIMARY KEY (tenant_id, day, start_min)
        );
        CREATE TABLE IF NOT EXISTS voice_routing_overrides (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          starts_at REAL NOT NULL,
          ends_at REAL,
          ai_on INTEGER NOT NULL DEFAULT 0,
          kind TEXT NOT NULL DEFAULT 'pause',
          reason TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL DEFAULT '',
          created_at REAL NOT NULL,
          cancelled_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_routing_overrides_tenant
          ON voice_routing_overrides (tenant_id, starts_at);
    """)


def _m007_call_meta(db: Database) -> None:
    # How each call was routed (AI / forwarded / voicemail / closed) and any
    # voicemail recording link, as JSON.
    if "meta" not in db.columns("call_transcripts"):
        db.execute("ALTER TABLE call_transcripts ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")


MIGRATIONS: list[tuple[str, Callable[[Database], None]]] = [
    ("001_user_roles", _m001_user_roles),
    ("002_platform_users", _m002_platform_users),
    ("003_feature_flags", _m003_feature_flags),
    ("004_audit_logs", _m004_audit_logs),
    ("005_job_runs", _m005_job_runs),
    ("006_voice_routing", _m006_voice_routing),
    ("007_call_meta", _m007_call_meta),
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
