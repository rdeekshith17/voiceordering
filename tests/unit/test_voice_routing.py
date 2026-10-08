"""PR 2: AI ON/OFF routing through the real Twilio webhooks and portal APIs."""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voiceorder.api import main as api_main
from voiceorder.api.storage import InMemoryOrderStore
from voiceorder.portal.portal import PortalDeps, build_portal_router
from voiceorder.routing import RoutingConfig, Window
from voiceorder.tenants.store import TenantStore

T = api_main.default_tenant
STORE = api_main.tenant_store
REAL_STAFF = "+12145550100"


class _Session:
    caller = None

    def __init__(self):
        self.cart = None


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(api_main, "_new_twilio_session",
                        lambda tenant=None, caller_number="": _Session())
    monkeypatch.setattr(api_main, "_speak_to_url", lambda *a, **k: None)
    db = STORE._conn
    for table in ("voice_routing", "voice_routing_windows", "voice_routing_overrides",
                  "tenant_feature_flags"):
        db.execute(f"DELETE FROM {table}")
    db.commit()
    STORE.set_settings(T.id, {"transfer_number": REAL_STAFF, "timezone": "America/Chicago"})
    yield
    api_main._twilio_sessions.clear()
    STORE.set_settings(T.id, {"transfer_number": api_main.settings.transfer_number})


def call(sid: str):
    client = TestClient(api_main.app)
    r = client.post("/twilio/voice", data={"CallSid": sid, "From": "+14155550123",
                                            "To": T.phone_number or "+15550000000"})
    assert r.status_code == 200
    return r.text, client


def controls(mode="always_off", off_action="transfer", no_answer="voicemail", msg="", windows=()):
    STORE.set_flag(T.id, "voice_schedule_enabled", True, actor="test")
    STORE.save_routing(T.id, RoutingConfig(mode=mode, off_action=off_action,
                                           no_answer_action=no_answer, closed_message=msg,
                                           windows=list(windows)), actor="test")


def meta(sid):
    return STORE.get_transcript(T.id, sid)["meta"]


# --- call routing ------------------------------------------------------------------
def test_controls_off_keeps_todays_behaviour():
    STORE.save_routing(T.id, RoutingConfig(mode="always_off"), actor="test")  # saved but flag off
    xml, _ = call("CAr1")
    assert "<Gather" in xml and "CAr1" in api_main._twilio_sessions
    assert meta("CAr1") == {}


def test_ai_off_forwards_to_staff_with_ring_timeout_and_fallback():
    controls()
    xml, _ = call("CAr2")
    assert "CAr2" not in api_main._twilio_sessions          # no AI session at all
    assert f'<Dial timeout="20" action="https://example.test/twilio/dial-status?tid={T.id}"' in xml
    assert f">{REAL_STAFF}</Dial>" in xml
    assert meta("CAr2")["route"] == "forwarded" and meta("CAr2")["reason"] == "Always off"


def test_placeholder_transfer_number_goes_straight_to_voicemail():
    controls()
    STORE.set_settings(T.id, {"transfer_number": "+15550134200"})
    xml, _ = call("CAr3")
    assert "<Dial" not in xml and "<Record" in xml
    assert f"/twilio/voicemail?tid={T.id}" in xml
    assert meta("CAr3")["route"] == "voicemail"


@pytest.mark.parametrize("outcome,expect", [("completed", "<Hangup />"), ("no-answer", "<Record"),
                                            ("busy", "<Record"), ("failed", "<Record")])
def test_dial_outcome_hangs_up_or_falls_back(outcome, expect):
    controls()
    _, client = call("CAr4")
    r = client.post(f"/twilio/dial-status?tid={T.id}",
                    data={"CallSid": "CAr4", "DialCallStatus": outcome})
    assert expect in r.text
    assert meta("CAr4")["transfer_result"] == outcome


def test_no_answer_can_play_the_closed_message_instead():
    controls(no_answer="message", msg="We're closed until 11 AM.")
    _, client = call("CAr5")
    r = client.post(f"/twilio/dial-status?tid={T.id}",
                    data={"CallSid": "CAr5", "DialCallStatus": "no-answer"})
    assert "<Say>We're closed until 11 AM.</Say><Hangup />" in r.text.replace("&apos;", "'")
    assert meta("CAr5")["route"] == "closed"


