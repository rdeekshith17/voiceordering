from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg2
import psycopg2.pool

from ..core.backup import BackupEntry, BackupKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS backup_entries (
    id SERIAL PRIMARY KEY,
    restaurant_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS backup_entries_restaurant_kind_idx
    ON backup_entries (restaurant_id, kind);
"""


class _Pool:
    """One shared connection pool per process (per dsn), reused by every
    restaurant's PostgresBackupStore rather than opening a pool each."""

    _pools: dict[str, psycopg2.pool.SimpleConnectionPool] = {}

    @classmethod
    def get(cls, dsn: str) -> psycopg2.pool.SimpleConnectionPool:
        if dsn not in cls._pools:
            pool = psycopg2.pool.SimpleConnectionPool(1, 10, dsn)
            conn = pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(_SCHEMA)
                conn.commit()
            finally:
                pool.putconn(conn)
            cls._pools[dsn] = pool
        return cls._pools[dsn]


@dataclass
class PostgresBackupStore:
    """The real BackupStore (core/backup.py's Protocol) -- one instance per
    restaurant, all sharing the same connection pool and table, filtered by
    restaurant_id."""

    dsn: str
    restaurant_id: str

    def __post_init__(self) -> None:
        self._pool = _Pool.get(self.dsn)

    @contextmanager
    def _cursor(self) -> Iterator[tuple]:
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                yield conn, cur
        finally:
            self._pool.putconn(conn)

    def record(self, call_id: str, kind: BackupKind, detail: str) -> BackupEntry:
        with self._cursor() as (conn, cur):
            cur.execute(
                "INSERT INTO backup_entries (restaurant_id, call_id, kind, detail) "
                "VALUES (%s, %s, %s, %s) RETURNING created_at",
                (self.restaurant_id, call_id, kind, detail),
            )
            (created_at,) = cur.fetchone()
            conn.commit()
        return BackupEntry(call_id=call_id, kind=kind, detail=detail, created_at=created_at)

    def list_entries(self, kind: BackupKind | None = None) -> list[BackupEntry]:
        query = (
            "SELECT call_id, kind, detail, created_at FROM backup_entries "
            "WHERE restaurant_id = %s"
        )
        params: list = [self.restaurant_id]
        if kind is not None:
            query += " AND kind = %s"
            params.append(kind)
        query += " ORDER BY created_at"
        with self._cursor() as (conn, cur):
            cur.execute(query, params)
            rows = cur.fetchall()
        return [
            BackupEntry(call_id=r[0], kind=r[1], detail=r[2], created_at=r[3])
            for r in rows
        ]
