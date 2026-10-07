"""FastAPI front door for the order engine.

Endpoints:
  POST /call/start        a call begins; returns call_id + cart_id + greeting
  POST /tools/{name}      the order tools, driven by the voice agent
  POST /call/end          the call ends; the cart expires, the record is kept
  GET  /health
  GET  /chat              demo web chat with the agent
  POST /voice/speak       ElevenLabs TTS: text -> MP3 (Phase 3, cache-first)
  GET  /voice/audio/{key}.mp3  serve a cached TTS clip (for Twilio <Play>)
  Twilio voice (Phase 5a, TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN):
  POST /twilio/voice      incoming call -> greeting + Gather
  POST /twilio/gather     SpeechResult -> agent turn -> reply + Gather
  POST /twilio/status     call ended -> close the cart
  Owner app (Phase 4, OWNER_SECRET):
  GET  /owner                     dashboard: recent orders + menu manager
  GET  /owner/orders              recent orders (JSON)
  GET  /owner/menu                menu with runtime availability (JSON)
  POST /owner/menu/availability   86 / restore an item
  GET  /owner/backup              hand-order screen for when the AI is down
  POST /owner/backup/submit       fire a hand-entered order through the engine

Auth: set VOICE_SECRET and every endpoint requires an X-Voice-Secret header
(ElevenLabs tool auth / Vapi server authentication in Phase 5). Until it is
set, the server logs a warning and allows local traffic.
"""
from __future__ import annotations
import itertools
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from ..agent.llm import AnthropicClient, MissingCredentials
from ..agent.loop import AgentSession
from ..core import tools
from ..core.cart import CartState
from ..core.catalog import Catalog
from ..core.ports import RestaurantContext
from ..pos_adapters.fake import PROFILES, FakePos
from ..pos_adapters.clover import CloverPosAdapter
from ..pos_adapters.square import SquarePosAdapter
from ..pos_adapters.toast import ToastPosAdapter
from ..voice import tts
from ..voice import voices as voice_catalog
from ..voice.tts import TtsError
from ..voice_adapters import twilio as twilio_adapter
from ..voice_adapters.text import TextAdapter
from .owner import OwnerDeps, build_owner_router
from ..portal.portal import PortalDeps, build_portal_router
from ..tenants.store import Tenant, TenantStore
from .storage import (
    InMemoryCartStore,
    InMemoryOrderStore,
    SqliteCartStore,
    SqliteOrderStore,
)

