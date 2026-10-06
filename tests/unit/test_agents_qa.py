"""Tests for the CallQaAgent: heuristic flags, severity, dedupe, auto-resolve.

No LLM, no network: transcripts are built with the store API.
"""
from __future__ import annotations

import pytest

from voiceorder.agents import qa
from voiceorder.tenants.store import TenantStore


@pytest.fixture()
def store(tmp_path):
    return TenantStore(tmp_path / "qa.db")


@pytest.fixture()
def tenant(store):
    t = store.create_tenant("Taco Palace", "+15551234567")
    store.set_settings(t.id, {"pos_profile": "square"})
    return store.get_tenant(t.id)


def _call(store, tenant_id, sid, turns, status="completed"):
    store.start_call(sid, tenant_id, "+15550001111", "+15551234567")
    for heard, reply, tools in turns:
        store.append_turn(sid, heard, reply, tools)
    store.end_call(sid, status)


def _open(store, tenant_id):
    return [t for t in store.list_tickets(tenant_id, include_platform=True)
            if t["kind"] == "call_quality" and t["status"] in ("open", "acked")]


def test_failed_order_flags_warning(store, tenant):
    _call(store, tenant.id, "ca1", [
        ("two tacos please", "adding two tacos",
         [{"name": "submit_order", "ok": False}]),
    ])
    new = qa.run_qa_review(store)
    assert len(new) == 1
    t = new[0]
    assert t["kind"] == "call_quality"
    assert t["severity"] == "warning"
    assert "ca1" in t["detail"]
    assert "failed_order" in t["detail"]


def test_confusion_loop_flags_warning(store, tenant):
    _call(store, tenant.id, "ca2", [
        ("i want a burrito", "Sorry, I didn't catch that.", []),
        ("a burrito", "Sorry, I didn't catch that.", []),
        ("BURRITO", "Sorry, I didn't catch that.", []),
    ])
    new = qa.run_qa_review(store)
    assert len(new) == 1
    assert new[0]["severity"] == "warning"
    assert "confusion_loop" in new[0]["detail"]


def test_frustration_is_info_only(store, tenant):
    _call(store, tenant.id, "ca3", [
        ("this is ridiculous, I want a human", "let me help", []),
    ])
    new = qa.run_qa_review(store)
    assert len(new) == 1
    assert new[0]["severity"] == "info"
    assert "customer_frustration" in new[0]["detail"]


def test_transfer_is_info_only(store, tenant):
    _call(store, tenant.id, "ca4", [
        ("let me talk to someone", "transferring you now",
         [{"name": "transfer_call", "ok": True}]),
    ])
    new = qa.run_qa_review(store)
    assert len(new) == 1
    assert new[0]["severity"] == "info"
    assert "staff_transfer" in new[0]["detail"]


def test_successful_order_is_clean(store, tenant):
    _call(store, tenant.id, "ca5", [
        ("two tacos please", "order placed, thanks",
         [{"name": "submit_order", "ok": True}]),
    ])
    assert qa.run_qa_review(store) == []


def test_dedupe_on_second_run(store, tenant):
    _call(store, tenant.id, "ca6", [
        ("x", "Sorry, I didn't catch that.", []),
        ("y", "Sorry, I didn't catch that.", []),
        ("z", "Sorry, I didn't catch that.", []),
    ])
    assert len(qa.run_qa_review(store)) == 1
    assert qa.run_qa_review(store) == []
    assert len(_open(store, tenant.id)) == 1


def test_clean_day_resolves(store, tenant):
    _call(store, tenant.id, "ca7", [
        ("x", "Sorry, I didn't catch that.", []),
        ("y", "Sorry, I didn't catch that.", []),
        ("z", "Sorry, I didn't catch that.", []),
    ])
    assert len(qa.run_qa_review(store)) == 1
    # next day: the bad call is outside the 24h window -> auto-resolve
    import time
    new = qa.run_qa_review(store, now=time.time() + 25 * 3600)
    assert new == []
    resolved = [t for t in store.list_tickets(tenant.id)
                if t["kind"] == "call_quality"]
    assert resolved and resolved[0]["status"] == "resolved"


def test_llm_hook_failure_keeps_heuristics(store, tenant):
    def boom(transcript):
        raise RuntimeError("llm exploded")
    _call(store, tenant.id, "ca8", [
        ("two tacos", "adding them", [{"name": "submit_order", "ok": False}]),
    ])
    new = qa.run_qa_review(store, llm_assess=boom)
    assert len(new) == 1 and new[0]["severity"] == "warning"
