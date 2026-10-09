"""Phase 4 security pass: no card data anywhere, auth on owner endpoints,
secret rotation on the voice API."""
import json
import re
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from voiceorder.api import main as api_main
from voiceorder.api.owner import OwnerDeps, build_owner_router
from voiceorder.api.storage import InMemoryCartStore, InMemoryOrderStore
from voiceorder.core import tools
from voiceorder.core.cart import Cart
from voiceorder.core.catalog import Catalog
from voiceorder.core.ports import RestaurantContext
from voiceorder.pos_adapters.fake import FakePos

FIXTURE = Path(__file__).resolve().parents[2] / "voiceorder" / "fixtures" / "menu_taqueria.json"
CARD_RE = re.compile(r"\d{13,19}")  # card numbers are 13-19 contiguous digits


def _env():
    catalog = Catalog.from_json(FIXTURE)
    ctx = RestaurantContext(
        restaurant_id="taqueria-demo",
        restaurant_name="Taqueria Demo",
        pos_profile="square_like",
        voice_platform="text",
        transfer_number="+15550134200",
    )
    return catalog, ctx, FakePos(profile="square_like", catalog=catalog)


def test_no_card_numbers_in_any_tool_output():
    """Full order flow serialized: nothing shaped like a card number may appear."""
    catalog, ctx, pos = _env()
    cart = Cart(cart_id="s1", restaurant_id=ctx.restaurant_id,
                idempotency_key=uuid.uuid4().hex)
    blobs: list[str] = []
    calls = [
        ("search_menu", {"query": "burrito"}),
        ("add_item", {"item_ref": "B1", "quantity": 1, "modifier_ids": ["burrito_rice_beans"]}),
        ("add_item", {"item_ref": "D3", "quantity": 1, "variation_id": "large"}),
        ("get_cart", {}),
        ("submit_order", {"customer_name": "T", "customer_phone": "555-123-4567",
                          "confirmed": True, "idempotency_key": cart.idempotency_key}),
    ]
    for name, args in calls:
        result = tools.dispatch(name, cart=cart, catalog=catalog, pos=pos, ctx=ctx,
                                arguments=args)
        blobs.append(result.message)
        blobs.append(json.dumps(result.data, default=str))
    haystack = "\n".join(blobs)
    assert not CARD_RE.search(haystack), f"card-like digits leaked: {CARD_RE.search(haystack).group(0)}"


def _owner_client(secret: str) -> TestClient:
    catalog, ctx, pos = _env()
    app = FastAPI()
    app.include_router(
        build_owner_router(
            OwnerDeps(
                catalog=catalog, pos=pos,
                cart_store=InMemoryCartStore(), order_store=InMemoryOrderStore(),
                ctx=ctx, owner_secret=secret,
            )
        )
    )
    return TestClient(app, follow_redirects=False)


def test_owner_endpoints_reject_missing_secret():
    client = _owner_client("s3cret")
    assert client.get("/owner").headers["location"] == "/portal/login"  # pages send you to log in
    assert client.get("/owner/menu").status_code == 401
    assert client.post("/owner/menu/availability",
                       json={"item_ref": "T1", "available": False}).status_code == 401


def test_owner_secret_only_in_a_header_never_in_the_url():
    client = _owner_client("s3cret")
    assert client.get("/owner", headers={"X-Owner-Secret": "s3cret"}).status_code == 200
    assert client.get("/owner/menu?key=s3cret").status_code == 401  # leaked into logs/history
    assert client.get("/owner/menu", headers={"X-Owner-Secret": "wrong"}).status_code == 401


def test_owner_pages_open_for_a_signed_in_restaurant_admin_only():
    from voiceorder.api import main as api_main
    store, t = api_main.tenant_store, api_main.default_tenant
    for email, role in (("legacy-admin@hh.test", "admin"), ("legacy-cook@hh.test", "kitchen")):
        if not store.verify_user(email, "password123"):
            store.create_user(t.id, email, "password123", role=role)
    admin = TestClient(api_main.app, follow_redirects=False)
    admin.post("/portal/login", data={"email": "legacy-admin@hh.test", "password": "password123"})
    page = admin.get("/owner")
    assert page.status_code == 200 and "?key=" not in page.text
    assert admin.get("/owner/orders").status_code == 200
    cook = TestClient(api_main.app, follow_redirects=False)
    cook.post("/portal/login", data={"email": "legacy-cook@hh.test", "password": "password123"})
    assert cook.get("/owner/orders").status_code == 401
    assert cook.get("/owner").headers["location"] == "/portal/login"


def test_owner_pages_escape_customer_and_menu_text():
    client = _owner_client("s3cret")
    html = client.get("/owner", headers={"X-Owner-Secret": "s3cret"}).text
    assert "esc(o.customer_name" in html and "esc(i.name)" in html and "onclick=\"toggleAvail" not in html
    backup = client.get("/owner/backup", headers={"X-Owner-Secret": "s3cret"}).text
    assert "esc(i.name)" in backup and "onclick=\"add(" not in backup


def test_owner_can_86_and_restore_and_backup_order():
    client = _owner_client("s3cret")
    headers = {"X-Owner-Secret": "s3cret"}
    menu = client.get("/owner/menu", headers=headers).json()
    assert len(menu["items"]) == 25
    assert client.post("/owner/menu/availability", headers=headers,
                       json={"item_ref": "T1", "available": False}).json()["available"] is False
    blocked = client.post("/owner/backup/submit", headers=headers, json={
        "lines": [{"item_ref": "T1", "quantity": 1}],
        "customer_name": "Staff", "customer_phone": "555-000-0000",
    })
    assert blocked.status_code == 400
    client.post("/owner/menu/availability", headers=headers,
                json={"item_ref": "T1", "available": True})
    fired = client.post("/owner/backup/submit", headers=headers, json={
        "lines": [{"item_ref": "T1", "quantity": 2}],
        "customer_name": "Walk-in", "customer_phone": "555-000-0001",
    })
    assert fired.status_code == 200
    order = fired.json()["order"]
    assert order["totals"]["total"] > 0
    orders = client.get("/owner/orders", headers=headers).json()["orders"]
    assert any(o["order_number"] == order["order_number"] for o in orders)