log = logging.getLogger("voiceorder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the repo .env into os.environ (no override).

    Stdlib-only so there is nothing new to install. Secrets live in
    ~/workspace/voiceorder/.env (chmod 600, git-ignored), never in code."""
    env_path = BASE_DIR.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


@dataclass
class Settings:
    pos_profile: str = os.environ.get("POS_PROFILE", "square_like")
    restaurant_id: str = os.environ.get("RESTAURANT_ID", "taqueria-demo")
    restaurant_name: str = os.environ.get("RESTAURANT_NAME", "Taqueria Demo")
    transfer_number: str = os.environ.get("TRANSFER_NUMBER", "+15550134200")
    pickup_minutes: int = int(os.environ.get("PICKUP_MINUTES", "20"))
    tax_rate: float = float(os.environ.get("TAX_RATE", "0.0825"))
    voice_secret: str = os.environ.get("VOICE_SECRET", "")
    # Previous secret stays valid during rotation: set VOICE_SECRET to the new
    # value, keep VOICE_SECRET_PREVIOUS until every client has switched, then clear it.
    voice_secret_previous: str = os.environ.get("VOICE_SECRET_PREVIOUS", "")
    owner_secret: str = os.environ.get("OWNER_SECRET", "")
    menu_path: str = os.environ.get(
        "MENU_PATH", str(BASE_DIR / "fixtures" / "menu_taqueria.json")
    )
    # Real Square credentials (POS_PROFILE=square only). Set at runtime via env
    # or the repo .env -- never in code.
    square_access_token: str = os.environ.get("SQUARE_ACCESS_TOKEN", "")
    square_location_id: str = os.environ.get("SQUARE_LOCATION_ID", "")
    square_environment: str = os.environ.get("SQUARE_ENVIRONMENT", "sandbox")
    # Real Toast credentials (POS_PROFILE=toast only).
    toast_client_id: str = os.environ.get("TOAST_CLIENT_ID", "")
    toast_client_secret: str = os.environ.get("TOAST_CLIENT_SECRET", "")
    toast_restaurant_guid: str = os.environ.get("TOAST_RESTAURANT_GUID", "")
    toast_takeout_dining_guid: str = os.environ.get("TOAST_TAKEOUT_DINING_GUID", "")
    toast_environment: str = os.environ.get("TOAST_ENVIRONMENT", "sandbox")
    # Real Clover credentials (POS_PROFILE=clover only).
    clover_access_token: str = os.environ.get("CLOVER_ACCESS_TOKEN", "")
    clover_merchant_id: str = os.environ.get("CLOVER_MERCHANT_ID", "")
    clover_environment: str = os.environ.get("CLOVER_ENVIRONMENT", "sandbox")


settings = Settings()


def build_pos_adapter():
    """POS adapter for the configured profile.

    POS_PROFILE=square uses the real Square REST adapter (needs
    SQUARE_ACCESS_TOKEN + SQUARE_LOCATION_ID); the *_like profiles stay fakes.
    """
    if settings.pos_profile == "square":
        if not settings.square_access_token or not settings.square_location_id:
            raise RuntimeError(
                "POS_PROFILE=square needs SQUARE_ACCESS_TOKEN and "
                "SQUARE_LOCATION_ID in the environment"
            )
        return SquarePosAdapter(
            access_token=settings.square_access_token,
            location_id=settings.square_location_id,
            environment=settings.square_environment,
            catalog=catalog,
            tax_rate=settings.tax_rate,
            pickup_minutes=settings.pickup_minutes,
        )
    if settings.pos_profile == "toast":
        if not (
            settings.toast_client_id
            and settings.toast_client_secret
            and settings.toast_restaurant_guid
        ):
            raise RuntimeError(
                "POS_PROFILE=toast needs TOAST_CLIENT_ID, TOAST_CLIENT_SECRET "
                "and TOAST_RESTAURANT_GUID in the environment"
            )
        return ToastPosAdapter(
            client_id=settings.toast_client_id,
            client_secret=settings.toast_client_secret,
            restaurant_guid=settings.toast_restaurant_guid,
            environment=settings.toast_environment,
            takeout_dining_guid=settings.toast_takeout_dining_guid,
            catalog=catalog,
            tax_rate=settings.tax_rate,
            pickup_minutes=settings.pickup_minutes,
        )
    if settings.pos_profile == "clover":
        if not settings.clover_access_token or not settings.clover_merchant_id:
            raise RuntimeError(
                "POS_PROFILE=clover needs CLOVER_ACCESS_TOKEN and "
                "CLOVER_MERCHANT_ID in the environment"
            )
        return CloverPosAdapter(
            access_token=settings.clover_access_token,
            merchant_id=settings.clover_merchant_id,
            environment=settings.clover_environment,
            catalog=catalog,
            tax_rate=settings.tax_rate,
            pickup_minutes=settings.pickup_minutes,
        )
    if settings.pos_profile not in PROFILES:
        raise RuntimeError(f"unknown POS_PROFILE={settings.pos_profile}")
    return FakePos(profile=settings.pos_profile, catalog=catalog, tax_rate=settings.tax_rate)


def _load_catalog() -> Catalog:
    """Load the menu: the real Square catalog when on the square profile.

    Falls back to the local fixture when sync is disabled
    (SQUARE_MENU_SYNC=0), the token is missing, or Square is unreachable
    with no cached menu.
    """
    if (
        settings.pos_profile == "square"
        and os.environ.get("SQUARE_MENU_SYNC", "1") == "1"
        and settings.square_access_token
    ):
        from ..pos_adapters.square_catalog import sync_square_menu

        cache = Path("data") / "square_catalog.json"
        try:
            menu = sync_square_menu(
                settings.square_access_token,
                settings.square_environment,
                cache,
            )
            log.info("menu: %d items synced from Square", len(menu["items"]))
            return Catalog.from_dict(menu)
        except Exception:
            log.warning("Square menu sync failed; using local fixture", exc_info=True)
    return Catalog.from_json(settings.menu_path)


catalog = _load_catalog()
pos = build_pos_adapter()
adapter = TextAdapter()

_storage_backend = os.environ.get("STORAGE_BACKEND", "sqlite").lower()
_db_path = Path(
    os.environ.get(
        "VOICEORDER_DB", str(Path(__file__).resolve().parent.parent.parent / "data" / "voiceorder.db")
    )
)
if _storage_backend == "sqlite":
    cart_store = SqliteCartStore(_db_path)
    order_store = SqliteOrderStore(_db_path)
    log.info("storage backend: sqlite (%s)", _db_path)
else:
    cart_store = InMemoryCartStore()
    order_store = InMemoryOrderStore()
    log.warning("storage backend: memory (carts and orders are lost on restart)")

if not settings.voice_secret:
    log.warning("VOICE_SECRET is not set; endpoints accept unauthenticated local calls")


# --- Multi-tenancy -----------------------------------------------------------
# One platform, many restaurants. Each tenant owns: a name + Twilio number
# (calls to that number route to them), encrypted POS credentials, settings,
# orders, and call transcripts. The portal (/portal) is the tenant website.
tenant_store = TenantStore(_db_path)


def _env_pos_creds(provider: str) -> dict:
    """POS credentials from process env (legacy single-tenant path). Used to
    seed the default tenant and as a fallback when a tenant has none saved."""
    if provider == "square":
        if settings.square_access_token and settings.square_location_id:
            return {
                "access_token": settings.square_access_token,
                "location_id": settings.square_location_id,
                "environment": settings.square_environment,
            }
    elif provider == "toast":
        if (settings.toast_client_id and settings.toast_client_secret
                and settings.toast_restaurant_guid):
            return {
                "client_id": settings.toast_client_id,
                "client_secret": settings.toast_client_secret,
                "restaurant_guid": settings.toast_restaurant_guid,
                "takeout_dining_guid": settings.toast_takeout_dining_guid,
                "environment": settings.toast_environment,
            }
    elif provider == "clover":
        if settings.clover_access_token and settings.clover_merchant_id:
            return {
                "access_token": settings.clover_access_token,
                "merchant_id": settings.clover_merchant_id,
                "environment": settings.clover_environment,
            }
    return {}


def _seed_default_tenant() -> Tenant:
    """The pre-tenancy restaurant (Hyderabad House) becomes tenant #1: its
    env credentials are copied into encrypted tenant storage and old orders
    are tagged to it."""
    phone = os.environ.get("TWILIO_PHONE_NUMBER", "")
    tenant = tenant_store.get_tenant_by_number(phone) if phone else None
    if tenant is None:
        tenant = tenant_store.get_tenant_by_name(settings.restaurant_name)
    if tenant is None:
        tenant = tenant_store.create_tenant(settings.restaurant_name, phone)
        log.info("tenancy: created default tenant %s (%s)", tenant.id, tenant.name)
    tenant_store.set_settings(tenant.id, {
        "restaurant_name": settings.restaurant_name,
        "pos_profile": settings.pos_profile,
        "transfer_number": settings.transfer_number,
        "pickup_minutes": str(settings.pickup_minutes),
        "tax_rate": str(settings.tax_rate),
    })
    for provider in ("square", "toast", "clover"):
        if not tenant_store.has_secret(tenant.id, provider):
            creds = _env_pos_creds(provider)
            if creds:
                tenant_store.set_secret(tenant.id, provider, creds)
                log.info("tenancy: migrated %s credentials for default tenant", provider)
    moved = tenant_store.backfill_orders_tenant(tenant.id)
    if moved:
        log.info("tenancy: tagged %d legacy orders to default tenant", moved)
    return tenant_store.get_tenant(tenant.id)


default_tenant = _seed_default_tenant()

_tenant_adapters: dict[str, tuple[str, Any]] = {}  # tenant_id -> (fingerprint, adapter)
_tenant_catalogs: dict[str, tuple[str, Catalog]] = {}


def _tenant_fingerprint(tenant: Tenant, creds: dict) -> str:
    s = tenant.settings
    return "|".join([
        tenant.id, s.get("pos_profile", ""), s.get("tax_rate", ""),
        s.get("pickup_minutes", ""),
        str(sorted((k, str(v)) for k, v in creds.items() if "token" not in k and "secret" not in k)),
    ])


def tenant_pos_adapter(tenant: Tenant, override: dict | None = None) -> Any:
    """POS adapter for a tenant, built from their encrypted credentials.

    `override` is {provider: creds} and lets the portal test unsaved
    credentials without touching the store."""
    provider = tenant.setting("pos_profile", "") or settings.pos_profile
    if override and provider in override:
        creds = dict(override[provider])
    else:
        creds = tenant_store.get_secret(tenant.id, provider)
        if not creds:
            creds = _env_pos_creds(provider)  # legacy fallback
    tax_rate = float(tenant.setting("tax_rate", "") or settings.tax_rate)
    pickup_minutes = int(tenant.setting("pickup_minutes", "") or settings.pickup_minutes)
    cat = tenant_catalog(tenant)
    if provider == "square":
        if not creds.get("access_token") or not creds.get("location_id"):
            raise RuntimeError("Square needs an access token and location ID")
        return SquarePosAdapter(
            access_token=creds["access_token"],
            location_id=creds["location_id"],
            environment=creds.get("environment", "production"),
            catalog=cat, tax_rate=tax_rate, pickup_minutes=pickup_minutes,
        )
    if provider == "toast":
        if not (creds.get("client_id") and creds.get("client_secret")
                and creds.get("restaurant_guid")):
            raise RuntimeError("Toast needs client ID, client secret and restaurant GUID")
        return ToastPosAdapter(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
            restaurant_guid=creds["restaurant_guid"],
            environment=creds.get("environment", "production"),
            takeout_dining_guid=creds.get("takeout_dining_guid", ""),
            catalog=cat, tax_rate=tax_rate, pickup_minutes=pickup_minutes,
        )
    if provider == "clover":
        if not creds.get("access_token") or not creds.get("merchant_id"):
            raise RuntimeError("Clover needs an API token and merchant ID")
        return CloverPosAdapter(
            access_token=creds["access_token"],
            merchant_id=creds["merchant_id"],
            environment=creds.get("environment", "production"),
            catalog=cat, tax_rate=tax_rate, pickup_minutes=pickup_minutes,
        )
    # fake profiles (dev / demo tenants)
    profile = PROFILES.get(provider)
    if profile is None:
        raise RuntimeError(f"unknown POS profile: {provider}")
    return FakePos(profile, catalog=cat)


def cached_tenant_adapter(tenant: Tenant) -> Any:
    """Live-call path: reuse the adapter until the tenant's config changes."""
    creds = tenant_store.get_secret(tenant.id, tenant.setting("pos_profile", ""))
    fp = _tenant_fingerprint(tenant, creds)
    hit = _tenant_adapters.get(tenant.id)
    if hit and hit[0] == fp:
        return hit[1]
    adapter = tenant_pos_adapter(tenant)
    _tenant_adapters[tenant.id] = (fp, adapter)
    return adapter


def tenant_catalog(tenant: Tenant) -> Catalog:
    """Menu for a tenant. Square tenants sync their live catalog (cached per
    tenant); everyone else uses the platform default menu."""
    provider = tenant.setting("pos_profile", "") or settings.pos_profile
    if tenant.id == default_tenant.id:
        return catalog
    if provider == "square":
        creds = tenant_store.get_secret(tenant.id, "square") or _env_pos_creds("square")
        if creds.get("access_token"):
            from ..pos_adapters.square_catalog import sync_square_menu

            cache = Path("data") / "menus" / f"{tenant.id}.json"
            fp = creds.get("environment", "production")
            hit = _tenant_catalogs.get(tenant.id)
            if hit and hit[0] == fp:
                return hit[1]
            try:
                menu = sync_square_menu(
                    creds["access_token"], creds.get("environment", "production"), cache
                )
                cat = Catalog.from_dict(menu)
                _tenant_catalogs[tenant.id] = (fp, cat)
                return cat
            except Exception:
                log.warning("tenant %s: Square menu sync failed", tenant.id, exc_info=True)
                if cache.exists():
                    return Catalog.from_json(str(cache))
    return catalog


def tenant_context(tenant: Tenant) -> RestaurantContext:
    s = tenant.settings
    return RestaurantContext(
        restaurant_id=tenant.slug,
        restaurant_name=s.get("restaurant_name", "") or tenant.name,
        pos_profile=s.get("pos_profile", "") or settings.pos_profile,
        voice_platform="voice",
        transfer_number=s.get("transfer_number", "") or settings.transfer_number,
        pickup_minutes=int(s.get("pickup_minutes", "") or settings.pickup_minutes),
        tax_rate=float(s.get("tax_rate", "") or settings.tax_rate),
        tenant_id=tenant.id,
    )


def _drop_tenant_caches(tenant_id: str) -> None:
    _tenant_adapters.pop(tenant_id, None)
    _tenant_catalogs.pop(tenant_id, None)


def resolve_call_tenant(to_number: str) -> Tenant:
    """Route an incoming call to the tenant owning the dialed number."""
    return tenant_store.get_tenant_by_number(to_number) or default_tenant


def restaurant_context() -> RestaurantContext:
    """Legacy single-tenant entry point: the default tenant's context."""
    return tenant_context(default_tenant)


def _voice_secrets() -> list[str]:
    return [s for s in (settings.voice_secret, settings.voice_secret_previous) if s]


def require_secret(x_voice_secret: str | None = Header(default=None)) -> None:
    valid = _voice_secrets()
    if valid and x_voice_secret not in valid:
        raise HTTPException(status_code=401, detail="invalid voice secret")


app = FastAPI(title="VoiceOrderAI", version="0.4.0")
app.include_router(
    build_owner_router(
        OwnerDeps(
            catalog=catalog,
            pos=pos,
            cart_store=cart_store,
            order_store=order_store,
            ctx=restaurant_context(),
            owner_secret=settings.owner_secret,
        )
    )
)
app.include_router(
    build_portal_router(
        PortalDeps(
            tenants=tenant_store,
            order_store=order_store,
            build_adapter=tenant_pos_adapter,
            get_catalog=tenant_catalog,
            on_config_changed=_drop_tenant_caches,
        )
    )
)

# --- Web chat demo (Phase 2): talk to the agent in a browser page -----------
_chat_sessions: dict[str, AgentSession] = {}
_chat_llm: AnthropicClient | None = None


def _chat_llm_client() -> AnthropicClient:
    global _chat_llm
    if _chat_llm is None:
        try:
            _chat_llm = AnthropicClient()
        except MissingCredentials as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _chat_llm


class ChatStartResponse(BaseModel):
    session_id: str
    greeting: str


class ChatMessageRequest(BaseModel):
    session_id: str
    text: str


CHAT_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>VoiceOrderAI demo chat</title>
<style>body{font-family:system-ui;max-width:640px;margin:2em auto;padding:0 1em}
#log{border:1px solid #ccc;border-radius:8px;padding:1em;height:60vh;overflow-y:auto}
.msg{margin:.5em 0}.agent{color:#1a5}.you{text-align:right;color:#15c}
#row{display:flex;gap:.5em;margin-top:1em}input{flex:1;padding:.6em;font-size:1em}
button{padding:.6em .9em;font-size:1em;cursor:pointer}</style>
</head><body>
<h2>Taqueria Demo &mdash; order chat</h2>
<div id="log"></div>
<div id="row"><input id="box" placeholder="Type your order..." autocomplete="off">
<button onclick="send()">Send</button>
<button id="voiceBtn" onclick="toggleVoice()" title="Toggle spoken replies">🔊</button></div>
<script>
let sid=null, voiceOn=true;
const log=document.getElementById('log'), box=document.getElementById('box'),
      voiceBtn=document.getElementById('voiceBtn');
function toggleVoice(){voiceOn=!voiceOn;voiceBtn.textContent=voiceOn?'🔊':'🔇';}
function add(cls,t){const d=document.createElement('div');d.className='msg '+cls;d.textContent=t;log.appendChild(d);log.scrollTop=log.scrollHeight;}
async function speak(text){
  if(!voiceOn||!text)return;
  try{
    const r=await fetch('/voice/speak',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:text})});
    if(!r.ok)return;
    const url=URL.createObjectURL(await r.blob());
    await new Audio(url).play().catch(()=>{});
  }catch(e){/* voice is best-effort; the text reply still shows */}
}
fetch('/chat/start',{method:'POST'}).then(r=>r.json()).then(j=>{sid=j.session_id;add('agent',j.greeting);speak(j.greeting);});
async function send(){const t=box.value.trim();if(!t||!sid)return;box.value='';add('you',t);
 const r=await fetch('/chat/message',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({session_id:sid,text:t})});
 const j=await r.json();const reply=j.reply||('Error: '+(j.detail||r.status));add('agent',reply);speak(j.reply);}
