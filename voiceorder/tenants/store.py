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
from . import crypto


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
        return conn

    # -- tenants -----------------------------------------------------------
    def create_tenant(self, name: str, phone_number: str = "") -> Tenant:
        tid = uuid.uuid4().hex[:12]
        slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or tid
        phone = normalize_number(phone_number)
        with self._lock:
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

    def get_tenant_by_number(self, phone_number: str) -> Tenant | None:
        phone = normalize_number(phone_number)
        if not phone:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM tenants WHERE phone_number = ? AND status = 'active'",
                (phone,),
            ).fetchone()
        return self.get_tenant(row[0]) if row else None

    def set_phone_number(self, tenant_id: str, phone_number: str) -> None:
        with self._lock:
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
    def create_user(self, tenant_id: str, email: str, password: str) -> PortalUser:
        email = email.strip().lower()
        if not email or "@" not in email or len(password) < 8:
            raise ValueError("need a valid email and a password of 8+ characters")
        uid = uuid.uuid4().hex[:12]
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM tenant_users WHERE email = ?", (email,)
            ).fetchone():
                raise ValueError("that email is already registered")
            self._conn.execute(
                "INSERT INTO tenant_users (id, tenant_id, email, password_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (uid, tenant_id, email, crypto.hash_password(password), time.time()),
            )
            self._conn.commit()
        return PortalUser(id=uid, tenant_id=tenant_id, email=email)

    def verify_user(self, email: str, password: str) -> PortalUser | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, tenant_id, email, password_hash FROM tenant_users"
                " WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        if not row or not crypto.verify_password(password, row[3]):
            return None
        return PortalUser(id=row[0], tenant_id=row[1], email=row[2])

    def get_user(self, user_id: str) -> PortalUser | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, tenant_id, email FROM tenant_users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return PortalUser(id=row[0], tenant_id=row[1], email=row[2]) if row else None

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
                " started_at, updated_at FROM call_transcripts"
                " WHERE call_sid = ? AND tenant_id = ?",
                (call_sid, tenant_id),
            ).fetchone()
        if not row:
            return None
        return {
            "call_sid": row[0], "tenant_id": row[1], "from_number": row[2],
            "to_number": row[3], "status": row[4], "turns": json.loads(row[5]),
            "started_at": row[6], "updated_at": row[7],
        }

    def list_calls(self, tenant_id: str, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT call_sid, from_number, to_number, status, turns,"
                " started_at, updated_at FROM call_transcripts"
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
                "started_at": r[5], "updated_at": r[6],
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
