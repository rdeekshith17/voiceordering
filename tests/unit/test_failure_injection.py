"""PR 6: break one dependency at a time mid-call and check the caller always
gets a sensible spoken response (never Twilio's "application error"), and no
order is lost or duplicated."""
from __future__ import annotations

import html
import re
import time

import pytest
from fastapi.testclient import TestClient

from voiceorder.agent.llm import LLMResponse
from voiceorder.api import main as api_main
from voiceorder.voice.tts import TtsError

T = api_main.default_tenant
STORE = api_main.tenant_store
REAL_SPEAK = api_main._speak_to_url  # captured before any test stubs it


class AI:
    def __init__(self, *responses):
        self.responses = list(responses)

    def complete(self, *, system, messages, tools, model):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture()
def call(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "test-model")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(api_main, "_speak_to_url", lambda *a, **k: None)
    db = STORE._conn
    for table in ("tenant_feature_flags", "approval_requests", "approval_events", "call_sessions"):
        db.execute(f"DELETE FROM {table}")
    db.commit()

    def start(sid, ai):
        monkeypatch.setattr(api_main, "_chat_llm_client", lambda: ai)
        client = TestClient(api_main.app)
        form = {"CallSid": sid, "From": "+14155550123", "To": T.phone_number or "+1"}
        r = client.post("/twilio/voice", data=form)
        assert r.status_code == 200
        return client, form, r.text
    yield start
    api_main._twilio_sessions.clear()


def say(xml):
    return " ".join(html.unescape(m) for m in re.findall(r"<Say>(.*?)</Say>", xml))


def finish_turn(client, form, speech):
    xml = client.post("/twilio/gather", data={**form, "SpeechResult": speech}).text
    for _ in range(100):
        m = re.search(r'<Redirect method="POST">([^<]+)</Redirect>', xml)
        if not m or "/twilio/hold" in m.group(1):
            return xml
        time.sleep(0.02)
        r = client.post(html.unescape(m.group(1)).replace("https://example.test", ""), data=form)
        assert r.status_code == 200, r.text  # never a 500 to Twilio
        xml = r.text
    raise AssertionError("turn never finished")


def test_ai_provider_error_apologizes_and_keeps_listening(call):
    client, form, _ = call("CAf1", AI(RuntimeError("Anthropic 529 overloaded"),
                                     LLMResponse(text="Sure, what can I get you?")))
    xml = finish_turn(client, form, "hi")
    assert "hit a snag" in say(xml) and "<Gather" in xml
    assert "what can I get you" in say(finish_turn(client, form, "hello?"))  # recovers next turn


def test_voice_service_outage_falls_back_to_twilio_speech(call, monkeypatch):
    from voiceorder.voice import tts
    calls = []

    def down(*a, **k):
        calls.append(a)
        raise TtsError("ElevenLabs down")
    monkeypatch.setattr(tts, "synthesize", down)
    client, form, _ = call("CAf2", AI(LLMResponse(text="Two tacos, anything else?")))
    monkeypatch.setattr(api_main, "_speak_to_url", REAL_SPEAK)  # the real code path, not a stub
    greeting = client.post("/twilio/voice", data={**form, "CallSid": "CAf2b"}).text
    form = {**form, "CallSid": "CAf2b"}
    api_main._twilio_sessions["CAf2b"].llm = AI(LLMResponse(text="Two tacos, anything else?"))
    assert calls, "the real synthesizer was tried"
    assert "<Say>" in greeting and "<Play>" not in greeting
    assert "Two tacos" in say(finish_turn(client, form, "two tacos"))


def test_database_write_failure_doesnt_break_the_call(call, monkeypatch):
    client, form, _ = call("CAf3", AI(LLMResponse(text="Got it.")))
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Neon connection reset"))
    monkeypatch.setattr(STORE, "save_call_session", boom)
    monkeypatch.setattr(STORE, "append_turn", boom)
    xml = finish_turn(client, form, "one taco")
    assert "Got it." in say(xml) and "<Gather" in xml