box.addEventListener('keydown',e=>{if(e.key==='Enter')send();});
</script></body></html>"""


@app.get("/chat", response_class=HTMLResponse)
def chat_page() -> str:
    return CHAT_PAGE


@app.post("/chat/start", response_model=ChatStartResponse)
def chat_start() -> ChatStartResponse:
    llm = _chat_llm_client()
    model = os.environ.get("ANTHROPIC_MODEL", "")
    if not model:
        raise HTTPException(status_code=503, detail="set ANTHROPIC_MODEL to an Anthropic model id")
    ctx = restaurant_context()
    session_id = uuid.uuid4().hex[:12]
    chat_pos = build_pos_adapter()
    cart = cart_store.create(ctx.restaurant_id)
    session = AgentSession(llm, catalog, chat_pos, ctx, cart, model)
    _chat_sessions[session_id] = session
    return ChatStartResponse(
        session_id=session_id, greeting=adapter.call_start_response(ctx)["greeting"]
    )


@app.post("/chat/message")
def chat_message(req: ChatMessageRequest) -> dict:
    session = _chat_sessions.get(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown chat session")
    turn = session.handle_caller_message(req.text)
    cart = session.cart
    if cart.transferred or cart.state == CartState.SUBMITTED:
        _chat_sessions.pop(req.session_id, None)
    return {
        "reply": turn["text"],
        "cart_state": cart.state.value,
        "transferred": cart.transferred,
    }


class CallStartRequest(BaseModel):
    called_number: str = "unknown"
    caller_id: str = "unknown"
    call_id: str | None = None


# --- Voice: ElevenLabs TTS (Phase 3) ---------------------------------------
def _tts_enabled() -> bool:
    return os.environ.get("TTS_ENABLED", "1") == "1"


# Rate limiting for the public TTS endpoint: the ElevenLabs free plan is
# 10,000 chars/month, so an open endpoint is a budget hole. Token bucket per
# client IP: 30 renders per minute, burst-friendly. The Twilio gather loop
# calls tts.synthesize() directly (server-side), so phone calls are unaffected.
_TTS_BUCKET_MAX = 30
_TTS_BUCKET_WINDOW = 60.0
_tts_buckets: dict[str, list[float]] = {}


def _tts_rate_limit_ok(client_ip: str) -> bool:
    now = time.monotonic()
    stamps = _tts_buckets.get(client_ip, [])
    stamps = [t for t in stamps if now - t < _TTS_BUCKET_WINDOW]
    if len(stamps) >= _TTS_BUCKET_MAX:
        _tts_buckets[client_ip] = stamps
        return False
    stamps.append(now)
    _tts_buckets[client_ip] = stamps
    return True


class SpeakRequest(BaseModel):
    text: str
    voice_id: str | None = None
    model_id: str | None = None


@app.post("/voice/speak")
def voice_speak(
    req: SpeakRequest,
    request: Request,
    _auth: None = Depends(require_secret),
) -> Response:
    """Render text to MP3 with ElevenLabs. Cache-first to protect the free
    character budget. Requires X-Voice-Secret once VOICE_SECRET is set, and is
    rate-limited per client IP. Set TTS_ENABLED=0 to disable synthesis."""
    client_ip = request.client.host if request.client else "unknown"
    if not _tts_rate_limit_ok(client_ip):
        raise HTTPException(
            status_code=429,
            detail="TTS rate limit exceeded (30/min per client)",
            headers={"Retry-After": "60"},
        )
    if not _tts_enabled():
        raise HTTPException(
            status_code=503, detail="voice synthesis is disabled (TTS_ENABLED=0)"
        )
    voice_id = req.voice_id or tts.DEFAULT_VOICE
    model_id = req.model_id or tts.DEFAULT_MODEL
    try:
        cleaned = " ".join(req.text.split())
        if not cleaned:
            raise ValueError("nothing to synthesize: text is empty")
        cached = tts.cache_path(cleaned, voice_id, model_id).exists()
        audio = tts.synthesize(req.text, voice_id=voice_id, model_id=model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TtsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"X-TTS-Cache": "HIT" if cached else "MISS"},
    )


@app.get("/voice/audio/{key}.mp3")
def voice_audio(key: str) -> FileResponse:
    """Serve a cached TTS clip. Twilio <Play> needs a plain GET URL, so the
    gather loop pre-renders replies through /voice/speak and points TwiML here."""
    if not (len(key) == 64 and all(c in "0123456789abcdef" for c in key)):
        raise HTTPException(status_code=404, detail="unknown audio")
    path = tts.cache_dir() / f"{key}.mp3"
    if not path.exists():
        raise HTTPException(status_code=404, detail="unknown audio")
    return FileResponse(path, media_type="audio/mpeg")


# --- Twilio voice: the Gather loop (Phase 5a) --------------------------------
# Twilio cannot send our X-Voice-Secret header, so these webhooks authenticate
# with Twilio's own request signature (X-Twilio-Signature) instead.
_twilio_sessions: dict[str, AgentSession] = {}  # CallSid -> agent session
_twilio_turns: dict[str, dict] = {}  # turn_id -> {"done": bool, ...}


def _twilio_signature_ok(request: Request, form: dict[str, str]) -> bool:
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    public_base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not token or not public_base:
        log.warning(
            "TWILIO_AUTH_TOKEN/PUBLIC_BASE_URL not set; "
            "accepting Twilio webhook without signature validation (dev only)"
        )
        return True
    signature = request.headers.get("x-twilio-signature", "")
    url = f"{public_base}{request.url.path}"
    # Twilio signs the full URL *including* the query string (e.g.
    # /twilio/turn?id=...&sid=...&n=0). Omitting it breaks validation on
    # every poll request and the caller hears Twilio's "application error".
    if request.url.query:
        url = f"{url}?{request.url.query}"
    return twilio_adapter.validate_signature(url, form, signature, token)


def _public_base_url() -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base.startswith("https://"):
        raise HTTPException(
            status_code=503,
            detail="set PUBLIC_BASE_URL to the public https URL Twilio calls",
        )
    return base


def _new_twilio_session(tenant: Tenant | None = None) -> AgentSession | None:
    """One agent brain per phone call, wired to the tenant being called."""
    try:
        llm = _chat_llm_client()
    except HTTPException:
        return None
    model = os.environ.get("ANTHROPIC_MODEL", "")
    if not model:
        return None
    tenant = tenant or default_tenant
    ctx = tenant_context(tenant)
    call_pos = cached_tenant_adapter(tenant)
    cat = tenant_catalog(tenant)
    cart = cart_store.create(ctx.restaurant_id)
    return AgentSession(llm, cat, call_pos, ctx, cart, model)


def _tenant_voice(tenant_id: str = "") -> tuple[str, str]:
    """(voice_id, model_id) for a tenant's AI voice, falling back to the
    platform defaults when unset. Owners pick these in the portal's Settings
    page; the choice applies to every Twilio call for their restaurant."""
    if tenant_id:
        try:
            tenant = tenant_store.get_tenant(tenant_id)
            if tenant is not None:
                voice_id = tenant.setting("voice_id", "") or voice_catalog.DEFAULT_VOICE_ID
                model_id = tenant.setting("voice_model", "") or voice_catalog.DEFAULT_MODEL_ID
                return voice_id, model_id
        except Exception:
            log.warning("tenant voice lookup failed for %s", tenant_id, exc_info=True)
    return voice_catalog.DEFAULT_VOICE_ID, voice_catalog.DEFAULT_MODEL_ID


def _speak_to_url(text: str, tenant_id: str = "") -> str | None:
    """Render a reply to MP3 and return its public URL, or None when synthesis
    is unavailable (the caller then falls back to Twilio <Say>)."""
    try:
        cleaned = " ".join(text.split())
        voice_id, model_id = _tenant_voice(tenant_id)
        tts.synthesize(cleaned, voice_id=voice_id, model_id=model_id)
        key = tts.cache_key(cleaned, voice_id, model_id)
        return f"{_public_base_url()}/voice/audio/{key}.mp3"
    except (TtsError, ValueError, HTTPException) as exc:
        log.warning("TTS failed, falling back to <Say>: %s", exc)
        return None


def _send_receipt_sms(to_number: str, cart, restaurant_name: str = "") -> None:
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_number = os.environ.get("TWILIO_PHONE_NUMBER", "")
    if not (sid and token and from_number and to_number):
        log.info("SMS receipt skipped: TWILIO_* not fully configured")
        return
    order = cart.last_order or {}
    totals = (order.get("totals") or {})
    payment = order.get("payment") or {}
    body = (
        f"{restaurant_name or settings.restaurant_name}: order {order.get('order_number', '')} confirmed. "
        f"Total ${totals.get('total', 0):.2f}. "
        f"Ready {order.get('pickup_time', 'soon')}."
    )
    if payment.get("kind") == "link" and payment.get("url"):
        body += f" Pay here: {payment['url']}"
    try:
        twilio_adapter.send_sms(sid, token, from_number, to_number, body)
        log.info("SMS receipt sent to %s", to_number[-4:].rjust(len(to_number), "*"))
    except Exception as exc:  # best-effort: never break the call over SMS
        log.warning("SMS receipt failed: %s", exc)


def _twiml_response(xml: str) -> Response:
    return Response(content=xml, media_type="application/xml")


@app.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    """Twilio webhook: a call just came in. Greet and start listening."""
    form = {k: v for k, v in (await request.form()).items()}
    if not _twilio_signature_ok(request, form):
        raise HTTPException(status_code=403, detail="bad twilio signature")
    call_sid = str(form.get("CallSid", ""))
    if not call_sid:
        raise HTTPException(status_code=400, detail="missing CallSid")
    tenant = resolve_call_tenant(str(form.get("To", "")))
    session = _new_twilio_session(tenant)
    if session is None:
        log.warning("twilio call %s: LLM not configured", call_sid)
        return _twiml_response(twilio_adapter.unavailable())
    _twilio_sessions[call_sid] = session
    tenant_store.start_call(
        call_sid, tenant.id, str(form.get("From", "")), str(form.get("To", ""))
    )
    ctx = tenant_context(tenant)
    greeting = adapter.call_start_response(ctx)["greeting"]
    base = _public_base_url()
    gather_url = f"{base}/twilio/gather"
    log.info("twilio call started: %s from %s (tenant %s)",
             call_sid, form.get("From"), tenant.name)
    return _twiml_response(
        twilio_adapter.answer_call(_speak_to_url(greeting, tenant.id), greeting, gather_url)
    )


# Varied filler phrases so the caller doesn't hear "One moment." on every turn.
_FILLERS = (
    "One moment.",
    "Let me check that.",
    "Got it, one second.",
    "Looking into that.",
)
_filler_cycle = itertools.cycle(_FILLERS)


def _filler_audio_url(filler: str, tenant_id: str) -> str | None:
    """Public URL for a pre-synthesized filler clip, or None if not cached.

    Never synthesizes here: the webhook must answer in milliseconds, so we
    only use clips the startup pre-warm already rendered to the TTS cache.
    """
    try:
        voice_id, model_id = _tenant_voice(tenant_id)
        cleaned = " ".join(filler.split())
        path = tts.cache_path(cleaned, voice_id, model_id)
        if path.exists():
            key = tts.cache_key(cleaned, voice_id, model_id)
            return f"{_public_base_url()}/voice/audio/{key}.mp3"
    except Exception:
        log.warning("filler cache check failed", exc_info=True)
    return None


@app.post("/twilio/gather")
async def twilio_gather(request: Request) -> Response:
    """Twilio webhook: the caller said something. Start one agent turn.

    Voice-webhook latency budget: Twilio (and the tunnel in front of us)
    give up after ~15s and the caller hears "application error". A turn is
    two LLM round-trips plus a POS call plus TTS -- routinely 5-10s and
    occasionally 30s+. So the turn runs on a background thread; this
    webhook answers instantly with a filler and a <Redirect> that polls
    /twilio/turn until the turn is done.
    """
    form = {k: v for k, v in (await request.form()).items()}
    if not _twilio_signature_ok(request, form):
        raise HTTPException(status_code=403, detail="bad twilio signature")
    call_sid = str(form.get("CallSid", ""))
    session = _twilio_sessions.get(call_sid)
    if session is None:
        return _twiml_response(twilio_adapter.end_call(None, "Sorry, I lost track of your call. Goodbye."))
    gather_url = f"{_public_base_url()}/twilio/gather"
    speech = str(form.get("SpeechResult", "")).strip()
    if not speech:
        return _twiml_response(
            twilio_adapter.continue_call(
                _speak_to_url("Sorry, I didn't catch that. What would you like to order?",
                              session.ctx.tenant_id),
                "Sorry, I didn't catch that. What would you like to order?",
                gather_url,
            )
        )
    turn_id = uuid.uuid4().hex
    _twilio_turns[turn_id] = {"done": False, "sid": call_sid}
    threading.Thread(
        target=_run_turn_background,
        args=(turn_id, call_sid, session, speech),
        daemon=True,
    ).start()
    base = _public_base_url()
    poll_url = _xml_escape(f"{base}/twilio/turn?id={turn_id}&sid={call_sid}&n=0")
    # Varied filler in the tenant's voice when cached; plain <Say> fallback
    # keeps the webhook answering in milliseconds.
    filler = next(_filler_cycle)
    filler_audio = _filler_audio_url(filler, session.ctx.tenant_id)
    if filler_audio:
        spoken = f"<Play>{_xml_escape(filler_audio)}</Play>"
    else:
        spoken = f"<Say>{_xml_escape(filler)}</Say>"
    return _twiml_response(
        f'<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"{spoken}"
        f'<Redirect method="POST">{poll_url}</Redirect>'
        f"</Response>"
    )


def _run_turn_background(
    turn_id: str, call_sid: str, session: AgentSession, speech: str
) -> None:
    """Run one agent turn off the webhook thread; /twilio/turn polls this."""
    try:
        turn = session.handle_caller_message(speech)
        log.info(
            "twilio call %s turn: heard=%r tools=%s reply=%r",
            call_sid,
            speech[:160],
            [
                f"{c.get('name')}:{c.get('ok')}:{c.get('error_code')}"
                for c in turn.get("tool_calls", [])
            ],
            turn["text"][:220],
        )
        # Synthesize the reply audio here, in the background, so the poll
        # that finds the turn done can answer with the final TwiML
        # immediately instead of blocking on TTS first.
        audio_url = _speak_to_url(turn["text"], session.ctx.tenant_id)
        _twilio_turns[turn_id] = {"done": True, "turn": turn, "audio_url": audio_url}
        # Tenant-visible live transcript: the portal polls this per call.
        try:
            tenant_store.append_turn(
                call_sid, speech, turn["text"], turn.get("tool_calls") or []
            )
        except Exception:
            log.warning("twilio call %s: transcript append failed", call_sid,
                        exc_info=True)
    except Exception as exc:  # never leave the caller hanging on a poll
        log.exception("twilio call %s background turn failed", call_sid)
        _twilio_turns[turn_id] = {"done": True, "error": str(exc)[:200]}


@app.post("/twilio/turn")
async def twilio_turn(request: Request) -> Response:
    """Poll for a background agent turn; render the final TwiML when done."""
    # Redirect parameters travel in the query string; Twilio's standard
    # webhook fields (CallSid, From, ...) arrive in the POST body.
    query = dict(request.query_params)
    form = {k: v for k, v in (await request.form()).items()}
    if not _twilio_signature_ok(request, form):
        raise HTTPException(status_code=403, detail="bad twilio signature")
    call_sid = str(query.get("sid") or form.get("CallSid", ""))
    turn_id = str(query.get("id", ""))
    poll_n = int(query.get("n", "0") or 0)
    session = _twilio_sessions.get(call_sid)
    base = _public_base_url()
    gather_url = f"{base}/twilio/gather"
    if session is None:
        _twilio_turns.pop(turn_id, None)
        return _twiml_response(twilio_adapter.end_call(None, "Sorry, I lost track of your call. Goodbye."))
    entry = _twilio_turns.get(turn_id)
    if entry is None or not entry.get("done"):
        # Still thinking: pause briefly, then poll again. Cap the loop so a
        # stuck turn degrades to a polite retry instead of spinning forever.
        if poll_n >= 30:
            _twilio_turns.pop(turn_id, None)
            return _twiml_response(
                twilio_adapter.continue_call(
                    _speak_to_url("Sorry, that took too long. Could you say that again?",
                                  session.ctx.tenant_id),
                    "Sorry, that took too long. Could you say that again?",
                    gather_url,
                )
            )
        poll_url = _xml_escape(f"{base}/twilio/turn?id={turn_id}&sid={call_sid}&n={poll_n + 1}")
        return _twiml_response(
            f'<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Pause length="1"/>'
            f'<Redirect method="POST">{poll_url}</Redirect>'
            f"</Response>"
        )
    _twilio_turns.pop(turn_id, None)
    if entry.get("error"):
        return _twiml_response(
            twilio_adapter.continue_call(
                _speak_to_url("Sorry, I hit a snag. Could you say that again?",
                              session.ctx.tenant_id),
                "Sorry, I hit a snag. Could you say that again?",
                gather_url,
            )
        )
    return _twiml_response(_twilio_final_twiml(call_sid, session, entry, form))


def _twilio_final_twiml(
    call_sid: str, session: AgentSession, entry: dict, form: dict
) -> str:
    """Build the end-of-turn TwiML: continue, transfer, or submit+hangup."""
    cart = session.cart
    turn = entry["turn"]
    reply = turn["text"]
    # The reply audio was synthesized in the background turn thread; only
    # synthesize here as a fallback (e.g. cache miss across restarts).
    audio_url = entry.get("audio_url") or _speak_to_url(reply, session.ctx.tenant_id)
    gather_url = f"{_public_base_url()}/twilio/gather"
    if cart.transferred or cart.state == CartState.SUBMITTED:
        _twilio_sessions.pop(call_sid, None)
    if cart.transferred:
        log.info("twilio call %s: transferring to %s", call_sid, session.ctx.transfer_number)
        return twilio_adapter.transfer_call(
            audio_url, reply, session.ctx.transfer_number
        )
    if cart.state == CartState.SUBMITTED:
        from_number = str(form.get("From", ""))
        _persist_submitted_order(cart, call_sid, session.ctx)
        if from_number:
            _send_receipt_sms(from_number, cart, session.ctx.restaurant_name)
        log.info("twilio call %s: order submitted", call_sid)
        return twilio_adapter.end_call(audio_url, reply)
    return twilio_adapter.continue_call(audio_url, reply, gather_url)


@app.post("/twilio/status")
async def twilio_status(request: Request) -> Response:
    """Twilio webhook: call status changed. Clean up when the call is over."""
    form = {k: v for k, v in (await request.form()).items()}
    if not _twilio_signature_ok(request, form):
        raise HTTPException(status_code=403, detail="bad twilio signature")
    call_sid = str(form.get("CallSid", ""))
    status = str(form.get("CallStatus", ""))
    if status in ("completed", "busy", "failed", "no-answer", "canceled"):
        session = _twilio_sessions.pop(call_sid, None)
        if session is not None:
            cart_store.delete(session.cart.cart_id)
        # Drop any in-flight turn entries for this call.
        for tid in [t for t, e in _twilio_turns.items() if e.get("sid") == call_sid]:
            _twilio_turns.pop(tid, None)
        try:
            tenant_store.end_call(call_sid, status)
        except Exception:
            log.warning("twilio call %s: transcript close failed", call_sid, exc_info=True)
        log.info("twilio call %s ended: %s", call_sid, status)
    return _twiml_response('<?xml version="1.0" encoding="UTF-8"?><Response />')


class ToolRequest(BaseModel):
    call_id: str
    cart_id: str
    arguments: dict[str, Any] = {}


class CallEndRequest(BaseModel):
    call_id: str
    cart_id: str
    duration_seconds: float = 0.0
    transcript: str = ""


_APP_STARTED_AT = time.time()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VoiceOrderAI</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:3em auto;
padding:0 1em;color:#222}h1{font-size:1.6em}a.btn{display:inline-block;margin:.4em .6em .4em 0;
padding:.7em 1.2em;background:#1a73e8;color:#fff;border-radius:8px;text-decoration:none}
a.btn.alt{background:#5f6368}p{color:#555}</style></head><body>
<h1>🎙️ VoiceOrderAI</h1>
<p>AI phone ordering for restaurants. Manage your restaurant below, or try the text demo.</p>
<a class="btn" href="/portal/login">Restaurant portal</a>
<a class="btn alt" href="/owner">Owner dashboard</a>
<a class="btn alt" href="/chat">Text demo</a>
</body></html>"""


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": "0.4.0",
        "pos_profile": settings.pos_profile,
        "restaurant": settings.restaurant_name,
        "menu_items": len(catalog.all_items()),
        "tts_enabled": _tts_enabled(),
        "started_at": _APP_STARTED_AT,
        "uptime_seconds": round(time.time() - _APP_STARTED_AT, 1),
    }


