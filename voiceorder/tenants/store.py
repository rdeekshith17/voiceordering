"""Multi-tenant storage: tenants, per-tenant settings + encrypted POS
secrets, portal users, and per-call transcripts. SQLite or Postgres (see
voiceorder.db), same patterns as voiceorder.api.storage."""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .. import db as dbmod
from .. import routing
from ..migrations import run_migrations
from . import crypto, rbac


# The code's built-in transfer default: a 555 placeholder, never a real line.
PLACEHOLDER_TRANSFER = "+15550134200"


def real_transfer_number(number: str) -> str:
    """The number to forward calls to, or "" when it's missing or the placeholder."""
    digits = normalize_number(number)
    return number if len(digits) >= 10 and digits != normalize_number(PLACEHOLDER_TRANSFER) else ""


def normalize_number(number: str) -> str:
    """Digits only; US numbers reduced to the last 10 digits for matching."""
    digits = "".join(c for c in (number or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


@dataclass
class Tenant:
    id: str
    name: str
    slug: str
    phone_number: str
    status: str
    settings: dict = field(default_factory=dict)

    def setting(self, key: str, default: str = "") -> str:
        return str(self.settings.get(key, default))


@dataclass
class PortalUser:
    id: str
    tenant_id: str
    email: str
    role: str = "admin"


@dataclass
class PlatformUser:
    id: str
    email: str
    role: str


# Keys whose values never go into the audit log.
_SECRET_HINTS = ("token", "secret", "password", "key", "blob")


def redact(values: dict | None) -> dict:
    return {k: ("[redacted]" if any(h in k.lower() for h in _SECRET_HINTS) else v)
            for k, v in (values or {}).items()}


class TenantStore:
    def __init__(self, db_path: str | Path) -> None:
        """db_path: a SQLite file path or a postgres:// URL."""
        self._lock = threading.Lock()
        self._conn = self._connect(db_path)

    # -- schema ------------------------------------------------------------
    def _connect(self, target: str | Path) -> dbmod.Database:
        conn = dbmod.Database(target)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tenants (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              slug TEXT NOT NULL UNIQUE,
              phone_number TEXT UNIQUE,
              status TEXT NOT NULL DEFAULT 'active',
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tenant_settings (
              tenant_id TEXT NOT NULL,
              key TEXT NOT NULL,
              value TEXT NOT NULL DEFAULT '',
              PRIMARY KEY (tenant_id, key)
            );
            CREATE TABLE IF NOT EXISTS tenant_secrets (
              tenant_id TEXT NOT NULL,
              provider TEXT NOT NULL,
              blob TEXT NOT NULL,
              updated_at REAL NOT NULL,
              PRIMARY KEY (tenant_id, provider)
            );
            CREATE TABLE IF NOT EXISTS tenant_users (
              id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              email TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS call_transcripts (
              call_sid TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              from_number TEXT NOT NULL DEFAULT '',
              to_number TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'live',
              turns TEXT NOT NULL DEFAULT '[]',
              started_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_transcripts_tenant
              ON call_transcripts (tenant_id, updated_at DESC);
            -- background agents: support tickets, marketing drafts, usage metering
            CREATE TABLE IF NOT EXISTS tickets (
              id TEXT PRIMARY KEY,
              tenant_id TEXT,
              kind TEXT NOT NULL,
              severity TEXT NOT NULL DEFAULT 'warning',
              title TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'open',
              created_at REAL NOT NULL,
              resolved_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_tickets_open
              ON tickets (status, kind, tenant_id);
            CREATE TABLE IF NOT EXISTS marketing_drafts (
              id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              title TEXT NOT NULL,
              body TEXT NOT NULL,
              channel TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'draft',
              created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_drafts_tenant
              ON marketing_drafts (tenant_id, created_at DESC);
            -- callers remembered per restaurant, keyed by normalized phone
            CREATE TABLE IF NOT EXISTS customers (
              tenant_id TEXT NOT NULL,
              phone TEXT NOT NULL,
              name TEXT NOT NULL DEFAULT '',
              order_count INTEGER NOT NULL DEFAULT 0,
              last_order TEXT NOT NULL DEFAULT '',
              first_seen REAL NOT NULL,
              last_seen REAL NOT NULL,
              PRIMARY KEY (tenant_id, phone)
            );
            CREATE TABLE IF NOT EXISTS usage_daily (
              tenant_id TEXT NOT NULL,
              date TEXT NOT NULL,
              calls INTEGER NOT NULL DEFAULT 0,
              talk_minutes REAL NOT NULL DEFAULT 0,
              tts_chars INTEGER NOT NULL DEFAULT 0,
              sms_sent INTEGER NOT NULL DEFAULT 0,
              computed_at REAL NOT NULL,
              PRIMARY KEY (tenant_id, date)
            );
            """
        )
        # orders.tenant_id migration (orders table lives in the same DB file
        # when the app uses the default path; harmless if the table is absent)
        try:
            cols = conn.columns("orders")
            if cols and "tenant_id" not in cols:
                conn.execute("ALTER TABLE orders ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_orders_tenant "
                    "ON orders (tenant_id, saved_at DESC)"
                )
        except dbmod.Error:
            pass
        conn.commit()
        run_migrations(conn)
        return conn

    # -- tenants -----------------------------------------------------------
    def _number_taken(self, phone: str, by_other_than: str = "") -> bool:
        """True when another tenant already owns this normalized number."""
        row = self._conn.execute(
            "SELECT id FROM tenants WHERE phone_number = ? AND id != ?",
            (phone, by_other_than),
        ).fetchone()
        return row is not None

    def create_tenant(self, name: str, phone_number: str = "") -> Tenant:
        tid = uuid.uuid4().hex[:12]
        slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or tid
        phone = normalize_number(phone_number)
        with self._lock:
            if phone and self._number_taken(phone):
                raise ValueError("that phone number is already registered to another restaurant")
            # unique-ify slug
            base, n = slug, 2
            while self._conn.execute(
                "SELECT 1 FROM tenants WHERE slug = ?", (slug,)
            ).fetchone():
                slug = f"{base}-{n}"
                n += 1
            self._conn.execute(
                "INSERT INTO tenants (id, name, slug, phone_number, status, created_at)"
                " VALUES (?, ?, ?, ?, 'active', ?)",
                (tid, name, slug, phone or None, time.time()),
            )
            self._conn.commit()
        return self.get_tenant(tid)

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, slug, phone_number, status FROM tenants WHERE id = ?",
                (tenant_id,),
            ).fetchone()
        if not row:
            return None
        return Tenant(
            id=row[0], name=row[1], slug=row[2], phone_number=row[3] or "",
            status=row[4], settings=self.get_settings(row[0]),
        )

    def get_tenant_by_number(self, phone_number: str, include_inactive: bool = False) -> Tenant | None:
        phone = normalize_number(phone_number)
        if not phone:
            return None
        q = "SELECT id FROM tenants WHERE phone_number = ?"
        if not include_inactive:
            q += " AND status = 'active'"
        with self._lock:
            row = self._conn.execute(q, (phone,)).fetchone()
        return self.get_tenant(row[0]) if row else None

    def set_phone_number(self, tenant_id: str, phone_number: str) -> None:
        phone = normalize_number(phone_number)
        with self._lock:
            if phone and self._number_taken(phone, by_other_than=tenant_id):
                raise ValueError("that phone number is already registered to another restaurant")
            self._conn.execute(
                "UPDATE tenants SET phone_number = ? WHERE id = ?",
                (normalize_number(phone_number) or None, tenant_id),
            )
            self._conn.commit()

    # -- settings ----------------------------------------------------------
    def get_settings(self, tenant_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM tenant_settings WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        return {k: v for k, v in rows}

    def set_settings(self, tenant_id: str, values: dict) -> None:
        with self._lock:
            for k, v in values.items():
                self._conn.upsert("tenant_settings",
                                  {"tenant_id": tenant_id, "key": k, "value": str(v)},
                                  key=("tenant_id", "key"))
            self._conn.commit()

    # -- secrets (encrypted at rest) ---------------------------------------
    def set_secret(self, tenant_id: str, provider: str, values: dict) -> None:
        blob = crypto.encrypt_secret(json.dumps(values))
        with self._lock:
            self._conn.upsert("tenant_secrets",
                              {"tenant_id": tenant_id, "provider": provider, "blob": blob,
                               "updated_at": time.time()},
                              key=("tenant_id", "provider"))
            self._conn.commit()

    def get_secret(self, tenant_id: str, provider: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT blob FROM tenant_secrets WHERE tenant_id = ? AND provider = ?",
                (tenant_id, provider),
            ).fetchone()
        if not row:
            return {}
        return json.loads(crypto.decrypt_secret(row[0]))

    def has_secret(self, tenant_id: str, provider: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM tenant_secrets WHERE tenant_id = ? AND provider = ?",
                (tenant_id, provider),
            ).fetchone()
        return row is not None

    def clear_secret(self, tenant_id: str, provider: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM tenant_secrets WHERE tenant_id = ? AND provider = ?",
                (tenant_id, provider),
            )
            self._conn.commit()

    # -- portal users ------------------------------------------------------
    def create_user(self, tenant_id: str, email: str, password: str,
                    role: str = "admin") -> PortalUser:
        email = email.strip().lower()
        if not email or "@" not in email or len(password) < 8:
            raise ValueError("need a valid email and a password of 8+ characters")
        if role not in rbac.ROLES:
            raise ValueError(f"unknown role {role!r}")
        uid = uuid.uuid4().hex[:12]
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM tenant_users WHERE email = ?", (email,)
            ).fetchone():
                raise ValueError("that email is already registered")
            self._conn.execute(
                "INSERT INTO tenant_users (id, tenant_id, email, password_hash, created_at, role)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (uid, tenant_id, email, crypto.hash_password(password), time.time(), role),
            )
            self._conn.commit()
        return PortalUser(id=uid, tenant_id=tenant_id, email=email, role=role)

    def verify_user(self, email: str, password: str) -> PortalUser | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, tenant_id, email, password_hash, role FROM tenant_users"
                " WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        if not row or not crypto.verify_password(password, row[3]):
            return None
        return PortalUser(id=row[0], tenant_id=row[1], email=row[2], role=rbac.normalize_role(row[4]))

    def get_user(self, user_id: str) -> PortalUser | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, tenant_id, email, role FROM tenant_users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return PortalUser(id=row[0], tenant_id=row[1], email=row[2], role=rbac.normalize_role(row[3])) if row else None

    def list_users(self, tenant_id: str) -> list[PortalUser]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tenant_id, email, role FROM tenant_users WHERE tenant_id = ?"
                " ORDER BY created_at", (tenant_id,),
            ).fetchall()
        return [PortalUser(id=r[0], tenant_id=r[1], email=r[2], role=rbac.normalize_role(r[3])) for r in rows]

    def set_user_role(self, tenant_id: str, user_id: str, role: str) -> bool:
        """Tenant-scoped role change. Refuses to remove the restaurant's last admin."""
        if role not in rbac.ROLES:
            raise ValueError(f"unknown role {role!r}")
        users = {u.id: u for u in self.list_users(tenant_id)}
        if user_id not in users:
            return False
        admins = [u for u in users.values() if rbac.normalize_role(u.role) == "admin"]
        if role != "admin" and admins == [users[user_id]]:
            raise ValueError("a restaurant needs at least one admin")
        with self._lock:
            self._conn.execute("UPDATE tenant_users SET role = ? WHERE id = ? AND tenant_id = ?",
                               (role, user_id, tenant_id))
            self._conn.commit()
        return True

    def count_users(self, tenant_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tenant_users WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return row[0] if row else 0

    def get_tenant_by_name(self, name: str) -> Tenant | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM tenants WHERE lower(name) = lower(?)",
                (name.strip(),),
            ).fetchone()
        return self.get_tenant(row[0]) if row else None

    # -- call transcripts --------------------------------------------------
    def start_call(self, call_sid: str, tenant_id: str, from_number: str, to_number: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.upsert("call_transcripts",
                              {"call_sid": call_sid, "tenant_id": tenant_id,
                               "from_number": from_number, "to_number": to_number,
                               "status": "live", "turns": "[]",
                               "started_at": now, "updated_at": now},
                              key=("call_sid",))
            self._conn.commit()

    def append_turn(
        self, call_sid: str, heard: str, reply: str, tool_calls: list[dict]
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT turns FROM call_transcripts WHERE call_sid = ?", (call_sid,)
            ).fetchone()
            if not row:
                return
            turns = json.loads(row[0])
            turns.append(
                {
                    "ts": time.time(),
                    "heard": heard,
                    "reply": reply,
                    "tools": [
                        {"name": t.get("name"), "ok": t.get("ok")}
                        for t in (tool_calls or [])
                    ],
                }
            )
            self._conn.execute(
                "UPDATE call_transcripts SET turns = ?, updated_at = ?"
                " WHERE call_sid = ?",
                (json.dumps(turns), time.time(), call_sid),
            )
            self._conn.commit()

    def end_call(self, call_sid: str, status: str = "ended") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE call_transcripts SET status = ?, updated_at = ?"
                " WHERE call_sid = ?",
                (status, time.time(), call_sid),
            )
            self._conn.commit()

    def get_transcript(self, tenant_id: str, call_sid: str) -> dict | None:
        """Tenant-scoped: returns None unless the call belongs to the tenant."""
        with self._lock:
            row = self._conn.execute(
                "SELECT call_sid, tenant_id, from_number, to_number, status, turns,"
                " started_at, updated_at, meta FROM call_transcripts"
                " WHERE call_sid = ? AND tenant_id = ?",
                (call_sid, tenant_id),
            ).fetchone()
        if not row:
            return None
        return {
            "call_sid": row[0], "tenant_id": row[1], "from_number": row[2],
            "to_number": row[3], "status": row[4], "turns": json.loads(row[5]),
            "started_at": row[6], "updated_at": row[7], "meta": json.loads(row[8] or "{}"),
        }

    def list_calls(self, tenant_id: str, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT call_sid, from_number, to_number, status, turns,"
                " started_at, updated_at, meta FROM call_transcripts"
                " WHERE tenant_id = ?"
                " ORDER BY CASE status WHEN 'live' THEN 0 ELSE 1 END, updated_at DESC"
                " LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            turns = json.loads(r[4])
            out.append({
                "call_sid": r[0], "from_number": r[1], "to_number": r[2],
                "status": r[3], "turn_count": len(turns),
                "last_reply": turns[-1]["reply"][:120] if turns else "",
                "started_at": r[5], "updated_at": r[6], "meta": json.loads(r[7] or "{}"),
            })
        return out

    # -- migration helper --------------------------------------------------
    def backfill_orders_tenant(self, tenant_id: str) -> int:
        """Tag pre-tenancy orders (empty tenant_id) to the given tenant."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE orders SET tenant_id = ? WHERE tenant_id = ''", (tenant_id,)
            )
            self._conn.commit()
            return cur.rowcount

    # -- tenant listing ----------------------------------------------------
    def list_tenants(self, active_only: bool = True) -> list[Tenant]:
        """All tenants, for background agents that sweep the platform."""
        with self._lock:
            q = "SELECT id FROM tenants"
            if active_only:
                q += " WHERE status = 'active'"
            q += " ORDER BY created_at"
            rows = self._conn.execute(q).fetchall()
        return [t for t in (self.get_tenant(r[0]) for r in rows) if t]

    def set_tenant_status(self, tenant_id: str, status: str, actor: str) -> bool:
        if status not in ("active", "suspended"):
            raise ValueError("status must be active or suspended")
        before = self.get_tenant(tenant_id)
        if before is None:
            return False
        with self._lock:
            self._conn.execute("UPDATE tenants SET status = ? WHERE id = ?", (status, tenant_id))
            self._conn.commit()
        self.audit("platform", actor, tenant_id, "tenant.status", f"tenant:{tenant_id}",
                   {"status": before.status}, {"status": status})
        return True

    def reset_user_password(self, tenant_id: str, user_id: str, password: str, actor: str) -> bool:
        if len(password) < 8:
            raise ValueError("password must be 8+ characters")
        with self._lock:
            n = self._conn.execute(
                "UPDATE tenant_users SET password_hash = ? WHERE id = ? AND tenant_id = ?",
                (crypto.hash_password(password), user_id, tenant_id)).rowcount
            self._conn.commit()
        if n:
            self.audit("platform", actor, tenant_id, "user.password_reset", f"user:{user_id}")
        return n == 1

    # -- platform-wide reads (Super Admin) ------------------------------------------
    def _count_by_tenant(self, sql: str, params: tuple) -> dict[str, int]:
        try:
            with self._lock:
                return {r[0]: int(r[1]) for r in self._conn.execute(sql, params).fetchall()}
        except dbmod.Error:  # e.g. orders table absent in a bare test DB
            return {}

    def calls_by_tenant(self, since: float) -> dict[str, int]:
        return self._count_by_tenant(
            "SELECT tenant_id, COUNT(*) FROM call_transcripts WHERE started_at >= ?"
            " GROUP BY tenant_id", (since,))

    def orders_by_tenant(self, since: float) -> dict[str, int]:
        return self._count_by_tenant(
            "SELECT tenant_id, COUNT(*) FROM orders WHERE saved_at >= ? GROUP BY tenant_id",
            (since,))

    def open_tickets_by_tenant(self) -> dict[str, int]:
        return self._count_by_tenant(
            "SELECT COALESCE(tenant_id, '*'), COUNT(*) FROM tickets WHERE status = 'open'"
            " GROUP BY COALESCE(tenant_id, '*')", ())

    def platform_calls(self, since: float, limit: int = 200) -> list[dict]:
        """Recent calls across every restaurant (newest first), with routing meta."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT call_sid, tenant_id, from_number, status, started_at, updated_at, meta"
                " FROM call_transcripts WHERE started_at >= ? ORDER BY started_at DESC LIMIT ?",
                (since, limit)).fetchall()
        return [{"call_sid": r[0], "tenant_id": r[1], "from_number": r[2], "status": r[3],
                 "started_at": r[4], "updated_at": r[5], "meta": json.loads(r[6] or "{}")}
                for r in rows]

    def platform_pending_approvals(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._APPROVAL_COLS} FROM approval_requests WHERE status = 'pending'"
                " ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        return [self._approval_row(r) for r in rows]

    def usage_by_tenant(self, month: str) -> dict[str, dict]:
        """Sum of usage_daily rows for a 'YYYY-MM' month, per tenant."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tenant_id, SUM(calls), SUM(talk_minutes), SUM(tts_chars), SUM(sms_sent),"
                " MAX(computed_at) FROM usage_daily WHERE date LIKE ? GROUP BY tenant_id",
                (month + "%",)).fetchall()
        return {r[0]: {"calls": int(r[1] or 0), "talk_minutes": float(r[2] or 0),
                       "tts_chars": int(r[3] or 0), "sms_sent": int(r[4] or 0),
                       "computed_at": r[5]} for r in rows}

    def tenant_created_at(self, tenant_id: str) -> float | None:
        """created_at epoch for a tenant (onboarding agent staleness)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
        return float(row[0]) if row else None

    def order_count(self, tenant_id: str) -> int:
        """Total orders ever for a tenant. 0 when the orders table is absent."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM orders WHERE tenant_id = ?",
                    (tenant_id,),
                ).fetchone()
        except dbmod.Error:
            return 0
        return row[0] if row else 0

    # -- support tickets ---------------------------------------------------
    @staticmethod
    def _tenant_match(tenant_id: str | None) -> tuple[str, tuple]:
        """SQL fragment matching a tenant_id that may be NULL (platform-wide)."""
        if tenant_id is None:
            return "tenant_id IS NULL", ()
        return "tenant_id = ?", (tenant_id,)

    def open_ticket(self, tenant_id: str | None, kind: str, severity: str,
                    title: str, detail: str = "", now: float | None = None) -> dict | None:
        """Open a ticket unless one is already open for tenant_id+kind.

        Returns the new ticket dict, or None when deduped against an
        existing open/acked ticket. tenant_id=None means platform-wide.
        """
        now = now if now is not None else time.time()
        frag, params = self._tenant_match(tenant_id)
        with self._lock:
            dup = self._conn.execute(
                f"SELECT id FROM tickets WHERE kind = ? AND status IN ('open','acked')"
                f" AND {frag}",
                (kind, *params),
            ).fetchone()
            if dup:
                return None
            tid = uuid.uuid4().hex[:12]
            self._conn.execute(
                "INSERT INTO tickets (id, tenant_id, kind, severity, title, detail,"
                " status, created_at, resolved_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'open', ?, NULL)",
                (tid, tenant_id, kind, severity, title, detail, now),
            )
            self._conn.commit()
        return self.get_ticket(tid)

    def get_ticket(self, ticket_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, tenant_id, kind, severity, title, detail, status,"
                " created_at, resolved_at FROM tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "tenant_id": row[1], "kind": row[2], "severity": row[3],
            "title": row[4], "detail": row[5], "status": row[6],
            "created_at": row[7], "resolved_at": row[8],
        }

    def list_tickets(self, tenant_id: str, include_platform: bool = True,
                     include_resolved: bool = True, limit: int = 50) -> list[dict]:
        """Tenant's tickets (plus platform-wide ones), open first."""
        with self._lock:
            q = ("SELECT id, tenant_id, kind, severity, title, detail, status,"
                 " created_at, resolved_at FROM tickets WHERE (tenant_id = ?")
            params: list = [tenant_id]
            if include_platform:
                q += " OR tenant_id IS NULL"
            q += ")"
            if not include_resolved:
                q += " AND status IN ('open','acked')"
            q += (" ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'acked' THEN 1"
                  " ELSE 2 END, created_at DESC LIMIT ?")
            params.append(limit)
            rows = self._conn.execute(q, params).fetchall()
        return [
            {"id": r[0], "tenant_id": r[1], "kind": r[2], "severity": r[3],
             "title": r[4], "detail": r[5], "status": r[6],
             "created_at": r[7], "resolved_at": r[8]}
            for r in rows
        ]

    def ack_ticket(self, tenant_id: str, ticket_id: str) -> bool:
        """Tenant-scoped acknowledge: only the tenant's own open tickets."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tickets SET status = 'acked'"
                " WHERE id = ? AND tenant_id = ? AND status = 'open'",
                (ticket_id, tenant_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def resolve_ticket(self, tenant_id: str | None, kind: str,
                       now: float | None = None) -> bool:
        """Auto-resolve the open/acked ticket for tenant_id+kind on recovery."""
        now = now if now is not None else time.time()
        frag, params = self._tenant_match(tenant_id)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tickets SET status = 'resolved', resolved_at = ?"
                f" WHERE kind = ? AND status IN ('open','acked') AND {frag}",
                (now, kind, *params),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # -- marketing drafts --------------------------------------------------
    def save_draft(self, tenant_id: str, title: str, body: str, channel: str,
                   now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        did = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO marketing_drafts (id, tenant_id, title, body, channel,"
                " status, created_at) VALUES (?, ?, ?, ?, ?, 'draft', ?)",
                (did, tenant_id, title, body, channel, now),
            )
            self._conn.commit()
        return {"id": did, "tenant_id": tenant_id, "title": title, "body": body,
                "channel": channel, "status": "draft", "created_at": now}

    def list_drafts(self, tenant_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tenant_id, title, body, channel, status, created_at"
                " FROM marketing_drafts WHERE tenant_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [
            {"id": r[0], "tenant_id": r[1], "title": r[2], "body": r[3],
             "channel": r[4], "status": r[5], "created_at": r[6]}
            for r in rows
        ]

    def drafts_since(self, tenant_id: str, since_ts: float) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM marketing_drafts"
                " WHERE tenant_id = ? AND created_at >= ?",
                (tenant_id, since_ts),
            ).fetchone()
        return row[0] if row else 0

    # -- platform users (super admins) --------------------------------------
    def create_platform_user(self, email: str, password: str,
                             role: str = "super_admin") -> PlatformUser:
        email = email.strip().lower()
        if not email or "@" not in email or len(password) < 12:
            raise ValueError("need a valid email and a password of 12+ characters")
        if role not in rbac.PLATFORM_ROLES:
            raise ValueError(f"unknown platform role {role!r}")
        uid = uuid.uuid4().hex[:12]
        with self._lock:
            if self._conn.execute("SELECT 1 FROM platform_users WHERE email = ?",
                                  (email,)).fetchone():
                raise ValueError("that email is already a platform user")
            self._conn.execute(
                "INSERT INTO platform_users (id, email, password_hash, role, active, created_at)"
                " VALUES (?, ?, ?, ?, 1, ?)",
                (uid, email, crypto.hash_password(password), role, time.time()),
            )
            self._conn.commit()
        return PlatformUser(id=uid, email=email, role=role)

    def verify_platform_user(self, email: str, password: str) -> PlatformUser | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, email, password_hash, role FROM platform_users"
                " WHERE email = ? AND active = 1", (email.strip().lower(),),
            ).fetchone()
        if not row or not crypto.verify_password(password, row[2]):
            return None
        return PlatformUser(id=row[0], email=row[1], role=row[3])

    def get_platform_user(self, user_id: str) -> PlatformUser | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, email, role FROM platform_users WHERE id = ? AND active = 1",
                (user_id,),
            ).fetchone()
        return PlatformUser(id=row[0], email=row[1], role=row[2]) if row else None

    # -- feature flags (default OFF) ----------------------------------------
    def flag_enabled(self, tenant_id: str, flag: str) -> bool:
        """On only if switched on for the tenant and not killed platform-wide
        (env DISABLED_FEATURES, or a disabled '*' row)."""
        if flag not in rbac.KNOWN_FLAGS or rbac.env_killed(flag):
            return False
        with self._lock:
            rows = dict(self._conn.execute(
                "SELECT tenant_id, enabled FROM tenant_feature_flags"
                " WHERE flag = ? AND tenant_id IN (?, '*')", (flag, tenant_id),
            ).fetchall())
        if "*" in rows and not rows["*"]:
            return False
        return bool(rows.get(tenant_id, 0))

    def set_flag(self, tenant_id: str, flag: str, enabled: bool, actor: str = "") -> None:
        """tenant_id '*' sets the platform-wide switch for the flag."""
        if flag not in rbac.KNOWN_FLAGS:
            raise ValueError(f"unknown feature flag {flag!r}")
        before = self.list_flags(tenant_id).get(flag, False)
        with self._lock:
            self._conn.upsert("tenant_feature_flags",
                              {"tenant_id": tenant_id, "flag": flag, "enabled": 1 if enabled else 0,
                               "updated_at": time.time(), "updated_by": actor},
                              key=("tenant_id", "flag"))
            self._conn.commit()
        self.audit("user", actor, None if tenant_id == "*" else tenant_id, "flag.set",
                   f"flag:{flag}", {"enabled": before}, {"enabled": bool(enabled)})

    def list_flags(self, tenant_id: str) -> dict[str, bool]:
        saved = self.saved_flags(tenant_id)
        return {f: saved.get(f, False) for f in rbac.KNOWN_FLAGS}

    def saved_flags(self, tenant_id: str) -> dict[str, bool]:
        """Only the flags explicitly set for this tenant ('*' = platform switches)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT flag, enabled FROM tenant_feature_flags WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        return {r[0]: bool(r[1]) for r in rows}

    # -- audit log (append-only, secrets redacted) --------------------------
    def audit(self, actor_type: str, actor_id: str, tenant_id: str | None, action: str,
              resource: str = "", before: dict | None = None, after: dict | None = None,
              now: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_logs (id, at, actor_type, actor_id, tenant_id, action,"
                " resource, before_json, after_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, now if now is not None else time.time(), actor_type,
                 actor_id or "", tenant_id, action, resource,
                 json.dumps(redact(before)), json.dumps(redact(after))),
            )
            self._conn.commit()

    def list_audit(self, tenant_id: str | None = None, limit: int = 100,
                   action_prefix: str = "") -> list[dict]:
        """A tenant's events, or every event when tenant_id is None (platform view)."""
        q = ("SELECT at, actor_type, actor_id, tenant_id, action, resource, before_json,"
             " after_json FROM audit_logs WHERE 1 = 1")
        params: list = []
        if tenant_id is not None:
            q += " AND tenant_id = ?"
            params.append(tenant_id)
        if action_prefix:
            q += " AND action LIKE ?"
            params.append(action_prefix.replace("%", "") + "%")
        q += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [{"at": r[0], "actor_type": r[1], "actor_id": r[2], "tenant_id": r[3],
                 "action": r[4], "resource": r[5], "before": json.loads(r[6]),
                 "after": json.loads(r[7])} for r in rows]

    # -- background job leases ------------------------------------------------
    def try_start_job(self, job: str, lease_seconds: float, min_interval: float,
                      now: float | None = None) -> bool:
        """Claim a job if it is due and nobody holds its lease. Atomic, so two
        app instances never run the same job at once."""
        now = now if now is not None else time.time()
        with self._lock:
            try:  # first sight of this job; another instance may race us to it
                self._conn.execute(
                    "INSERT INTO job_runs (job) SELECT ? WHERE NOT EXISTS"
                    " (SELECT 1 FROM job_runs WHERE job = ?)", (job, job))
            except dbmod.Error:
                pass
            cur = self._conn.execute(
                "UPDATE job_runs SET lease_until = ?, last_started_at = ?"
                " WHERE job = ? AND lease_until < ?"
                " AND (last_finished_at = 0 OR last_finished_at <= ?)",
                (now + lease_seconds, now, job, now, now - min_interval),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def finish_job(self, job: str, status: str, detail: str = "",
                   now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE job_runs SET lease_until = 0, last_finished_at = ?, last_status = ?,"
                " last_detail = ? WHERE job = ?", (now, status, detail[:500], job))
            self._conn.commit()

    def job_status(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT job, last_started_at, last_finished_at, last_status, last_detail"
                " FROM job_runs ORDER BY job").fetchall()
        return [{"job": r[0], "last_started_at": r[1], "last_finished_at": r[2],
                 "last_status": r[3], "last_detail": r[4]} for r in rows]

    # -- AI answering policy (on/off, schedule, pauses) -------------------------
    def routing_config(self, tenant_id: str, now: float | None = None) -> tuple[routing.RoutingConfig, int]:
        """The restaurant's policy and its version (0 = never saved, defaults).
        Overrides that ended more than a day ago are left out."""
        now = now if now is not None else time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT mode, off_action, no_answer_action, closed_message, emergency_off, version"
                " FROM voice_routing WHERE tenant_id = ?", (tenant_id,)).fetchone()
            wins = self._conn.execute(
                "SELECT day, start_min, end_min FROM voice_routing_windows WHERE tenant_id = ?"
                " ORDER BY day, start_min", (tenant_id,)).fetchall()
            ovs = self._conn.execute(
                "SELECT id, starts_at, ends_at, ai_on, kind, reason, created_at"
                " FROM voice_routing_overrides WHERE tenant_id = ? AND cancelled_at IS NULL"
                " AND (ends_at IS NULL OR ends_at > ?) ORDER BY starts_at",
                (tenant_id, now - 86400)).fetchall()
        cfg = routing.RoutingConfig(
            windows=[routing.Window(int(d), int(a), int(b)) for d, a, b in wins],
            overrides=[routing.Override(o[0], o[1], o[2], bool(o[3]), o[4], o[5], o[6]) for o in ovs])
        if not row:
            return cfg, 0
        cfg.mode, cfg.off_action, cfg.no_answer_action, cfg.closed_message = row[0], row[1], row[2], row[3]
        cfg.emergency_off = bool(row[4])
        return cfg, int(row[5])

    def save_routing(self, tenant_id: str, cfg: routing.RoutingConfig, actor: str,
                     expected_version: int | None = None) -> int:
        """Replace mode, actions, message and weekly windows. Returns the new
        version; raises ValueError if someone saved in between (stale page)."""
        if cfg.mode not in routing.MODES or cfg.off_action not in routing.OFF_ACTIONS \
                or cfg.no_answer_action not in routing.NO_ANSWER_ACTIONS:
            raise ValueError("unknown mode or action")
        for w in cfg.windows:
            if not (0 <= w.day <= 6 and 0 <= w.start_min < 1440 and 0 <= w.end_min < 1440):
                raise ValueError("schedule times must be within the day")
        before, version = self.routing_config(tenant_id)
        if expected_version is not None and expected_version != version:
            raise ValueError("these settings were changed by someone else; reload and try again")
        with self._lock, self._conn.transaction():  # callers never see a half-saved schedule
            self._conn.upsert("voice_routing", {
                "tenant_id": tenant_id, "mode": cfg.mode, "off_action": cfg.off_action,
                "no_answer_action": cfg.no_answer_action,
                "closed_message": cfg.closed_message.strip()[:500],
                "emergency_off": 1 if before.emergency_off else 0, "version": version + 1,
                "updated_at": time.time(), "updated_by": actor}, key=("tenant_id",))
            self._conn.execute("DELETE FROM voice_routing_windows WHERE tenant_id = ?", (tenant_id,))
            for w in sorted(set(cfg.windows), key=lambda w: (w.day, w.start_min)):
                self._conn.execute(
                    "INSERT INTO voice_routing_windows (tenant_id, day, start_min, end_min)"
                    " VALUES (?, ?, ?, ?)", (tenant_id, w.day, w.start_min, w.end_min))

        def summary(c: routing.RoutingConfig) -> dict:
            return {"mode": c.mode, "off_action": c.off_action,
                    "no_answer_action": c.no_answer_action, "closed_message": c.closed_message,
                    "windows": [[w.day, w.start_min, w.end_min] for w in c.windows]}
        self.audit("user", actor, tenant_id, "voice_routing.update", "voice_routing",
                   summary(before), summary(cfg))
        return version + 1

    def set_routing_emergency(self, tenant_id: str, off: bool, actor: str) -> None:
        """Turn the AI off now (until resumed). tenant_id '*' = platform-wide stop."""
        cfg, version = self.routing_config(tenant_id)
        with self._lock:
            self._conn.upsert("voice_routing", {
                "tenant_id": tenant_id, "mode": cfg.mode, "off_action": cfg.off_action,
                "no_answer_action": cfg.no_answer_action, "closed_message": cfg.closed_message,
                "emergency_off": 1 if off else 0, "version": version + 1,
                "updated_at": time.time(), "updated_by": actor}, key=("tenant_id",))
            self._conn.commit()
        self.audit("user", actor, None if tenant_id == "*" else tenant_id,
                   "voice_routing.emergency_off" if off else "voice_routing.emergency_cleared",
                   "voice_routing", {"emergency_off": cfg.emergency_off}, {"emergency_off": off})

    def platform_emergency(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT emergency_off FROM voice_routing WHERE tenant_id = '*'").fetchone()
        return bool(row and row[0])

    def add_routing_override(self, tenant_id: str, starts_at: float, ends_at: float | None,
                             ai_on: bool, kind: str, reason: str, actor: str) -> str:
        if kind not in ("pause", "exception"):
            raise ValueError("unknown override kind")
        if ends_at is not None and ends_at <= starts_at:
            raise ValueError("the end must be after the start")
        oid = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO voice_routing_overrides (id, tenant_id, starts_at, ends_at, ai_on,"
                " kind, reason, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, tenant_id, starts_at, ends_at, 1 if ai_on else 0, kind,
                 reason.strip()[:120], actor, time.time()))
            self._conn.commit()
        self.audit("user", actor, tenant_id, f"voice_routing.{kind}_added", f"override:{oid}", None,
                   {"starts_at": starts_at, "ends_at": ends_at, "ai_on": ai_on, "reason": reason})
        return oid

    def cancel_routing_overrides(self, tenant_id: str, actor: str, override_id: str | None = None,
                                 kind: str | None = None, now: float | None = None) -> int:
        """Cancel one override (by id) or all current ones of a kind. Tenant-scoped."""
        now = now if now is not None else time.time()
        q = ("UPDATE voice_routing_overrides SET cancelled_at = ? WHERE tenant_id = ?"
             " AND cancelled_at IS NULL")
        params: list = [now, tenant_id]
        if override_id:
            q += " AND id = ?"
            params.append(override_id)
        if kind:
            q += " AND kind = ? AND starts_at <= ? AND (ends_at IS NULL OR ends_at > ?)"
            params += [kind, now, now]
        with self._lock:
            n = self._conn.execute(q, params).rowcount
            self._conn.commit()
        if n:
            self.audit("user", actor, tenant_id, "voice_routing.override_cancelled",
                       f"override:{override_id or kind}", None, {"cancelled": n})
        return n

    # -- kitchen approvals (human in the loop) ------------------------------------
    _APPROVAL_COLS = ("id, tenant_id, call_sid, cart_id, line_id, item_name, category,"
                      " request_text, status, decision_note, decided_by, created_at,"
                      " deadline_at, decided_at, relayed_at, version")

    @staticmethod
    def _approval_row(r) -> dict:
        keys = ("id", "tenant_id", "call_sid", "cart_id", "line_id", "item_name", "category",
                "request_text", "status", "decision_note", "decided_by", "created_at",
                "deadline_at", "decided_at", "relayed_at", "version")
        return dict(zip(keys, r))

    def _approval_event(self, approval_id: str, tenant_id: str, actor_type: str, actor_id: str,
                        event: str, detail: dict | None = None, now: float | None = None) -> None:
        self._conn.execute(
            "INSERT INTO approval_events (id, approval_id, tenant_id, at, actor_type, actor_id,"
            " event, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, approval_id, tenant_id, now if now is not None else time.time(),
             actor_type, actor_id or "", event, json.dumps(redact(detail))))

    def create_approval(self, tenant_id: str, call_sid: str, category: str, request_text: str,
                        hold_seconds: float, cart_id: str = "", line_id: str = "",
                        item_name: str = "", now: float | None = None) -> dict:
        """Open a request for the kitchen. A repeat of the same pending request on
        the same call (e.g. a retried webhook) returns the existing one."""
        now = now if now is not None else time.time()
        text = " ".join(str(request_text).split())[:300]
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._APPROVAL_COLS} FROM approval_requests WHERE call_sid = ?"
                " AND tenant_id = ? AND status = 'pending' AND request_text = ?",
                (call_sid, tenant_id, text)).fetchone()
            if row:
                return self._approval_row(row)
            aid = uuid.uuid4().hex[:12]
            with self._conn.transaction():
                self._conn.execute(
                    "INSERT INTO approval_requests (id, tenant_id, call_sid, cart_id, line_id,"
                    " item_name, category, request_text, status, created_at, deadline_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (aid, tenant_id, call_sid, cart_id, line_id, item_name[:80], category, text,
                     now, now + hold_seconds))
                self._approval_event(aid, tenant_id, "agent", call_sid, "requested",
                                     {"category": category, "request": text}, now)
        return self.get_approval(tenant_id, aid)

    def get_approval(self, tenant_id: str, approval_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._APPROVAL_COLS} FROM approval_requests WHERE id = ? AND tenant_id = ?",
                (approval_id, tenant_id)).fetchone()
        return self._approval_row(row) if row else None

    def list_approvals(self, tenant_id: str, statuses: tuple[str, ...] | None = None,
                       limit: int = 50) -> list[dict]:
        q = f"SELECT {self._APPROVAL_COLS} FROM approval_requests WHERE tenant_id = ?"
        params: list = [tenant_id]
        if statuses:
            q += f" AND status IN ({', '.join('?' for _ in statuses)})"
            params += list(statuses)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(q, params).fetchall()
        return [self._approval_row(r) for r in rows]

    def approvals_for_call(self, call_sid: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._APPROVAL_COLS} FROM approval_requests WHERE call_sid = ?"
                " ORDER BY created_at", (call_sid,)).fetchall()
        return [self._approval_row(r) for r in rows]

    def decide_approval(self, tenant_id: str, approval_id: str, decision: str, note: str,
                        actor_id: str, expected_version: int | None = None,
                        now: float | None = None) -> dict | None:
        """Staff decision. Atomic: only a still-pending request (at the version the
        staff member saw, if given) and before its deadline can be decided, so two
        people tapping at once produce one decision. Returns the request, or None
        when it was already decided / expired / changed."""
        from ..approvals import DECISIONS
        if decision not in DECISIONS:
            raise ValueError("decision must be approved, rejected or needs_info")
        now = now if now is not None else time.time()
        q = ("UPDATE approval_requests SET status = ?, decision_note = ?, decided_by = ?,"
             " decided_at = ?, version = version + 1"
             " WHERE id = ? AND tenant_id = ? AND status = 'pending' AND deadline_at > ?")
        params: list = [decision, " ".join(str(note or "").split())[:300], actor_id, now,
                        approval_id, tenant_id, now]
        if expected_version is not None:
            q += " AND version = ?"
            params.append(int(expected_version))
        with self._lock, self._conn.transaction():
            if self._conn.execute(q, params).rowcount != 1:
                return None
            self._approval_event(approval_id, tenant_id, "user", actor_id, decision,
                                 {"note": note}, now)
        return self.get_approval(tenant_id, approval_id)

    def expire_approval(self, tenant_id: str, approval_id: str, now: float | None = None) -> bool:
        """Mark a pending request past its deadline as timed out (never approved)."""
        now = now if now is not None else time.time()
        with self._lock, self._conn.transaction():
            n = self._conn.execute(
                "UPDATE approval_requests SET status = 'timed_out', decided_at = ?,"
                " version = version + 1 WHERE id = ? AND tenant_id = ? AND status = 'pending'"
                " AND deadline_at <= ?", (now, approval_id, tenant_id, now)).rowcount
            if n:
                self._approval_event(approval_id, tenant_id, "system", "", "timed_out", None, now)
        return n == 1

    def cancel_approvals(self, call_sid: str, reason: str, line_id: str | None = None,
                         now: float | None = None) -> int:
        """Cancel a call's pending requests (caller hung up, or the item was removed)."""
        now = now if now is not None else time.time()
        pending = [a for a in self.approvals_for_call(call_sid) if a["status"] == "pending"
                   and (line_id is None or a["line_id"] == line_id)]
        n = 0
        with self._lock, self._conn.transaction():
            for a in pending:
                if self._conn.execute(
                        "UPDATE approval_requests SET status = 'cancelled', decided_at = ?,"
                        " version = version + 1 WHERE id = ? AND status = 'pending'",
                        (now, a["id"])).rowcount:
                    self._approval_event(a["id"], a["tenant_id"], "system", "", "cancelled",
                                         {"reason": reason}, now)
                    n += 1
        return n

    def mark_approval_relayed(self, tenant_id: str, approval_id: str,
                              now: float | None = None) -> bool:
        """Record that the caller has been told the outcome; True only the first time."""
        now = now if now is not None else time.time()
        with self._lock:
            n = self._conn.execute(
                "UPDATE approval_requests SET relayed_at = ? WHERE id = ? AND tenant_id = ?"
                " AND relayed_at IS NULL", (now, approval_id, tenant_id)).rowcount
            self._conn.commit()
        return n == 1

    def approval_events(self, tenant_id: str, approval_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT at, actor_type, actor_id, event, detail_json FROM approval_events"
                " WHERE approval_id = ? AND tenant_id = ? ORDER BY at", (approval_id, tenant_id)
            ).fetchall()
        return [{"at": r[0], "actor_type": r[1], "actor_id": r[2], "event": r[3],
                 "detail": json.loads(r[4])} for r in rows]

    # -- live call sessions (survive restarts) ------------------------------------
    def save_call_session(self, call_sid: str, tenant_id: str, cart_id: str,
                          caller: dict | None, history: list, state: dict | None = None) -> None:
        with self._lock:
            self._conn.upsert("call_sessions", {
                "call_sid": call_sid, "tenant_id": tenant_id, "cart_id": cart_id,
                "caller_json": json.dumps(caller), "history_json": json.dumps(history, default=str),
                "state_json": json.dumps(state or {}), "updated_at": time.time()},
                key=("call_sid",))
            self._conn.commit()

    def load_call_session(self, call_sid: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT tenant_id, cart_id, caller_json, history_json, state_json, updated_at"
                " FROM call_sessions WHERE call_sid = ?", (call_sid,)).fetchone()
        if not row:
            return None
        return {"tenant_id": row[0], "cart_id": row[1], "caller": json.loads(row[2]),
                "history": json.loads(row[3]), "state": json.loads(row[4]), "updated_at": row[5]}

    def delete_call_session(self, call_sid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM call_sessions WHERE call_sid = ?", (call_sid,))
            self._conn.commit()

    # -- per-call routing record -------------------------------------------------
    def set_call_meta(self, call_sid: str, **values) -> None:
        """Merge values into the call's meta JSON (routing outcome, voicemail link)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT meta FROM call_transcripts WHERE call_sid = ?", (call_sid,)).fetchone()
            if not row:
                return
            meta = json.loads(row[0] or "{}")
            meta.update(values)
            self._conn.execute("UPDATE call_transcripts SET meta = ?, updated_at = ?"
                               " WHERE call_sid = ?", (json.dumps(meta), time.time(), call_sid))
            self._conn.commit()

    # -- customers (returning-caller memory) -------------------------------
    def get_customer(self, tenant_id: str, phone: str) -> dict | None:
        """A remembered caller of this restaurant, by any format of their number."""
        number = normalize_number(phone)
        if not number:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT phone, name, order_count, last_order, first_seen, last_seen"
                " FROM customers WHERE tenant_id = ? AND phone = ?",
                (tenant_id, number),
            ).fetchone()
        if not row:
            return None
        return {"phone": row[0], "name": row[1], "order_count": row[2],
                "last_order": row[3], "first_seen": row[4], "last_seen": row[5]}

    def record_customer(self, tenant_id: str, phone: str, name: str = "",
                        last_order: str = "", ordered: bool = False,
                        now: float | None = None) -> dict | None:
        """Create or update a caller. Blank name/last_order keep what was saved."""
        number = normalize_number(phone)
        if not number:
            return None
        now = now if now is not None else time.time()
        existing = self.get_customer(tenant_id, number) or {}
        row = {
            "tenant_id": tenant_id, "phone": number,
            "name": (name or "").strip()[:80] or existing.get("name", ""),
            "order_count": int(existing.get("order_count", 0)) + (1 if ordered else 0),
            "last_order": (last_order or "").strip()[:300] or existing.get("last_order", ""),
            "first_seen": existing.get("first_seen", now), "last_seen": now,
        }
        with self._lock:
            self._conn.upsert("customers", row, key=("tenant_id", "phone"))
            self._conn.commit()
        return self.get_customer(tenant_id, number)

    def list_customers(self, tenant_id: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT phone, name, order_count, last_order, first_seen, last_seen"
                " FROM customers WHERE tenant_id = ? ORDER BY last_seen DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [{"phone": r[0], "name": r[1], "order_count": r[2], "last_order": r[3],
                 "first_seen": r[4], "last_seen": r[5]} for r in rows]

    # -- usage metering ----------------------------------------------------
    def upsert_usage(self, tenant_id: str, date: str, calls: int,
                     talk_minutes: float, tts_chars: int, sms_sent: int,
                     now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            self._conn.upsert("usage_daily",
                              {"tenant_id": tenant_id, "date": date, "calls": calls,
                               "talk_minutes": talk_minutes, "tts_chars": tts_chars,
                               "sms_sent": sms_sent, "computed_at": now},
                              key=("tenant_id", "date"))
            self._conn.commit()

    def get_usage(self, tenant_id: str, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT tenant_id, date, calls, talk_minutes, tts_chars, sms_sent,"
                " computed_at FROM usage_daily WHERE tenant_id = ?"
                " ORDER BY date DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [
            {"tenant_id": r[0], "date": r[1], "calls": r[2],
             "talk_minutes": r[3], "tts_chars": r[4], "sms_sent": r[5],
             "computed_at": r[6]}
            for r in rows
        ]

    # -- analytics helpers for the background agents -----------------------
    def top_selling_items(self, tenant_id: str, days: int = 30, limit: int = 5,
                          now: float | None = None) -> list[dict]:
        """Best sellers from the orders table (payload JSON lines).

        Returns [{name, qty, revenue}]. Empty list when the orders table is
        absent (it lives in the same DB file only in the app's default
        wiring) or the tenant has no orders yet.
        """
        now = now if now is not None else time.time()
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT payload FROM orders WHERE tenant_id = ? AND saved_at >= ?",
                    (tenant_id, now - days * 86400),
                ).fetchall()
        except dbmod.Error:
            return []
        agg: dict[str, dict] = {}
        for (payload,) in rows:
            try:
                order = json.loads(payload)
            except (ValueError, TypeError):
                continue
            for line in order.get("lines", []) or []:
                name = str(line.get("item_name", "")).strip()
                if not name:
                    continue
                qty = int(line.get("quantity", 0) or 0)
                price = float(line.get("unit_price", 0) or 0)
                a = agg.setdefault(name, {"name": name, "qty": 0, "revenue": 0.0})
                a["qty"] += qty
                a["revenue"] = round(a["revenue"] + qty * price, 2)
        ranked = sorted(agg.values(), key=lambda a: (-a["qty"], -a["revenue"], a["name"]))
        return ranked[:limit]

    def calls_in_window(self, tenant_id: str | None, since_ts: float,
                        until_ts: float | None = None) -> list[dict]:
        """Call rows with timing + raw turns JSON for agent rollups.

        tenant_id=None sweeps every tenant (platform-wide checks).
        """
        with self._lock:
            q = ("SELECT call_sid, tenant_id, from_number, status, turns,"
                 " started_at, updated_at"
                 " FROM call_transcripts WHERE started_at >= ?")
            params: list = [since_ts]
            if until_ts is not None:
                q += " AND started_at < ?"
                params.append(until_ts)
            if tenant_id is not None:
                q += " AND tenant_id = ?"
                params.append(tenant_id)
            q += " ORDER BY started_at"
            rows = self._conn.execute(q, params).fetchall()
        return [
            {"call_sid": r[0], "tenant_id": r[1], "from_number": r[2],
             "status": r[3], "turns": r[4], "started_at": r[5], "updated_at": r[6]}
            for r in rows
        ]