def test_database_outage_while_on_hold_keeps_holding_then_apologizes(call, monkeypatch):
    STORE.set_flag(T.id, "hitl_enabled", True, actor="test")
    client, form, _ = call("CAf4", AI(LLMResponse(tool_calls=[{
        "id": "k", "name": "request_kitchen_approval",
        "arguments": {"request": "extra crispy", "category": "custom_modification"}}])))
    xml = finish_turn(client, form, "extra crispy please")
    hold = html.unescape(re.search(r'<Redirect method="POST">([^<]+)</Redirect>', xml).group(1))
    hold = hold.replace("https://example.test", "")
    monkeypatch.setattr(STORE, "get_approval",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Neon down")))
    r = client.post(hold, data=form)
    assert r.status_code == 200 and "/twilio/hold" in r.text and "n=1" in r.text  # still holding
    r = client.post(hold.replace("n=0", "n=60"), data=form)
    assert r.status_code == 200 and "couldn't reach the kitchen" in say(r.text) and "<Gather" in r.text


def test_restoring_a_session_during_an_outage_ends_politely(call, monkeypatch):
    client, form, _ = call("CAf5", AI())
    api_main._twilio_sessions.clear()
    monkeypatch.setattr(STORE, "load_call_session",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Neon down")))
    r = client.post("/twilio/gather", data={**form, "SpeechResult": "hello"})
    assert r.status_code == 200 and "lost track of your call" in say(r.text)


def test_replayed_webhooks_are_harmless(call):
    submit = LLMResponse(tool_calls=[{"id": "s", "name": "submit_order", "arguments": {
        "customer_name": "Sam", "customer_phone": "+14155550123", "confirmed": True}}])
    client, form, _ = call("CAf6", AI(
        LLMResponse(tool_calls=[{"id": "a", "name": "add_item", "arguments": {"item_ref": "T1"}}]),
        LLMResponse(tool_calls=[{"id": "g", "name": "get_cart", "arguments": {}}]),
        LLMResponse(text="One chicken taco, right?"), submit))
    assert client.post("/twilio/voice", data=form).status_code == 200  # Twilio retried the start
    finish_turn(client, form, "one chicken taco")
    finish_turn(client, form, "yes send it")
    orders_before = len([o for o in api_main.order_store.list_by_tenant(T.id, 500) if o.get("call_id") == "CAf6"])
    for _ in range(2):  # Twilio re-sends the "call ended" callback
        assert client.post("/twilio/status", data={**form, "CallStatus": "completed"}).status_code == 200
    orders_after = len([o for o in api_main.order_store.list_by_tenant(T.id, 500) if o.get("call_id") == "CAf6"])
    assert orders_before == orders_after == 1


def test_pos_outage_parks_the_order_for_staff_and_tells_the_truth(call):
    from voiceorder.pos_adapters.fake import FakePos

    submit = LLMResponse(tool_calls=[{"id": "s", "name": "submit_order", "arguments": {
        "customer_name": "Sam Patel", "customer_phone": "+14155550123", "confirmed": True}}])
    client, form, _ = call("CAf7", AI(
        LLMResponse(tool_calls=[{"id": "a", "name": "add_item", "arguments": {"item_ref": "T1", "quantity": 2}}]),
        LLMResponse(tool_calls=[{"id": "g", "name": "get_cart", "arguments": {}}]),
        LLMResponse(text="Two chicken tacos, right?"),
        submit, LLMResponse(text="Sorry, it didn't go through; staff will call you back."),
        submit, LLMResponse(text="Still not going through, sorry.")))
    session = api_main._twilio_sessions["CAf7"]
    finish_turn(client, form, "two chicken tacos")
    assert len(session.cart.lines) == 1
    session.pos = FakePos(catalog=session.catalog, mode="down")  # Square goes down before submit
    finish_turn(client, form, "yes, send it")
    told = str(session.history)  # what the AI was given to relay
    assert "get your order into the restaurant" in told and "passed it to the staff" in told
    assert "saved your order" not in told
    finish_turn(client, form, "please try again")  # the caller retries; still down
    parked = [o for o in api_main.order_store.list_by_tenant(T.id, 500) if o.get("call_id") == "CAf7"]
    assert len(parked) == 1 and parked[0]["status"] == "pos_failed"          # once, not twice
    assert parked[0]["customer_phone"] == "+14155550123" and parked[0]["lines"][0]["quantity"] == 2
    ticket = [t for t in STORE.list_tickets(T.id) if t["title"].startswith("Order didn't reach the POS")]
    assert len(ticket) == 1 and ticket[0]["severity"] == "critical"
    assert "+14155550123" in ticket[0]["detail"] and "2 Chicken Taco" in ticket[0]["detail"]
