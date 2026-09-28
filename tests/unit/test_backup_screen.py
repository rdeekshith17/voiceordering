from __future__ import annotations

from voiceorder.core.backup import BackupScreen


def test_record_and_list_all_entries():
    screen = BackupScreen()
    screen.record("call-1", "unpaid", "order awaiting payment")
    screen.record("call-2", "transferred", "caller asked for a person")
    assert len(screen.list_entries()) == 2


def test_list_entries_filters_by_kind():
    screen = BackupScreen()
    screen.record("call-1", "unpaid", "order awaiting payment")
    screen.record("call-2", "failed", "POS unreachable")
    unpaid = screen.list_entries(kind="unpaid")
    assert len(unpaid) == 1
    assert unpaid[0].call_id == "call-1"
