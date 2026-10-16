"""Safer defaults, strict mode and the admin security checks."""
from __future__ import annotations

from fastapi.testclient import TestClient

from voiceorder import security
from voiceorder.api import main as api_main
from tests.unit.test_super_admin import admin_client, env  # noqa: F401

FORM = {"CallSid": "CAstrict", "From": "+15551234567", "To": "+15557654321"}


def test_demo_and_signup_are_off_unless_set_to_1(monkeypatch):
    for value, on in ((None, False), ("", False), ("0", False), ("yes", False), ("1", True)):
        for name, fn in (("CHAT_DEMO_ENABLED", security.chat_demo_enabled),
                         ("PORTAL_SIGNUP_ENABLED", security.signup_enabled)):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
            assert fn() is on, (name, value)


def test_chat_demo_closed_by_default(monkeypatch):
    monkeypatch.delenv("CHAT_DEMO_ENABLED", raising=False)
    client = TestClient(api_main.app)
    assert client.get("/chat").status_code == 404
    assert client.post("/chat/start").status_code == 404


def test_strict_mode_rejects_unsigned_twilio_webhooks(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setenv("SECURITY_STRICT", "1")
    client = TestClient(api_main.app)
    for path in ("/twilio/voice", "/twilio/gather", "/twilio/status"):
        assert client.post(path, data=FORM).status_code == 403, path


def test_strict_mode_rejects_when_voice_secret_missing(monkeypatch):
    monkeypatch.setattr(api_main.settings, "voice_secret", "")
    monkeypatch.setattr(api_main.settings, "voice_secret_previous", "")
    monkeypatch.setenv("SECURITY_STRICT", "1")
    client = TestClient(api_main.app)
    assert client.get("/orders/recent").status_code == 503
    monkeypatch.delenv("SECURITY_STRICT")
    assert client.get("/orders/recent").status_code == 200


def test_checks_flag_missing_secrets(monkeypatch):
    for name in ("TWILIO_AUTH_TOKEN", "PUBLIC_BASE_URL", "VOICE_SECRET", "SECURITY_STRICT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHAT_DEMO_ENABLED", "1")
    bad = {c.key: c for c in security.problems()}
    assert bad["twilio_auth_token"].severity == "critical"
    assert {"public_base_url", "voice_secret", "chat_demo", "strict"} <= set(bad)
    assert security.run_checks()[0].severity == "critical"  # worst problems first
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://not-https.test")
    assert "public_base_url" in {c.key for c in security.problems()}


def test_admin_overview_and_api_show_security_checks(env, monkeypatch):  # noqa: F811
    app, store, _ = env
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    store.audit("system", "twilio", None, "webhook.signature_failed", "/twilio/voice")
    c = admin_client(app)
    page = c.get("/admin/").text
    assert 'data-testid="security-card"' in page and "Twilio call signature check" in page
    j = c.get("/admin/api/security").json()
    assert j["ok"] is False and j["rejected_webhooks_24h"] == 1
    assert any(x["key"] == "twilio_auth_token" and not x["ok"] for x in j["checks"])
    assert TestClient(app).get("/admin/api/security").status_code in (401, 403)