@app.post("/call/start")
def call_start(req: CallStartRequest, _auth: None = Depends(require_secret)) -> dict:
    call = adapter.parse_call_start(req.model_dump())
    ctx = restaurant_context()
    cart = cart_store.create(ctx.restaurant_id)
    response = adapter.call_start_response(ctx)
    response.update({"call_id": call.call_id, "cart_id": cart.cart_id})
    log.info("call started: %s from %s", call.call_id, call.caller_id)
    return response


def _persist_submitted_order(cart, call_id: str, ctx: RestaurantContext) -> None:
    """Record a submitted order in the order store.

    Single choke point: the /tools/submit_order endpoint AND the Twilio
    gather loop both land here, so a phone order is never lost from the
    owner's view just because it came in over voice instead of HTTP.
    Defensive getattr: never let persistence break the caller's response."""
    order = getattr(cart, "last_order", None) or {}
    lines = getattr(cart, "lines", None) or []
    order_store.save(
        {
            "order_id": order.get("order_id"),
            "restaurant_id": ctx.restaurant_id,
            "tenant_id": ctx.tenant_id,
            "call_id": call_id,
            "cart_id": getattr(cart, "cart_id", call_id),
            "order_number": order.get("order_number"),
            "status": order.get("status"),
            "pickup_time": order.get("pickup_time"),
            "totals": order.get("totals"),
            "payment": order.get("payment"),
            "customer_name": getattr(cart, "customer_name", None),
            "customer_phone": getattr(cart, "customer_phone", None),
            "lines": [line.to_dict() for line in lines],
        }
    )
    state = getattr(getattr(cart, "state", None), "value", "?")
    log.info("order %s submitted on %s (state=%s)", order.get("order_number"), call_id, state)