def test_closed_message_and_voicemail_as_the_off_action():
    controls(off_action="message")
    xml, _ = call("CAr6")
    assert "can't take your call right now" in xml.replace("&apos;", "'") and "<Hangup />" in xml
    controls(off_action="voicemail")
    xml, client = call("CAr7")
    assert "<Record" in xml
    r = client.post(f"/twilio/voicemail?tid={T.id}", data={
        "CallSid": "CAr7", "RecordingUrl": "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1",
        "RecordingDuration": "14"})
    assert "we got your message" in r.text
    assert meta("CAr7")["voicemail_url"].endswith("/RE1") and meta("CAr7")["voicemail_seconds"] == 14
    client.post(f"/twilio/voicemail?tid={T.id}", data={"CallSid": "CAr7",
                                                       "RecordingUrl": "https://evil.example/x"})
    assert meta("CAr7")["voicemail_url"].endswith("/RE1")  # only Twilio links are kept


def test_schedule_decides_per_call(monkeypatch):
    every_day_lunch = [Window(d, 11 * 60, 14 * 60) for d in range(7)]
    controls(mode="scheduled", windows=every_day_lunch)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    noon = datetime(2026, 10, 7, 12, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    night = datetime(2026, 10, 7, 23, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    assert api_main.routing_decision(STORE.get_tenant(T.id), noon)[0].ai is True
    off = api_main.routing_decision(STORE.get_tenant(T.id), night)[0]
    assert off.ai is False and off.reason == "Outside scheduled hours"


def test_policy_failure_sends_calls_to_staff_not_silently_to_the_ai(monkeypatch):
    controls(mode="always_on")
    monkeypatch.setattr(STORE, "routing_config", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    d, cfg, _ = api_main.routing_decision(STORE.get_tenant(T.id))
    assert d.ai is False and d.reason == "Routing policy unavailable" and cfg.off_action == "transfer"
    STORE.set_settings(T.id, {"transfer_number": ""})
    assert api_main.routing_decision(STORE.get_tenant(T.id))[0].ai is True  # nobody to forward to


def test_platform_emergency_stop_beats_always_on():
    controls(mode="always_on")
    STORE.set_routing_emergency("*", True, actor="ops")
    try:
        xml, _ = call("CAr8")
        assert "<Dial" in xml and meta("CAr8")["reason"] == "Platform emergency stop"
    finally:
        STORE.set_routing_emergency("*", False, actor="ops")


def test_changing_the_mode_mid_call_doesnt_cut_off_the_ai_call():
    controls(mode="always_on")
    xml, client = call("CAr9")
    assert "CAr9" in api_main._twilio_sessions and meta("CAr9")["route"] == "ai"
    controls(mode="always_off")
    assert "CAr9" in api_main._twilio_sessions  # the live call keeps its AI session


# --- the AI's own "talk to a person" transfer -----------------------------------------------------
class _TransferCart:
    transferred = True
    state = None


class _TransferSession:
    def __init__(self):
        self.cart = _TransferCart()
        self.ctx = api_main.tenant_context(STORE.get_tenant(T.id))


def test_ai_transfer_uses_fallback_only_with_controls_on():
    entry = {"turn": {"text": "Connecting you to staff."}, "audio_url": None}
    xml = api_main._twilio_final_twiml("CAt1", _TransferSession(), entry, {})
    assert f"<Dial>{REAL_STAFF}</Dial>" in xml  # unchanged without controls
    controls(mode="always_on")
    STORE.start_call("CAt2", T.id, "+14155550123", "")
    xml = api_main._twilio_final_twiml("CAt2", _TransferSession(), entry, {})
    assert 'timeout="20"' in xml and "/twilio/dial-status" in xml
    STORE.set_settings(T.id, {"transfer_number": "+15550134200"})
    STORE.start_call("CAt3", T.id, "+14155550123", "")
    xml = api_main._twilio_final_twiml("CAt3", _TransferSession(), entry, {})
    assert "<Record" in xml and "<Dial" not in xml  # placeholder: voicemail instead of a dead line


# --- portal -----------------------------------------------------------------------------------------
def _portal(tmp_path):
    store = TenantStore(tmp_path / "p.db")
    app = FastAPI()
    app.include_router(build_portal_router(PortalDeps(
        tenants=store, order_store=InMemoryOrderStore(),
        build_adapter=lambda t, o: None, get_catalog=lambda t: None)))
    owner = TestClient(app, follow_redirects=False)
    owner.post("/portal/signup", data={"restaurant": "Hyderabad House", "phone": "+15622680097",
                                       "timezone": "America/Chicago",
                                       "email": "o@example.com", "password": "password123"})
    return app, store, store.get_tenant_by_name("Hyderabad House"), owner


def test_owner_turns_on_a_schedule_pauses_and_resumes(tmp_path):
    app, store, t, owner = _portal(tmp_path)
    page = owner.get("/portal/phone")
    assert page.status_code == 200 and "AI answers every call" in page.text
    body = {"enabled": True, "mode": "scheduled", "off_action": "transfer",
            "no_answer_action": "message", "closed_message": "Closed!", "expected_version": 0,
            "windows": [{"day": 4, "start": "18:00", "end": "01:00"}]}
    assert owner.put("/portal/api/voice-routing", json=body).json()["ok"]
    v = owner.get("/portal/api/voice-routing").json()
    assert v["enabled"] and v["mode"] == "scheduled" and v["version"] == 1
    assert v["windows"] == [{"day": 4, "start": "18:00", "end": "01:00"}]
    assert owner.put("/portal/api/voice-routing", json=body).status_code == 400  # stale version
    assert owner.post("/portal/api/voice-routing/pause", json={"until": "resume"}).json()["ok"]
    v = owner.get("/portal/api/voice-routing").json()
    assert v["effective"]["ai"] is False and v["effective"]["reason"] == "Paused"
    assert "Resume AI" in owner.get("/portal/phone").text
    owner.post("/portal/api/voice-routing/resume", json={})
    assert not any(o["active"] for o in owner.get("/portal/api/voice-routing").json()["overrides"])
    owner.post("/portal/api/voice-routing/stop", json={})
    assert owner.get("/portal/api/voice-routing").json()["effective"]["reason"].startswith("AI turned off now")
    owner.post("/portal/api/voice-routing/resume", json={})
    assert owner.get("/portal/api/voice-routing").json()["emergency_off"] is False
    actions = {e["action"] for e in store.list_audit(t.id)}
    assert {"voice_routing.update", "flag.set", "voice_routing.pause_added",
            "voice_routing.override_cancelled", "voice_routing.emergency_off"} <= actions


def test_holiday_exception_is_whole_local_days_and_tenant_scoped(tmp_path):
    app, store, t, owner = _portal(tmp_path)
    r = owner.post("/portal/api/voice-routing/exceptions",
                   json={"from": "2026-12-25", "to": "2026-12-25", "ai_on": False, "reason": "Christmas"})
    assert r.json()["ok"]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    cfg, _ = store.routing_config(t.id, now=datetime(2026, 12, 1).timestamp())
    o = cfg.overrides[0]
    chi = ZoneInfo("America/Chicago")
    assert datetime.fromtimestamp(o.starts_at, chi).strftime("%m-%d %H:%M") == "12-25 00:00"
    assert datetime.fromtimestamp(o.ends_at, chi).strftime("%m-%d %H:%M") == "12-26 00:00"
    other = store.create_tenant("Taco Palace")
    store.create_user(other.id, "t@example.com", "password123")
    intruder = TestClient(app, follow_redirects=False)
    intruder.post("/portal/login", data={"email": "t@example.com", "password": "password123"})
    assert intruder.delete(f"/portal/api/voice-routing/exceptions/{o.id}").status_code == 404
    assert owner.delete(f"/portal/api/voice-routing/exceptions/{o.id}").json()["ok"]
    bad = owner.post("/portal/api/voice-routing/exceptions", json={"from": "2026-12-26", "to": "2026-12-25"})
    assert bad.status_code == 400


def test_invalid_schedules_are_rejected_and_kitchen_cannot_change_anything(tmp_path):
    app, store, t, owner = _portal(tmp_path)
    bad = {"enabled": True, "mode": "scheduled", "windows": [{"day": 9, "start": "10:00", "end": "11:00"}]}
    assert owner.put("/portal/api/voice-routing", json=bad).status_code == 400
    bad["windows"] = [{"day": 1, "start": "25:00", "end": "11:00"}]
    assert owner.put("/portal/api/voice-routing", json=bad).status_code == 400
    assert owner.put("/portal/api/voice-routing", json={"mode": "sometimes"}).status_code == 400
    store.create_user(t.id, "cook@example.com", "password123", role="kitchen")
    cook = TestClient(app, follow_redirects=False)
    cook.post("/portal/login", data={"email": "cook@example.com", "password": "password123"})
    assert cook.get("/portal/phone").headers["location"] == "/portal/orders"
    assert cook.post("/portal/api/voice-routing/stop", json={}).status_code == 403
    assert store.routing_config(t.id)[0].emergency_off is False
