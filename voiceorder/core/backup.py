from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

# "confirmed" is every order that reached the POS; "unpaid" additionally marks
# a confirmed Square order still waiting on its payment link; "failed" is one
# the POS never accepted after a retry; "transferred" is a call handed to
# staff. Every failure in section 8 lands in exactly one of these instead of
# a silent line or a phantom order.
BackupKind = str  # "confirmed" | "unpaid" | "failed" | "transferred"


@dataclass
class BackupEntry:
    call_id: str
    kind: BackupKind
    detail: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class BackupStore(Protocol):
    """What OrderTools writes order events to. The in-memory BackupScreen
    below is the default (and what every test uses); storage/postgres_backup.py
    has the real, Postgres-backed implementation for when a restaurant's
    order history needs to survive a restart."""

    def record(self, call_id: str, kind: BackupKind, detail: str) -> BackupEntry: ...
    def list_entries(self, kind: BackupKind | None = None) -> list[BackupEntry]: ...


@dataclass
class BackupScreen:
    """What staff check for anything that didn't make it cleanly onto the POS."""

    entries: list[BackupEntry] = field(default_factory=list)

    def record(self, call_id: str, kind: BackupKind, detail: str) -> BackupEntry:
        entry = BackupEntry(call_id=call_id, kind=kind, detail=detail)
        self.entries.append(entry)
        return entry

    def list_entries(self, kind: BackupKind | None = None) -> list[BackupEntry]:
        if kind is None:
            return list(self.entries)
        return [e for e in self.entries if e.kind == kind]
