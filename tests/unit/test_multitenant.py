"""Tests for multi-tenancy: crypto, tenant store, and the tenant portal.

No test touches the network or a real vendor account. conftest keeps the
DB and tenant master key hermetic.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from voiceorder.api.storage import InMemoryOrderStore
from voiceorder.portal.portal import PortalDeps, build_portal_router
from voiceorder.tenants import crypto
from voiceorder.tenants.store import TenantStore, normalize_number


@pytest.fixture()
def store(tmp_path):
    return TenantStore(tmp_path / "tenants.db")


# --- crypto -----------------------------------------------------------------
def test_secret_roundtrip():
    token = crypto.encrypt_secret("sq0atp-super-secret")
    assert token != "sq0atp-super-secret"
    assert crypto.decrypt_secret(token) == "sq0atp-super-secret"


def test_decrypt_garbage_raises():
    with pytest.raises(ValueError):
        crypto.decrypt_secret("not-a-token")


def test_password_roundtrip():
    h = crypto.hash_password("correct-horse-123")
    assert crypto.verify_password("correct-horse-123", h)
    assert not crypto.verify_password("wrong", h)
    assert not crypto.verify_password("correct-horse-123", "bogus")


def test_cookie_roundtrip_and_tamper():
    c = crypto.make_session_cookie({"uid": "abc", "tid": "def"})
    body = crypto.read_session_cookie(c)
    assert body["uid"] == "abc" and body["tid"] == "def"
    assert crypto.read_session_cookie(c + "x") is None
    assert crypto.read_session_cookie("garbage") is None


# --- tenant store ------------------------------------------------------------
def test_tenant_lifecycle(store):
    t = store.create_tenant("Taco Palace", "+1 (555) 123-4567")
    assert t.phone_number == "5551234567"
    assert store.get_tenant(t.id).name == "Taco Palace"
    assert store.get_tenant_by_number("+15551234567").id == t.id
    assert store.get_tenant_by_number("(555) 123-4567").id == t.id
    assert store.get_tenant_by_number("+19998887777") is None
    assert store.get_tenant_by_name("taco palace").id == t.id


def test_settings_roundtrip(store):
    t = store.create_tenant("Taco Palace")
    store.set_settings(t.id, {"pos_profile": "square", "pickup_minutes": "25"})
    assert store.get_tenant(t.id).setting("pos_profile") == "square"
    assert store.get_tenant(t.id).setting("pickup_minutes") == "25"


def test_secrets_encrypted_at_rest(store, tmp_path):
    t = store.create_tenant("Taco Palace")
    store.set_secret(t.id, "square", {"access_token": "sq0atp-SECRET123",
                                      "location_id": "LP999"})
    # raw DB row must not contain the plaintext token
    conn = sqlite3.connect(str(tmp_path / "tenants.db"))
    blob = conn.execute(
        "SELECT blob FROM tenant_secrets WHERE tenant_id = ?", (t.id,)
    ).fetchone()[0]
    assert "sq0atp-SECRET123" not in blob
    # but the app can read it back
    assert store.get_secret(t.id, "square")["access_token"] == "sq0atp-SECRET123"
    assert store.has_secret(t.id, "square")
    assert not store.has_secret(t.id, "toast")
    store.clear_secret(t.id, "square")
    assert not store.has_secret(t.id, "square")


def test_users(store):
    t = store.create_tenant("Taco Palace")
    u = store.create_user(t.id, "Owner@Example.com", "s3cur3pass")
    assert u.email == "owner@example.com"  # normalized
    assert store.verify_user("owner@example.com", "s3cur3pass").id == u.id
    assert store.verify_user("owner@example.com", "nope") is None
    assert store.count_users(t.id) == 1
    with pytest.raises(ValueError):
        store.create_user(t.id, "owner@example.com", "anotherpass")
    with pytest.raises(ValueError):
        store.create_user(t.id, "new@example.com", "short")


def test_transcript_isolation(store):
    a = store.create_tenant("A")
    b = store.create_tenant("B")
    store.start_call("CA111", a.id, "+15550001111", "+15552222222")
    store.append_turn("CA111", "one burrito", "Got it.", [{"name": "add_item", "ok": True}])
    # tenant A sees it
    t = store.get_transcript(a.id, "CA111")
    assert t["turns"][0]["heard"] == "one burrito"
    # tenant B does NOT
    assert store.get_transcript(b.id, "CA111") is None
    assert store.list_calls(b.id) == []
    assert len(store.list_calls(a.id)) == 1
    store.end_call("CA111", "completed")
    assert store.get_transcript(a.id, "CA111")["status"] == "completed"


def test_backfill_orders(store, tmp_path):
    t = store.create_tenant("Taco Palace")
    conn = sqlite3.connect(str(tmp_path / "tenants.db"))
    conn.execute("CREATE TABLE orders (order_id TEXT PRIMARY KEY, restaurant_id TEXT,"
                 " payload TEXT, saved_at REAL, tenant_id TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO orders VALUES ('o1','r','{}',1.0,'')")
    conn.commit()
    assert store.backfill_orders_tenant(t.id) == 1


# --- portal ------------------------------------------------------------------
class _FakeAdapter:
    def ping(self):
        return {"ok": True, "location_name": "Test Location"}


def _portal_client(tmp_path):
    store = TenantStore(tmp_path / "portal.db")
    deps = PortalDeps(
        tenants=store,
        order_store=InMemoryOrderStore(),
        build_adapter=lambda tenant, override: _FakeAdapter(),
        get_catalog=lambda tenant: None,
    )
    app = FastAPI()
    app.include_router(build_portal_router(deps))
    return TestClient(app, follow_redirects=False), store


def _signup(client, **kw):
    args = {"restaurant": "Taco Palace", "phone": "+15551234567",
            "email": "owner@example.com", "password": "s3cur3pass"}
    args.update(kw)
    return client.post("/portal/signup", data=args)


def test_portal_requires_login_simple(tmp_path):
    client, _ = _portal_client(tmp_path)
    r = client.get("/portal/")
    assert r.status_code == 302 and r.headers["location"] == "/portal/login"
    r = client.get("/portal/api/calls")
    assert r.status_code == 401


def test_signup_and_login_flow(tmp_path):
    client, store = _portal_client(tmp_path)
    r = _signup(client)
    assert r.status_code == 302 and r.headers["location"] == "/portal/pos"
    assert "vo_portal" in r.cookies
    # dashboard loads for the new tenant
    r = client.get("/portal/")
    assert r.status_code == 200 and "Taco Palace" in r.text
    # bad password rejected
    client2, _ = _portal_client(tmp_path)
    r = client2.post("/portal/login",
                     data={"email": "owner@example.com", "password": "wrong"})
    assert r.status_code == 401


def test_signup_claims_existing_tenant(tmp_path):
    client, store = _portal_client(tmp_path)
    seeded = store.create_tenant("Seeded Diner", "+15557654321")
    r = _signup(client, restaurant="Whatever", phone="+15557654321",
                email="boss@diner.com")
    assert r.status_code == 302
    user = store.verify_user("boss@diner.com", "s3cur3pass")
    assert user.tenant_id == seeded.id  # joined, not duplicated


def test_pos_save_and_test(tmp_path):
    client, store = _portal_client(tmp_path)
    _signup(client)
    # test before save works (override creds)
    r = client.post("/portal/api/pos/test", json={
        "provider": "square",
        "values": {"access_token": "x", "location_id": "y", "environment": "sandbox"}})
    assert r.json()["ok"] is True
    # save
    r = client.post("/portal/api/pos", json={
        "provider": "square",
        "values": {"access_token": "sq0atp-SECRET", "location_id": "LP1",
                   "environment": "production"}})
    assert r.json()["ok"] is True
    # view masks secrets
    r = client.get("/portal/api/pos")
    body = r.json()
    assert body["provider"] == "square" and body["has_secret"] is True
    assert body["values"]["access_token"] == "saved"
    assert body["values"]["location_id"] == "LP1"
    # blank secret on re-save keeps the stored one
    r = client.post("/portal/api/pos", json={
        "provider": "square",
        "values": {"access_token": "", "location_id": "LP2", "environment": "production"}})
    assert r.json()["ok"] is True
    tenant = store.get_tenant_by_name("Taco Palace")
    assert store.get_secret(tenant.id, "square")["access_token"] == "sq0atp-SECRET"
    # missing required field rejected
    r = client.post("/portal/api/pos", json={
        "provider": "toast", "values": {"client_id": "only-this"}})
    assert r.status_code == 400


def test_calls_isolated_between_tenants(tmp_path):
    client_a, store = _portal_client(tmp_path)
    _signup(client_a, phone="+15551111111", email="a@x.com")
    client_b, _ = _portal_client(tmp_path)
    # NOTE: _portal_client creates a fresh store per tmp dir; share one:
    # (rebuild client_b against store A)
    from fastapi import FastAPI as _F
    deps_b = PortalDeps(tenants=store, order_store=InMemoryOrderStore(),
                        build_adapter=lambda t, o: _FakeAdapter(),
                        get_catalog=lambda t: None)
    app_b = _F()
    app_b.include_router(build_portal_router(deps_b))
    client_b = TestClient(app_b, follow_redirects=False)
    _signup(client_b, phone="+15552222222", email="b@x.com")

    ta = store.get_tenant_by_number("+15551111111")
    store.start_call("CA999", ta.id, "+15550001111", "+15551111111")
    store.append_turn("CA999", "hello", "hi there", [])

    assert len(client_a.get("/portal/api/calls").json()["calls"]) == 1
    assert client_b.get("/portal/api/calls").json()["calls"] == []
    assert client_a.get("/portal/api/calls/CA999").status_code == 200
    assert client_b.get("/portal/api/calls/CA999").status_code == 404


def test_settings_save(tmp_path):
    client, store = _portal_client(tmp_path)
    _signup(client)
    r = client.post("/portal/api/settings", json={
        "restaurant_name": "Taco Palace 2", "pickup_minutes": "30",
        "tax_rate": "0.07", "transfer_number": "+15550009999"})
    assert r.json()["ok"] is True
    tenant = store.get_tenant_by_name("Taco Palace")
    assert tenant.setting("restaurant_name") == "Taco Palace 2"
    assert tenant.setting("pickup_minutes") == "30"


# --- main wiring --------------------------------------------------------------
def test_pos_mask_keeps_stored_secret(tmp_path):
    """Submitting the "saved" mask must not overwrite or test with the
    literal mask word -- stored credentials are used instead."""
    seen = {}

    def build(tenant, override):
        seen.update(override)
        return _FakeAdapter()

    store = TenantStore(tmp_path / "portal.db")
    deps = PortalDeps(
        tenants=store,
        order_store=InMemoryOrderStore(),
        build_adapter=build,
        get_catalog=lambda tenant: None,
    )
    app = FastAPI()
    app.include_router(build_portal_router(deps))
    client = TestClient(app, follow_redirects=False)
    _signup(client)

    # store a real secret first
    client.post("/portal/api/pos", json={
        "provider": "square",
        "values": {"access_token": "sq0atp-REAL", "location_id": "LP1",
                   "environment": "production"}})

    # 1. test connection with the mask left in the token field
    r = client.post("/portal/api/pos/test", json={
        "provider": "square",
        "values": {"access_token": "saved", "location_id": "LP1",
                   "environment": "production"}})
    assert r.json()["ok"] is True
    assert seen["square"]["access_token"] == "sq0atp-REAL"

    # 2. save with the mask left in the token field must not clobber it
    r = client.post("/portal/api/pos", json={
        "provider": "square",
        "values": {"access_token": "saved", "location_id": "LP2",
                   "environment": "production"}})
    assert r.json()["ok"] is True
    tenant = store.get_tenant_by_name("Taco Palace")
    assert store.get_secret(tenant.id, "square")["access_token"] == "sq0atp-REAL"
    assert store.get_secret(tenant.id, "square")["location_id"] == "LP2"


def test_tenant_voice_defaults_and_override():
    from voiceorder.api import main as api_main
    from voiceorder.voice import voices as voice_catalog
    t = api_main.default_tenant
    dflt = (voice_catalog.DEFAULT_VOICE_ID, voice_catalog.DEFAULT_MODEL_ID)
    assert api_main._tenant_voice(t.id) == dflt
    assert api_main._tenant_voice("") == dflt
    assert api_main._tenant_voice("no-such-tenant") == dflt
    api_main.tenant_store.set_settings(t.id, {"voice_id": "TX3LPaxmHKxFdv7VOQHJ",
                                              "voice_model": "eleven_turbo_v2_5"})
    try:
        assert api_main._tenant_voice(t.id) == ("TX3LPaxmHKxFdv7VOQHJ", "eleven_turbo_v2_5")
    finally:
        api_main.tenant_store.set_settings(t.id, {"voice_id": "", "voice_model": ""})
    assert api_main._tenant_voice(t.id) == dflt


def test_speak_to_url_uses_tenant_voice(monkeypatch):
    from voiceorder.api import main as api_main
    calls = {}

    def fake_synth(text, *, voice_id, model_id, use_cache=True):
        calls["voice_id"] = voice_id
        calls["model_id"] = model_id
        return b"mp3"

    monkeypatch.setattr(api_main.tts, "synthesize", fake_synth)
    monkeypatch.setattr(api_main.tts, "cache_key", lambda text, v, m: "k123")
    monkeypatch.setattr(api_main, "_public_base_url", lambda: "https://example.test")
    t = api_main.default_tenant
    api_main.tenant_store.set_settings(t.id, {"voice_id": "TX3LPaxmHKxFdv7VOQHJ",
                                              "voice_model": "eleven_turbo_v2_5"})
    try:
        url = api_main._speak_to_url("hello there", t.id)
        assert calls == {"voice_id": "TX3LPaxmHKxFdv7VOQHJ", "model_id": "eleven_turbo_v2_5"}
        assert url == "https://example.test/voice/audio/k123.mp3"
        api_main._speak_to_url("hi", "no-such-tenant")
        assert calls["voice_id"] == api_main.tts.DEFAULT_VOICE
        assert calls["model_id"] == api_main.tts.DEFAULT_MODEL
    finally:
        api_main.tenant_store.set_settings(t.id, {"voice_id": "", "voice_model": ""})


def test_portal_voice_preview_and_validation(tmp_path, monkeypatch):
    import voiceorder.portal.portal as portal_mod
    portal_mod._preview_buckets.clear()
    client, store = _portal_client(tmp_path)
    _signup(client)
    tenant = store.get_tenant_by_name("Taco Palace")

    synth_calls = {}

    def fake_synth(text, *, voice_id, model_id, use_cache=True):
        synth_calls["voice_id"] = voice_id
        synth_calls["model_id"] = model_id
        assert "Taco Palace" in text
        return b"FAKE-MP3"

    monkeypatch.setattr(portal_mod.tts, "synthesize", fake_synth)

    r = client.get("/portal/settings")
    assert r.status_code == 200 and "AI voice" in r.text and "Sarah" in r.text

    r = client.post("/portal/api/voice/preview",
                    json={"voice_id": "TX3LPaxmHKxFdv7VOQHJ",
                          "model_id": "eleven_turbo_v2_5"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"FAKE-MP3"
    assert synth_calls == {"voice_id": "TX3LPaxmHKxFdv7VOQHJ",
                           "model_id": "eleven_turbo_v2_5"}

    # preview falls back to saved tenant voice when not specified
    store.set_settings(tenant.id, {"voice_id": "cgSgspJ2msm6clMCkdW9"})
    r = client.post("/portal/api/voice/preview", json={})
    assert r.status_code == 200
    assert synth_calls["voice_id"] == "cgSgspJ2msm6clMCkdW9"
    assert synth_calls["model_id"] == "eleven_flash_v2_5"
    store.set_settings(tenant.id, {"voice_id": ""})

    r = client.post("/portal/api/voice/preview",
                    json={"voice_id": "TX3LPaxmHKxFdv7VOQHJ",
                          "model_id": "eleven_multilingual_v2"})
    assert r.status_code == 400
    r = client.post("/portal/api/voice/preview",
                    json={"voice_id": "!!!", "model_id": "eleven_flash_v2_5"})
    assert r.status_code == 400

    # settings save validates the model
    r = client.post("/portal/api/settings", json={"voice_model": "eleven_multilingual_v2"})
    assert r.status_code == 400
    # and accepts a good voice + model
    r = client.post("/portal/api/settings",
                    json={"voice_id": "TX3LPaxmHKxFdv7VOQHJ",
                          "voice_model": "eleven_turbo_v2_5"})
    assert r.json()["ok"] is True
    assert store.get_tenant(tenant.id).setting("voice_id") == "TX3LPaxmHKxFdv7VOQHJ"
    assert store.get_tenant(tenant.id).setting("voice_model") == "eleven_turbo_v2_5"
    # a pasted custom voice ID wins over the dropdown
    r = client.post("/portal/api/settings",
                    json={"voice_id": "EXAVITQu4vr4xnSDxMaL",
                          "voice_id_custom": "customVoice12345"})
    assert r.json()["ok"] is True
    assert store.get_tenant(tenant.id).setting("voice_id") == "customVoice12345"


def test_main_tenant_wiring():
    from voiceorder.api import main as api_main
    t = api_main.default_tenant
    assert t is not None and t.id
    ctx = api_main.tenant_context(t)
    assert ctx.tenant_id == t.id
    assert ctx.restaurant_name
    # unknown numbers fall back to the default tenant
    assert api_main.resolve_call_tenant("+19998887777").id == t.id
    # the default tenant resolves by its own number when set
    if t.phone_number:
        assert api_main.resolve_call_tenant("+" + t.phone_number).id == t.id
