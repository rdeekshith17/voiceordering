from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# The kinds from section 8: an order that's sitting unpaid (Square, before the
# link is paid), one the POS never accepted after a retry, or a call that got
# handed to staff. Every failure in the plan lands in exactly one of these
# instead of a silent line or a phantom order.
BackupKind = str  # "unpaid" | "failed" | "transferred"


@dataclass
class BackupEntry:
    call_id: str
    kind: BackupKind
    detail: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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
