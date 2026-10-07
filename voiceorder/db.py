"""One database connection that is either SQLite or Postgres.

Storage classes write SQLite-flavoured SQL with `?` placeholders; this
wrapper runs it as-is on SQLite and translates it for Postgres. A target
starting with postgres:// or postgresql:// (e.g. the DATABASE_URL a free
Neon or Supabase database gives you) selects Postgres; anything else is a
SQLite file path.

Postgres runs in autocommit mode, so commit() is a no-op there. Hosted free
tiers drop idle connections, so a statement that fails on a dead connection
is retried once on a fresh one.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger("voiceorder.db")

try:  # optional at import time: only needed when a Postgres URL is configured
    import psycopg
except ImportError:  # pragma: no cover - exercised only without the dependency
    psycopg = None

# Catch these around queries that may hit a missing table on either backend.
Error: tuple[type[BaseException], ...] = (sqlite3.Error,) + ((psycopg.Error,) if psycopg else ())


def is_postgres(target: str | Path) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


def describe(target: str | Path) -> str:
    """Loggable name for a target: never prints a password."""
    t = str(target)
    if not is_postgres(t):
        return t
    host = t.split("@", 1)[-1].split("?", 1)[0]
    return f"postgres://…@{host}"


_REAL = re.compile(r"\bREAL\b")


def _pg_sql(sql: str) -> str:
    # psycopg uses %s placeholders, so a literal % must be doubled first.
    return sql.replace("%", "%%").replace("?", "%s")


class Database:
    def __init__(self, target: str | Path) -> None:
        self.target = str(target)
        self.pg = is_postgres(self.target)
        self._lock = threading.Lock()
        self._conn = self._open()

    def _open(self) -> Any:
        if self.pg:
            if psycopg is None:
                raise RuntimeError("DATABASE_URL is Postgres but psycopg is not installed")
            # prepare_threshold=None keeps it working behind transaction poolers
            # (Supabase/Neon pooled URLs), which can't hold prepared statements.
            return psycopg.connect(self.target, autocommit=True, connect_timeout=15,
                                   prepare_threshold=None)
        path = Path(self.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _reconnect(self) -> None:
        log.warning("database connection lost; reconnecting to %s", describe(self.target))
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = self._open()

    def execute(self, sql: str, params: Sequence[Any] | Iterable[Any] = ()) -> Any:
        """Run one statement; returns a cursor (fetchone/fetchall/rowcount)."""
        if not self.pg:
            return self._conn.execute(sql, tuple(params))
        sql, params = _pg_sql(sql), tuple(params)
        with self._lock:
            try:
                return self._conn.execute(sql, params)
            except psycopg.OperationalError:
                if not self._conn.closed:
                    raise
                self._reconnect()
                return self._conn.execute(sql, params)

    def executescript(self, script: str) -> None:
        """Run several `;`-separated DDL statements (no params)."""
        if not self.pg:
            self._conn.executescript(script)
            return
        script = _REAL.sub("DOUBLE PRECISION", script)
        script = "\n".join(l for l in script.splitlines() if not l.strip().startswith("--"))
        for stmt in script.split(";"):
            if stmt.strip():
                self.execute(stmt)

    def commit(self) -> None:
        if not self.pg:
            self._conn.commit()

    def upsert(self, table: str, row: dict[str, Any], key: Sequence[str]) -> None:
        """Insert `row`, replacing any existing row with the same `key` columns."""
        cols = list(row)
        marks = ", ".join("?" for _ in cols)
        if self.pg:
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in key)
            sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})"
                   f" ON CONFLICT ({', '.join(key)}) DO "
                   + (f"UPDATE SET {updates}" if updates else "NOTHING"))
        else:
            sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({marks})"
        self.execute(sql, [row[c] for c in cols])

    def columns(self, table: str) -> list[str]:
        """Column names of `table`; empty when the table doesn't exist."""
        if self.pg:
            rows = self.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = ?",
                (table,),
            ).fetchall()
            return [r[0] for r in rows]
        return [r[1] for r in self.execute(f"PRAGMA table_info({table})").fetchall()]
