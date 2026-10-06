"""Tests for the Phase 5a Twilio Gather loop: adapter + webhooks.

No test touches the network: signature math is local, TTS is stubbed, and the
SMS path is skipped without TWILIO_* configured.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from voiceorder.api import main as api_main
from voiceorder.core.cart import CartState
from voiceorder.voice_adapters import twilio as twilio_adapter


# --- signature validation ---------------------------------------------------

TOKEN = "test_auth_token_123"


def _sign(url: str, params: dict[str, str]) -> str:
    import base64
    import hashlib
    import hmac

    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def test_validate_signature_round_trip():
    url = "https://example.test/twilio/voice"
    params = {"CallSid": "CA123", "From": "+15551234567"}
    sig = _sign(url, params)
    assert twilio_adapter.validate_signature(url, params, sig, TOKEN)


def test_validate_signature_rejects_tampering():
    url = "https://example.test/twilio/voice"
    params = {"CallSid": "CA123", "From": "+15551234567"}
    sig = _sign(url, params)
    assert not twilio_adapter.validate_signature(
        url, {**params, "From": "+19999999999"}, sig, TOKEN
    )
    assert not twilio_adapter.validate_signature(url, params, sig, "wrong-token")
    assert not twilio_adapter.validate_signature(url, params, "bogus", TOKEN)


# --- TwiML builders ----------------------------------------------------------

def test_answer_call_greets_and_gathers():
    xml = twilio_adapter.answer_call(
        "https://example.test/voice/audio/abc.mp3",
        "Thanks for calling Taqueria Demo!",
        "https://example.test/twilio/gather",
    )
    assert "<Play>https://example.test/voice/audio/abc.mp3</Play>" in xml
    assert '<Gather input="speech"' in xml
    assert 'action="https://example.test/twilio/gather"' in xml


def test_answer_call_falls_back_to_say_without_audio():
    # TTS failed: the caller still hears the greeting via <Say>, never silence.
    xml = twilio_adapter.answer_call(
        None,
        "Thanks for calling Taqueria Demo!",
        "https://example.test/twilio/gather",
    )
    assert "<Play>" not in xml
    assert "<Say>Thanks for calling Taqueria Demo!</Say>" in xml
    assert '<Gather input="speech"' in xml


def test_continue_call_prefers_play_falls_back_to_say():
    xml = twilio_adapter.continue_call("https://x/y.mp3", "hello", "https://x/gather")
    assert "<Play>https://x/y.mp3</Play>" in xml
    assert "<Say>hello</Say>" not in xml  # reply is Play, not Say
    xml = twilio_adapter.continue_call(None, "fish & chips", "https://x/gather")
    assert "<Say>fish &amp; chips</Say>" in xml
    assert "<Gather" in xml


def test_end_call_hangs_up():
    xml = twilio_adapter.end_call("https://x/y.mp3", "bye")
    assert "<Play>https://x/y.mp3</Play>" in xml
    assert "<Hangup />" in xml


def test_transfer_call_dials():
    xml = twilio_adapter.transfer_call(None, "transferring you", "+15550134200")
    assert "<Dial>+15550134200</Dial>" in xml


def test_unavailable_says_so():
    xml = twilio_adapter.unavailable()
    assert "<Say>" in xml and "<Hangup />" in xml


# --- webhooks -----------------------------------------------------------------

class _FakeCart:
    def __init__(self, state=CartState.BUILDING, transferred=False):
        self.state = state
        self.transferred = transferred
        self.cart_id = "cart_fake"
        self.last_order = {}


class _FakeSession:
    def __init__(self, reply: str, cart: _FakeCart):
        self._reply = reply
        self.cart = cart
        self.turns: list[str] = []
        self.ctx = api_main.restaurant_context()

    def handle_caller_message(self, text: str) -> dict:
        self.turns.append(text)
        return {"text": self._reply}


@pytest.fixture()
def client():
    return TestClient(api_main.app)


@pytest.fixture()
def public_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.setattr(
        "voiceorder.voice.tts.synthesize", lambda text, **kw: b"fake-mp3"
    )
    return "https://example.test"


def _voice_form(call_sid="CA123", **extra):
    return {"CallSid": call_sid, "From": "+15551234567", "To": "+15557654321", **extra}


def test_twilio_voice_answers_and_gathers(client, public_url, monkeypatch):
    cart = _FakeCart()
    monkeypatch.setattr(
        api_main, "_new_twilio_session", lambda tenant=None: _FakeSession("hi", cart)
    )
    resp = client.post("/twilio/voice", data=_voice_form())
    assert resp.status_code == 200
    assert "application/xml" in resp.headers["content-type"]
    assert "<Gather" in resp.text
    assert "/voice/audio/" in resp.text
    assert "CA123" in api_main._twilio_sessions


def test_twilio_voice_unavailable_without_llm(client, public_url, monkeypatch):
    monkeypatch.setattr(api_main, "_new_twilio_session", lambda tenant=None: None)
    resp = client.post("/twilio/voice", data=_voice_form())
    assert resp.status_code == 200
    assert "unavailable" in resp.text
    assert "<Hangup />" in resp.text


def _drain_turn(client, gather_resp, call_sid, from_number="+15551234567"):
    """Follow the gather -> turn poll loop until the final TwiML arrives.

    /twilio/gather answers instantly with a filler + <Redirect>; the agent
    turn runs on a background thread and /twilio/turn is polled for it.
    """
    import re

    assert gather_resp.status_code == 200
    m = re.search(r'<Redirect method="POST">([^<]+)</Redirect>', gather_resp.text)
    assert m, f"no turn redirect in: {gather_resp.text[:200]}"
    poll_url = m.group(1)
    import time as _time

    deadline = _time.monotonic() + 15  # like Twilio: wait out the <Pause>s
    while _time.monotonic() < deadline:
        # Twilio re-POSTs its standard fields on every redirect.
        resp = client.post(
            poll_url,
            data={"CallSid": call_sid, "From": from_number},
        )
        assert resp.status_code == 200
        if "<Pause" not in resp.text:
            return resp
        m = re.search(r'<Redirect method="POST">([^<]+)</Redirect>', resp.text)
        poll_url = m.group(1)
        _time.sleep(0.2)
    raise AssertionError("turn never completed")


def test_twilio_gather_runs_agent_turn(client, public_url, monkeypatch):
    cart = _FakeCart()
    session = _FakeSession("two tacos coming up", cart)
    api_main._twilio_sessions["CA123"] = session
    try:
        # Gather answers instantly (filler), well inside Twilio's timeout.
        resp = client.post(
            "/twilio/gather", data=_voice_form(SpeechResult="two tacos please")
        )
        assert resp.status_code == 200
        assert "<Say>One moment.</Say>" in resp.text
        assert "/twilio/turn" in resp.text
        # The turn completes on the poll; the reply plays and we listen on.
        final = _drain_turn(client, resp, "CA123")
        assert "<Play>" in final.text and "<Gather" in final.text
        assert session.turns == ["two tacos please"]
    finally:
        api_main._twilio_sessions.pop("CA123", None)


def test_twilio_gather_reprompts_on_empty_speech(client, public_url):
    api_main._twilio_sessions["CA999"] = _FakeSession("hi", _FakeCart())
    try:
        resp = client.post("/twilio/gather", data=_voice_form("CA999"))
        assert resp.status_code == 200
        # re-prompt renders through TTS (stubbed) -> <Play>, then keeps listening
        assert "<Play>" in resp.text and "<Gather" in resp.text
    finally:
        api_main._twilio_sessions.pop("CA999", None)


def test_twilio_gather_unknown_call(client, public_url):
    resp = client.post("/twilio/gather", data=_voice_form("CA_NOPE", SpeechResult="hi"))
    assert resp.status_code == 200
    assert "<Hangup />" in resp.text


def test_twilio_gather_submitted_order_hangs_up(client, public_url):
    cart = _FakeCart(state=CartState.SUBMITTED)
    cart.last_order = {
        "order_number": "1042",
        "totals": {"total": 25.98},
        "pickup_time": "in 20 minutes",
    }
    api_main._twilio_sessions["CA124"] = _FakeSession("confirmed", cart)
    resp = client.post("/twilio/gather", data=_voice_form("CA124", SpeechResult="yes"))
    final = _drain_turn(client, resp, "CA124")
    assert final.status_code == 200
    assert "<Hangup />" in final.text
    assert "CA124" not in api_main._twilio_sessions
    # The phone order must land in the order store, not just the lost cart.
    recent = api_main.order_store.list_recent("taqueria-demo", limit=5)
    assert any(o.get("call_id") == "CA124" for o in recent)


def test_twilio_gather_transfer_dials(client, public_url):
    cart = _FakeCart(transferred=True)
    api_main._twilio_sessions["CA125"] = _FakeSession("transferring", cart)
    resp = client.post("/twilio/gather", data=_voice_form("CA125", SpeechResult="human"))
    final = _drain_turn(client, resp, "CA125")
    assert final.status_code == 200
    assert "<Dial>" in final.text
    assert "CA125" not in api_main._twilio_sessions


def test_twilio_gather_answers_fast_when_turn_is_slow(client, public_url):
    """Regression: a 30s turn must not 502 the gather webhook.

    The gather webhook must answer in milliseconds even when the agent turn
    takes longer than Twilio's ~15s timeout; the turn result arrives via
    the /twilio/turn poll loop.
    """
    import time as _time

    class _SlowSession(_FakeSession):
        def handle_caller_message(self, text: str) -> dict:
            _time.sleep(3)  # stand-in for a very slow LLM/POS round-trip
            return super().handle_caller_message(text)

    api_main._twilio_sessions["CA128"] = _SlowSession("slow reply", _FakeCart())
    try:
        start = _time.monotonic()
        resp = client.post(
            "/twilio/gather", data=_voice_form("CA128", SpeechResult="hello")
        )
        elapsed = _time.monotonic() - start
        assert resp.status_code == 200
        assert elapsed < 5, f"gather took {elapsed:.1f}s -- Twilio would time out"
        final = _drain_turn(client, resp, "CA128")
        assert "slow reply" not in final.text  # reply goes through TTS stub
        assert "<Play>" in final.text and "<Gather" in final.text
    finally:
        api_main._twilio_sessions.pop("CA128", None)


def test_twilio_status_cleans_up(client, public_url):
    api_main._twilio_sessions["CA126"] = _FakeSession("hi", _FakeCart())
    resp = client.post(
        "/twilio/status", data=_voice_form("CA126", CallStatus="completed")
    )
    assert resp.status_code == 200
    assert "CA126" not in api_main._twilio_sessions


def test_twilio_status_ignores_in_progress(client, public_url):
    api_main._twilio_sessions["CA127"] = _FakeSession("hi", _FakeCart())
    try:
        resp = client.post(
            "/twilio/status", data=_voice_form("CA127", CallStatus="in-progress")
        )
        assert resp.status_code == 200
        assert "CA127" in api_main._twilio_sessions
    finally:
        api_main._twilio_sessions.pop("CA127", None)


def test_twilio_signature_enforced_when_configured(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    form = _voice_form()
    # no signature -> 403
    resp = client.post("/twilio/voice", data=form)
    assert resp.status_code == 403
    # valid signature -> through (LLM unconfigured -> unavailable TwiML, not 403)
    monkeypatch.setattr(api_main, "_new_twilio_session", lambda tenant=None: None)
    sig = _sign("https://example.test/twilio/voice", form)
    resp = client.post(
        "/twilio/voice", data=form, headers={"X-Twilio-Signature": sig}
    )
    assert resp.status_code == 200


def test_voice_audio_serves_cached_clip(client, tmp_path, monkeypatch):
    monkeypatch.setattr("voiceorder.voice.tts._CACHE_DIR", tmp_path)
    key = "a" * 64
    (tmp_path / f"{key}.mp3").write_bytes(b"clip-bytes")
    resp = client.get(f"/voice/audio/{key}.mp3")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == b"clip-bytes"


def test_voice_audio_rejects_bad_keys(client):
    assert client.get("/voice/audio/../../etc/passwd.mp3").status_code == 404
    assert client.get("/voice/audio/" + "b" * 64 + ".mp3").status_code == 404
