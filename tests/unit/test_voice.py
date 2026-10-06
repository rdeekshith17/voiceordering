"""Tests for the Phase 3 voice layer: TTS module + POST /voice/speak.

The network is always stubbed: no test may spend ElevenLabs characters.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from voiceorder.api import main as api_main
from voiceorder.voice import tts
from voiceorder.voice.tts import TtsError


@pytest.fixture(autouse=True)
def _no_voice_secret():
    # These tests exercise TTS logic, not auth: the repo .env sets
    # VOICE_SECRET for the server, which would otherwise 401 every call.
    api_main.settings.voice_secret = ""
    api_main.settings.voice_secret_previous = ""


@pytest.fixture()
def tts_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "_CACHE_DIR", tmp_path / "tts_cache")
    return tmp_path / "tts_cache"


def test_synthesize_rejects_empty(tts_cache):
    with pytest.raises(ValueError, match="empty"):
        tts.synthesize("   ")


def test_synthesize_rejects_too_long(tts_cache):
    with pytest.raises(ValueError, match="too long"):
        tts.synthesize("x" * (tts.MAX_CHARS + 1))


def test_synthesize_caches_and_reuses(tts_cache, monkeypatch):
    calls = []

    def fake_post(text, voice_id, model_id):
        calls.append(text)
        return b"fake-mp3-bytes"

    monkeypatch.setattr(tts, "_post_tts", fake_post)
    first = tts.synthesize("hello there")
    second = tts.synthesize("hello there")
    assert first == second == b"fake-mp3-bytes"
    assert calls == ["hello there"], "second call must be served from cache"
    assert list(tts_cache.glob("*.mp3")) != []


def test_synthesize_cache_key_varies_by_voice(tts_cache, monkeypatch):
    monkeypatch.setattr(tts, "_post_tts", lambda t, v, m: f"audio:{v}".encode())
    a = tts.synthesize("hi", voice_id="voice-a")
    b = tts.synthesize("hi", voice_id="voice-b")
    assert a != b


def test_synthesize_network_error_becomes_tts_error(tts_cache, monkeypatch):
    def boom(text, voice_id, model_id):
        raise TtsError("ElevenLabs TTS failed: HTTP 401")

    monkeypatch.setattr(tts, "_post_tts", boom)
    with pytest.raises(TtsError):
        tts.synthesize("hello")


@pytest.fixture()
def client():
    return TestClient(api_main.app)


def test_voice_speak_endpoint(client, monkeypatch):
    monkeypatch.setattr(
        "voiceorder.voice.tts.synthesize", lambda text, **kw: b"mp3-bytes"
    )
    resp = client.post("/voice/speak", json={"text": "thanks for calling"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == b"mp3-bytes"


def test_voice_speak_reports_cache_hit(client, monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_CACHE_DIR", tmp_path)
    path = tts.cache_path("cached greeting", tts.DEFAULT_VOICE, tts.DEFAULT_MODEL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"cached-mp3")
    resp = client.post("/voice/speak", json={"text": "cached greeting"})
    assert resp.status_code == 200
    assert resp.headers["X-TTS-Cache"] == "HIT"
    assert resp.content == b"cached-mp3"


def test_voice_speak_rejects_empty(client):
    resp = client.post("/voice/speak", json={"text": "   "})
    assert resp.status_code == 400


def test_voice_speak_rejects_too_long(client):
    resp = client.post("/voice/speak", json={"text": "x" * (tts.MAX_CHARS + 1)})
    assert resp.status_code == 400


def test_voice_speak_maps_tts_error_to_502(client, monkeypatch):
    def boom(text, **kw):
        raise TtsError("ElevenLabs TTS failed: HTTP 401")

    monkeypatch.setattr("voiceorder.voice.tts.synthesize", boom)
    resp = client.post("/voice/speak", json={"text": "hello"})
    assert resp.status_code == 502


def test_voice_speak_disabled_by_env(client, monkeypatch):
    monkeypatch.setenv("TTS_ENABLED", "0")
    resp = client.post("/voice/speak", json={"text": "hello"})
    assert resp.status_code == 503


def test_chat_page_has_voice_toggle(client):
    resp = client.get("/chat")
    assert resp.status_code == 200
    assert "/voice/speak" in resp.text
    assert "toggleVoice" in resp.text


def test_health_reports_tts_flag(client, monkeypatch):
    monkeypatch.setenv("TTS_ENABLED", "1")
    assert client.get("/health").json()["tts_enabled"] is True
    monkeypatch.setenv("TTS_ENABLED", "0")
    assert client.get("/health").json()["tts_enabled"] is False
    monkeypatch.delenv("TTS_ENABLED", raising=False)
    assert "TTS_ENABLED" not in os.environ