def test_owner_backup_rejects_empty_ticket():
    client = _owner_client("s3cret")
    response = client.post("/owner/backup/submit",
                           headers={"X-Owner-Secret": "s3cret"},
                           json={"lines": [], "customer_name": "X", "customer_phone": "1"})
    assert response.status_code == 400


def test_voice_secret_rotation_accepts_previous(monkeypatch):
    monkeypatch.setattr(api_main.settings, "voice_secret", "new-secret")
    monkeypatch.setattr(api_main.settings, "voice_secret_previous", "old-secret")
    api_main.require_secret("new-secret")  # no raise
    api_main.require_secret("old-secret")  # no raise
    try:
        api_main.require_secret("wrong")
    except Exception as exc:  # noqa: BLE001 -- asserting the 401 shape
        assert getattr(exc, "status_code", None) == 401
    else:
        raise AssertionError("expected HTTPException for a wrong secret")


# --- PR 6 hardening ------------------------------------------------------------------------
def _portal_app(tmp_path, signup=True):
    from voiceorder.portal import portal as portal_mod
    from voiceorder.portal.portal import PortalDeps, build_portal_router
    from voiceorder.tenants.store import TenantStore

    portal_mod._FAILED_LOGINS.clear()
    store = TenantStore(tmp_path / "s.db")
    app = FastAPI()
    app.include_router(build_portal_router(PortalDeps(
        tenants=store, order_store=InMemoryOrderStore(), build_adapter=lambda t, o: None,
        get_catalog=lambda t: None, signup_enabled=signup)))
    t = store.create_tenant("Hyderabad House")
    store.create_user(t.id, "owner@hh.test", "password123")
    return app, store


def test_every_response_carries_security_headers():
    from voiceorder.api import main as api_main
    client = TestClient(api_main.app)
    for path in ("/", "/portal/login", "/admin/login", "/health"):
        h = client.get(path).headers
        assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff", path
        assert "frame-ancestors 'none'" in h["content-security-policy"], path
        assert "strict-transport-security" not in h, path  # plain http in tests
    https = client.get("/portal/login", headers={"X-Forwarded-Proto": "https"}).headers
    assert https["strict-transport-security"] == "max-age=31536000"


def test_session_cookies_are_secure_behind_https(tmp_path):
    app, store = _portal_app(tmp_path)
    c = TestClient(app, follow_redirects=False)
    over_https = c.post("/portal/login", headers={"X-Forwarded-Proto": "https"},
                        data={"email": "owner@hh.test", "password": "password123"})
    cookie = over_https.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    plain = c.post("/portal/login", data={"email": "owner@hh.test", "password": "password123"})
    assert "secure" not in plain.headers["set-cookie"].lower()  # local http dev still works


def test_portal_login_locks_out_after_five_failures(tmp_path):
    app, store = _portal_app(tmp_path)
    c = TestClient(app, follow_redirects=False)
    for _ in range(5):
        assert c.post("/portal/login", data={"email": "owner@hh.test", "password": "nope"}).status_code == 401
    assert c.post("/portal/login", data={"email": "owner@hh.test", "password": "password123"}).status_code == 429
    assert c.post("/portal/login", data={"email": "owner@hh.test", "password": "x"}).status_code == 429
    locks = [e for e in store.list_audit(None, 50) if e["action"] == "portal.login_locked"]
    assert len(locks) == 1  # audited once, not on every attempt


def test_signup_can_be_switched_off(tmp_path):
    app, _ = _portal_app(tmp_path, signup=False)
    c = TestClient(app, follow_redirects=False)
    assert c.get("/portal/signup").status_code == 404
    assert c.post("/portal/signup", data={"restaurant": "X", "email": "x@x.test",
                                          "password": "password123"}).status_code == 404
    assert "Create your restaurant account" not in c.get("/portal/login").text


def test_logs_never_contain_full_phone_numbers(caplog):
    import logging
    from voiceorder.logsafe import PhoneRedactingFilter
    caplog.handler.addFilter(PhoneRedactingFilter())
    logging.getLogger("voiceorder").warning("twilio call %s from %s heard=%r", "CA0a16f1f9cca1777",
                                            "+12832298041", "call me at 415-555-0123")
    line = caplog.records[-1].getMessage()
    assert "2832298041" not in line and "555-0123" not in line
    assert "***8041" in line and "***0123" in line and "CA0a16f1f9cca1777" in line


def test_chat_demo_can_be_turned_off_and_is_rate_limited(monkeypatch):
    from voiceorder.api import main as api_main
    client = TestClient(api_main.app)
    monkeypatch.setenv("CHAT_DEMO_ENABLED", "0")
    assert client.get("/chat").status_code == 404
    assert client.post("/chat/start").status_code == 404
    assert 'href="/chat"' not in client.get("/").text
    monkeypatch.setenv("CHAT_DEMO_ENABLED", "1")
    monkeypatch.setattr(api_main, "_CHAT_LIMIT", 3)
    api_main._chat_hits.clear()
    codes = [client.post("/chat/message", json={"session_id": "nope", "text": "hi"}).status_code
             for _ in range(5)]
    assert codes[:3] == [404, 404, 404] and codes[3:] == [429, 429]  # unknown session, then limited
    api_main._chat_hits.clear()