@app.post("/tools/{name}")
def run_tool(name: str, req: ToolRequest, _auth: None = Depends(require_secret)) -> dict:
    if name not in tools.TOOL_NAMES:
        raise HTTPException(status_code=404, detail=f"unknown tool {name}")
    cart = cart_store.get(req.cart_id)
    if cart is None:
        raise HTTPException(status_code=404, detail="unknown or expired cart")
    ctx = restaurant_context()
    result = tools.dispatch(
        name, cart=cart, catalog=catalog, pos=pos, ctx=ctx, arguments=req.arguments
    )
    cart_store.save(cart)
    if name == "submit_order" and result.ok:
        _persist_submitted_order(cart, req.call_id, ctx)
    return result.to_dict()


@app.post("/call/end")
def call_end(req: CallEndRequest, _auth: None = Depends(require_secret)) -> dict:
    cart = cart_store.get(req.cart_id)
    record = adapter.parse_call_end(
        {
            "call_id": req.call_id,
            "restaurant_id": settings.restaurant_id,
            "duration_seconds": req.duration_seconds,
            "transcript": req.transcript,
            "order_id": (
                cart.last_order.get("order_id")
                if cart and cart.last_order
                else None
            ),
        }
    )
    if cart is not None and cart.state != CartState.SUBMITTED:
        log.info("call %s ended without an order (state=%s)", req.call_id, cart.state.value)
    cart_store.delete(req.cart_id)
    return {
        "call_id": record.call_id,
        "order_id": record.order_id,
        "duration_seconds": record.duration_seconds,
    }


@app.get("/orders/recent")
def recent_orders(limit: int = 20, _auth: None = Depends(require_secret)) -> dict:
    """Backup-screen preview: recent submitted orders (owner app in Phase 4)."""
    return {
        "orders": order_store.list_recent(settings.restaurant_id, limit=limit),
    }


def _prewarm_tts_cache() -> None:
    """Synthesize the static greeting and filler phrases at startup so callers
    hear them instantly instead of waiting on a cold TTS request."""
    try:
        greeting = (
            f"Thanks for calling {settings.restaurant_name}! "
            "This call may be recorded. What can I get started for you?"
        )
        _speak_to_url(greeting, default_tenant.id)
        log.info("TTS cache pre-warmed with greeting")
    except Exception:
        log.warning("TTS pre-warm failed", exc_info=True)
    for filler in _FILLERS:
        try:
            _speak_to_url(filler, default_tenant.id)
        except Exception:
            log.warning("filler pre-warm failed: %r", filler)
    log.info("TTS cache pre-warmed with %d fillers", len(_FILLERS))


# Started last: every name above (including _speak_to_url) is defined.
threading.Thread(target=_prewarm_tts_cache, daemon=True).start()
