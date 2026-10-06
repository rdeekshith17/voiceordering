"""Tenant self-service portal: login, POS configuration, and a live,
tenant-isolated view of phone-call conversations.

Every data read is scoped by the tenant_id in the signed session cookie, so
a tenant can only ever see their own calls, orders, and settings.
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..tenants import crypto
from ..tenants.store import PortalUser, Tenant, TenantStore
from ..voice import tts
from ..voice import voices as voice_catalog
from ..voice.tts import TtsError

log = logging.getLogger("voiceorder.portal")

COOKIE_NAME = "vo_portal"

# Placeholder the UI renders in secret fields when a credential is stored.
# The browser submits it verbatim, so the server must treat it as "keep".
_SECRET_MASK = "saved"

# Voice-preview rate limiting: user_id -> [monotonic timestamps]. Previews
# spend ElevenLabs characters, so cap them per user per hour.
_preview_buckets: dict[str, list[float]] = {}

POS_PROVIDERS: dict[str, dict] = {
    "square": {
        "label": "Square",
        "fields": [
            {"key": "access_token", "label": "Access Token", "secret": True,
             "placeholder": "sq0atp-…", "help": "From your Square Developer Dashboard → your app → Production credentials."},
            {"key": "location_id", "label": "Location ID", "secret": False,
             "placeholder": "LP…", "help": "Square Dashboard → Locations. The restaurant location taking orders."},
            {"key": "environment", "label": "Environment", "secret": False,
             "options": ["production", "sandbox"], "default": "production"},
        ],
    },
    "toast": {
        "label": "Toast",
        "fields": [
            {"key": "client_id", "label": "Client ID", "secret": False,
             "help": "From Toast's developer portal (API client credentials)."},
            {"key": "client_secret", "label": "Client Secret", "secret": True},
            {"key": "restaurant_guid", "label": "Restaurant GUID", "secret": False,
             "help": "Your restaurant's location GUID in Toast admin."},
            {"key": "takeout_dining_guid", "label": "Takeout Dining Option GUID",
             "secret": False, "optional": True,
             "help": "Optional but recommended — routes orders as takeout automatically."},
            {"key": "environment", "label": "Environment", "secret": False,
             "options": ["production", "sandbox"], "default": "production"},
        ],
    },
    "clover": {
        "label": "Clover",
        "fields": [
            {"key": "access_token", "label": "API Token", "secret": True,
             "help": "Clover merchant dashboard → API tokens (needs Orders permission)."},
            {"key": "merchant_id", "label": "Merchant ID", "secret": False},
            {"key": "environment", "label": "Environment", "secret": False,
             "options": ["production", "sandbox"], "default": "production"},
        ],
    },
}

SETTING_FIELDS = [
    {"key": "restaurant_name", "label": "Restaurant name", "placeholder": "Hyderabad House"},
    {"key": "phone_number", "label": "Twilio phone number",
     "help": "The Twilio number customers call. Calls to this number are routed to your restaurant.",
     "placeholder": "+15622680097"},
    {"key": "transfer_number", "label": "Transfer number",
     "help": "When a caller asks for a human, the call is transferred here.",
     "placeholder": "+12832298041"},
    {"key": "pickup_minutes", "label": "Pickup time (minutes)", "placeholder": "20"},
    {"key": "tax_rate", "label": "Tax rate (e.g. 0.0825)", "placeholder": "0.0825"},
]

# Voice-picker fields are rendered as dedicated selects in the settings page
# (not the generic SETTING_FIELDS text inputs), but saved through the same
# /portal/api/settings endpoint. voice_id_custom is write-only: a pasted
# custom/cloned voice ID that overrides the dropdown on save.
VOICE_SETTING_KEYS = {"voice_id", "voice_model", "voice_id_custom"}


@dataclass
class PortalDeps:
    tenants: TenantStore
    order_store: Any
    build_adapter: Callable[[Tenant, dict | None], Any]
    """Build a POS adapter for a tenant. Second arg overrides stored secrets
    (used by 'Test connection' before saving). May raise on bad config."""
    get_catalog: Callable[[Tenant], Any]
    platform_name: str = "VoiceOrderAI"
    signup_enabled: bool = True
    on_config_changed: Callable[[str], None] = lambda tenant_id: None
    """Called after a tenant saves POS/settings so caches can be dropped."""


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
_CSS = """
:root{--bg:#0f1420;--card:#1a2233;--line:#2a3550;--txt:#e8edf7;--mut:#93a0bb;
--acc:#5b9dff;--ok:#3ecf8e;--warn:#ffb020;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--acc);text-decoration:none}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px 64px}
.topbar{background:#0b0f1a;border-bottom:1px solid var(--line);padding:12px 16px;
display:flex;align-items:center;gap:18px;position:sticky;top:0;z-index:5}
.topbar .brand{font-weight:700;font-size:18px}
.topbar nav{display:flex;gap:14px;margin-left:auto}
.topbar nav a{color:var(--mut)}.topbar nav a.on,.topbar nav a:hover{color:var(--txt)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:20px;margin:16px 0}
h1{font-size:24px;margin:6px 0 14px}h2{font-size:18px;margin:0 0 12px}
.mut{color:var(--mut)}.small{font-size:13px}
label{display:block;font-size:13px;color:var(--mut);margin:14px 0 6px}
input,select,textarea{width:100%;padding:10px 12px;border-radius:8px;
border:1px solid var(--line);background:#0f1626;color:var(--txt);font-size:15px}
button,.btn{display:inline-block;padding:10px 18px;border-radius:8px;border:0;
background:var(--acc);color:#fff;font-size:15px;cursor:pointer;margin-top:16px}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--txt)}
button:disabled{opacity:.5;cursor:default}
.tabs{display:flex;gap:8px;margin:8px 0 4px}
.tab{padding:8px 16px;border-radius:8px;border:1px solid var(--line);
background:transparent;color:var(--mut);cursor:pointer;margin:0}
.tab.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-size:12px;
background:#24304a;color:var(--mut)}
.pill.live{background:#123f2c;color:var(--ok)}
.pill.bad{background:#4a1f24;color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;font-size:12px;text-transform:uppercase}
.turn{border-left:3px solid var(--acc);padding:8px 12px;margin:12px 0;
background:#141c2e;border-radius:0 8px 8px 0}
.turn .who{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px}
.turn.caller{border-color:var(--ok)}
.alert{padding:12px 14px;border-radius:8px;margin:12px 0;font-size:14px}
.alert.ok{background:#123f2c;color:var(--ok)}.alert.bad{background:#4a1f24;color:var(--bad)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 24px}
@media(max-width:640px){.grid2{grid-template-columns:1fr}}
.stat{font-size:28px;font-weight:700}.statrow{display:flex;gap:16px;flex-wrap:wrap}
.statcard{flex:1;min-width:150px}
"""

def _layout(title: str, body: str, tenant: Tenant | None, active: str,
            platform: str) -> str:
    nav = ""
    if tenant:
        links = [("Dashboard", "/portal/", "dash"), ("POS setup", "/portal/pos", "pos"),
                 ("Live calls", "/portal/calls", "calls"),
                 ("Support", "/portal/support", "support"),
                 ("Marketing", "/portal/marketing", "marketing"),
                 ("Usage", "/portal/usage", "usage"),
                 ("Settings", "/portal/settings", "settings")]
        nav = "<nav>" + "".join(
            f'<a href="{u}" class="{"on" if active == k else ""}">{l}</a>'
            for l, u, k in links) + f'<a href="/portal/logout">Log out</a></nav>'
    tname = html.escape(tenant.name) if tenant else ""
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · {html.escape(platform)}</title>
<style>{_CSS}</style></head><body>
<div class=topbar><div class=brand>📞 {html.escape(platform)}</div>
{f'<span class="mut small">{tname}</span>' if tenant else ''}{nav}</div>
<div class=wrap>{body}</div></body></html>"""


def _login_page(platform: str, error: str = "", signup: bool = True) -> str:
    body = f"""<div class=card style="max-width:420px;margin:48px auto">
<h1>Log in</h1>
{f'<div class="alert bad">{html.escape(error)}</div>' if error else ''}
<form method=post action="/portal/login">
<label>Email</label><input name=email type=email required autocomplete=email>
<label>Password</label><input name=password type=password required autocomplete=current-password>
<button type=submit style="width:100%">Log in</button></form>
{"<p class='mut small'>New here? <a href='/portal/signup'>Create your restaurant account</a></p>" if signup else ""}
</div>"""
    return _layout("Log in", body, None, "", platform)


def _signup_page(platform: str, error: str = "") -> str:
    body = f"""<div class=card style="max-width:480px;margin:48px auto">
<h1>Create your restaurant account</h1>
{f'<div class="alert bad">{html.escape(error)}</div>' if error else ''}
<form method=post action="/portal/signup">
<label>Restaurant name</label><input name=restaurant required placeholder="Hyderabad House">
<label>Twilio phone number</label><input name=phone placeholder="+15622680097">
<p class="mut small" style="margin:4px 0 0">The Twilio number customers call — calls to it route to you.</p>
<label>Your email</label><input name=email type=email required autocomplete=email>
<label>Password (8+ characters)</label><input name=password type=password required minlength=8 autocomplete=new-password>
<button type=submit style="width:100%">Create account</button></form>
<p class="mut small"><a href="/portal/login">Already have an account? Log in</a></p></div>"""
    return _layout("Sign up", body, None, "", platform)

def _dashboard_page(deps: PortalDeps, tenant: Tenant) -> str:
    orders = deps.order_store.list_by_tenant(tenant.id, 8)
    calls = deps.tenants.list_calls(tenant.id, 5)
    live = [c for c in calls if c["status"] == "live"]
    provider = tenant.setting("pos_profile", "")
    connected = deps.tenants.has_secret(tenant.id, provider) if provider else False
    rows = "".join(
        f"<tr><td class=small>{html.escape(str(o.get('order_number', o.get('order_id', ''))))}</td>"
        f"<td>${(o.get('totals') or {}).get('total', 0):.2f}</td>"
        f"<td class='mut small'>{html.escape(str((o.get('payment') or {}).get('kind', '')))}</td>"
        f"<td class='mut small'>{time.strftime('%b %d %H:%M', time.localtime(o.get('saved_at', 0)))}</td></tr>"
        for o in orders
    ) or "<tr><td colspan=4 class=mut>No orders yet — they'll appear here as calls come in.</td></tr>"
    callrows = "".join(
        f"<tr><td><span class='pill {'live' if c['status']=='live' else ''}'>{c['status']}</span></td>"
        f"<td class=small>{html.escape(c['from_number'])}</td>"
        f"<td class=small>{c['turn_count']} turns</td>"
        f"<td><a href='/portal/calls/{c['call_sid']}'>View</a></td></tr>"
        for c in calls
    ) or "<tr><td colspan=4 class=mut>No calls yet.</td></tr>"
    body = f"""<h1>{html.escape(tenant.name)}</h1>
<div class="statrow">
<div class="card statcard"><div class=stat>{len(live)}</div><div class=mut>live calls</div></div>
<div class="card statcard"><div class=stat>{len(orders)}</div><div class=mut>recent orders</div></div>
<div class="card statcard"><div class=stat>{html.escape(POS_PROVIDERS.get(provider, {}).get('label', '—'))}</div>
<div class=mut>POS · {'<span style="color:var(--ok)">connected</span>' if connected else '<span style="color:var(--warn)">not configured</span>'}</div></div>
</div>
{"<div class='alert bad'>Your POS isn't connected yet — <a href='/portal/pos'>connect it</a> so orders flow into your system.</div>" if not connected else ""}
<div class=card><h2>Recent orders</h2><table>
<tr><th>Order</th><th>Total</th><th>Payment</th><th>When</th></tr>{rows}</table></div>
<div class=card><h2>Recent calls</h2><table>
<tr><th>Status</th><th>Caller</th><th>Length</th><th></th></tr>{callrows}</table>
<p><a href="/portal/calls">All calls →</a></p></div>"""
    return _layout("Dashboard", body, tenant, "dash", deps.platform_name)


def _settings_page(deps: PortalDeps, tenant: Tenant, saved: bool = False) -> str:
    fields = ""
    for f in SETTING_FIELDS:
        val = tenant.setting(f["key"], "")
        if f["key"] == "phone_number" and not val:
            val = tenant.phone_number
        fields += (f"<label>{f['label']}</label>"
                   f"<input name='{f['key']}' value='{html.escape(val)}'"
                   f" placeholder='{html.escape(f.get('placeholder', ''))}'>"
                   + (f"<p class='mut small' style='margin:4px 0 0'>{f['help']}</p>" if f.get("help") else ""))
    cur_voice = tenant.setting("voice_id", "") or voice_catalog.DEFAULT_VOICE_ID
    cur_model = tenant.setting("voice_model", "") or voice_catalog.DEFAULT_MODEL_ID
    known_ids = {v[0] for v in voice_catalog.ELEVENLABS_VOICES}
    custom_voice = "" if cur_voice in known_ids else cur_voice
    voice_opts = "".join(
        f"<option value='{vid}'{' selected' if vid == cur_voice else ''}>"
        f"{html.escape(name)} — {gender}, {age}, {accent}</option>"
        for vid, name, gender, age, accent in voice_catalog.ELEVENLABS_VOICES
    )
    model_opts = "".join(
        f"<option value='{mid}'{' selected' if mid == cur_model else ''}>"
        f"{html.escape(label)}</option>"
        for mid, label in voice_catalog.ELEVENLABS_MODELS
    )
    body = f"""<h1>Settings</h1>
{f'<div class="alert ok">Saved.</div>' if saved else ''}
<div id=settings-msg></div>
<form id=settings-form>
<div class=card><h2>Restaurant</h2>
<div class=grid2>{fields}</div></div>
<div class=card><h2>AI voice</h2>
<p class='mut small'>The voice callers hear when they phone your restaurant.
Preview a voice before saving — each preview uses a few dozen characters of your ElevenLabs free monthly budget.</p>
<label>Voice</label>
<select name='voice_id'>{voice_opts}</select>
<label>Or a custom / cloned voice ID</label>
<input name='voice_id_custom' value='{html.escape(custom_voice)}' placeholder='Paste a voice ID — overrides the selection above'>
<label>Model</label>
<select name='voice_model'>{model_opts}</select>
<div style='margin-top:10px'>
<button type=button id=voice-preview>Preview voice</button>
<audio id=voice-preview-audio controls style='display:none;margin-top:8px;width:100%'></audio>
<div id=voice-preview-msg style='margin-top:8px'></div>
</div></div>
<button type=submit>Save settings</button></form>
<script>
document.getElementById('settings-form').addEventListener('submit', async e => {{
  e.preventDefault();
  const fd = new FormData(e.target);
  const values = Object.fromEntries(fd.entries());
  const r = await fetch('/portal/api/settings', {{method:'POST',
    headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(values)}});
  const j = await r.json().catch(() => ({{}}));
  if (j.ok) location.href = '/portal/settings?saved=1';
  else document.getElementById('settings-msg').innerHTML =
    '<div class="alert bad">' + (j.error || 'Save failed') + '</div>';
}});
document.getElementById('voice-preview').addEventListener('click', async () => {{
  const btn = document.getElementById('voice-preview');
  const msg = document.getElementById('voice-preview-msg');
  const audio = document.getElementById('voice-preview-audio');
  btn.disabled = true; btn.textContent = 'Rendering…';
  msg.innerHTML = '';
  try {{
    const fd = new FormData(document.getElementById('settings-form'));
    const custom = (fd.get('voice_id_custom') || '').toString().trim();
    const r = await fetch('/portal/api/voice/preview', {{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{voice_id: custom || fd.get('voice_id'),
                             model_id: fd.get('voice_model')}})}});
    if (!r.ok) {{
      const j = await r.json().catch(() => ({{}}));
      throw new Error(j.error || ('Preview failed (' + r.status + ')'));
    }}
    const blob = await r.blob();
    audio.src = URL.createObjectURL(blob);
    audio.style.display = 'block';
    audio.play();
  }} catch (err) {{
    msg.innerHTML = '<div class="alert bad">' + err.message + '</div>';
  }}
  btn.disabled = false; btn.textContent = 'Preview voice';
}});
</script>"""
    return _layout("Settings", body, tenant, "settings", deps.platform_name)


def _pos_page(deps: PortalDeps, tenant: Tenant) -> str:
    import json as _json
    tabs = "".join(
        "<button type=button class='tab' data-p='%s'>%s</button>" % (p, spec["label"])
        for p, spec in POS_PROVIDERS.items())
    # NOTE: plain string (not f-string): the JS below is full of ${...}
    # template literals that would collide with f-string braces.
    body = """
<h1>POS setup</h1>
<div class=card><h2>Choose your point of sale</h2>
<p class=mut>Orders taken by the AI caller are pushed into your POS as they happen.
Credentials are encrypted before they're stored and are never shown back.</p>
<div class=tabs>__TABS__</div>
<div id=status></div>
<form id=pos-form><div id=fields></div>
<button type=submit id=save>Save credentials</button>
<button type=button class=ghost id=test>Test connection</button></form></div>
<div class=card><h2>How it works</h2>
<p class=mut>When a customer calls your Twilio number, the AI takes the order and
submits it to the POS you connect here &mdash; as an <b>open, visible order</b> your staff
can see immediately. Payment is collected at pickup unless your POS account
supports online payment links.</p></div>
<script>
const PROVIDERS = __PROVIDERS__;
let current = null, configured = null;
async function load() {
  const r = await fetch('/portal/api/pos'); const j = await r.json();
  current = j.provider || 'square'; configured = j;
  render(); select(current);
}
function render() {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('on', t.dataset.p === current));
  const st = document.getElementById('status');
  if (configured && configured.provider && configured.has_secret) {
    st.innerHTML = '<div class="alert ok">\u2713 ' + PROVIDERS[configured.provider].label +
      ' connected.</div>';
  } else {
    st.innerHTML = '<div class="alert bad" style="background:#3a2f14;color:var(--warn)">' +
      'No POS connected yet.</div>';
  }
}
function fieldHtml(f, v) {
  const shown = f.secret ? (v ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022' : '') : (v || '');
  let input;
  if (f.options) {
    input = '<select name="' + f.key + '">' + f.options.map(o =>
      '<option' + (o === (v || f.default) ? ' selected' : '') + '>' + o + '</option>').join('') + '</select>';
  } else {
    input = '<input name="' + f.key + '" value="' + shown.replace(/"/g, '&quot;') + '"' +
      (f.secret ? ' type=password autocomplete=new-password' : '') +
      ' placeholder="' + (f.placeholder || '') + '"' +
      ((f.secret && v) ? ' data-keep=1' : '') + '>';
  }
  let h = '<label>' + f.label + (f.optional ? ' <span class=mut>(optional)</span>' : '') + '</label>' + input;
  if (f.help) h += '<p class="mut small" style="margin:4px 0 0">' + f.help + '</p>';
  if (f.secret && v) h += '<p class="mut small" style="margin:4px 0 0">Saved &mdash; leave blank to keep.</p>';
  return h;
}
function select(p) {
  current = p; render();
  const spec = PROVIDERS[p];
  const saved = (configured && configured.provider === p) ? configured.values : {};
  document.getElementById('fields').innerHTML =
    spec.fields.map(f => fieldHtml(f, saved[f.key] || '')).join('');
  document.querySelectorAll('#fields input[data-keep]').forEach(i =>
    i.addEventListener('input', () => i.removeAttribute('data-keep')));
}
document.querySelectorAll('.tab').forEach(t =>
  t.addEventListener('click', () => select(t.dataset.p)));
async function collect() {
  const f = document.getElementById('pos-form'); const fd = new FormData(f);
  const values = {};
  fd.forEach((v, k) => {
    const inp = f.elements[k];
    if (inp && inp.dataset.keep) return;
    values[k] = v;
  });
  return values;
}
document.getElementById('pos-form').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('save');
  btn.disabled = true; btn.textContent = 'Saving\u2026';
  const r = await fetch('/portal/api/pos', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({provider: current, values: await collect()})});
  const j = await r.json();
  btn.disabled = false; btn.textContent = 'Save credentials';
  document.getElementById('status').innerHTML = j.ok
    ? '<div class="alert ok">\u2713 Saved.</div>'
    : '<div class="alert bad">' + (j.error || 'Save failed') + '</div>';
  if (j.ok) load();
});
document.getElementById('test').addEventListener('click', async () => {
  const btn = document.getElementById('test');
  btn.disabled = true; btn.textContent = 'Testing\u2026';
  const r = await fetch('/portal/api/pos/test', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({provider: current, values: await collect()})});
  const j = await r.json();
  btn.disabled = false; btn.textContent = 'Test connection';
  document.getElementById('status').innerHTML = j.ok
    ? '<div class="alert ok">\u2713 Connection works' + (j.detail ? ' &mdash; ' + j.detail : '') + '.</div>'
    : '<div class="alert bad">\u2717 ' + (j.error || 'Connection failed') + '</div>';
});
load();
</script>
""".replace("__TABS__", tabs).replace("__PROVIDERS__", _json.dumps(POS_PROVIDERS))
    return _layout("POS setup", body, tenant, "pos", deps.platform_name)



def _calls_page(deps: PortalDeps, tenant: Tenant) -> str:
    body = """<h1>Calls</h1>
<div class=card><h2>Live & recent <span class="mut small">(auto-refreshes)</span></h2>
<table><thead><tr><th>Status</th><th>Caller</th><th>Started</th><th>Turns</th><th></th></tr></thead>
<tbody id=rows><tr><td colspan=5 class=mut>Loading…</td></tr></tbody></table></div>
<script>
async function load() {
  const r = await fetch('/portal/api/calls'); const j = await r.json();
  const tb = document.getElementById('rows');
  if (!j.calls.length) { tb.innerHTML = '<tr><td colspan=5 class=mut>No calls yet.</td></tr>'; return; }
  tb.innerHTML = j.calls.map(c => `<tr>
    <td><span class="pill ${c.status==='live'?'live':''}">${c.status}</span></td>
    <td class=small>${c.from_number||'—'}</td>
    <td class="mut small">${new Date(c.started_at*1000).toLocaleString()}</td>
    <td class=small>${c.turn_count}</td>
    <td><a href="/portal/calls/${c.call_sid}">View</a></td></tr>`).join('');
}
load(); setInterval(load, 3000);
</script>"""
    return _layout("Calls", body, tenant, "calls", deps.platform_name)


def _call_detail_page(deps: PortalDeps, tenant: Tenant, call_sid: str) -> str:
    body = f"""<p><a href="/portal/calls">← All calls</a></p><h1>Call</h1>
<div class=card><div id=head class=mut>Loading…</div><div id=turns></div></div>
<script>
const SID = {__import__('json').dumps(call_sid)};
async function load() {{
  const r = await fetch('/portal/api/calls/' + SID);
  if (r.status === 404) {{ document.getElementById('head').textContent = 'Call not found.'; return; }}
  const j = await r.json();
  document.getElementById('head').innerHTML =
    `<span class="pill ${{j.status==='live'?'live':''}}">${{j.status}}</span>
     <span class=small> from ${{j.from_number||'—'}} · started
     ${{new Date(j.started_at*1000).toLocaleString()}}</span>`;
  const el = document.getElementById('turns');
  el.innerHTML = j.turns.length ? j.turns.map(t => `
    <div class="turn caller"><div class=who>Caller · ${{new Date(t.ts*1000).toLocaleTimeString()}}</div>
      ${{t.heard||'<i class=mut>(no speech captured)</i>'}}</div>
    <div class=turn><div class=who>Assistant${{t.tools&&t.tools.length?' · '+t.tools.map(x=>x.name).join(', '):''}}</div>
      ${{t.reply}}</div>`).join('')
    : '<p class=mut>No conversation yet.</p>';
  el.scrollTop = el.scrollHeight;
}}
load(); setInterval(load, 2000);
</script>"""
    return _layout("Call", body, tenant, "calls", deps.platform_name)


_SEV_PILL = {"critical": "bad", "warning": "", "info": "live"}
_CHANNEL_PILL = {"sms": "live", "social": "", "in-store": ""}


def _support_page(deps: PortalDeps, tenant: Tenant) -> str:
    tickets = deps.tenants.list_tickets(tenant.id)
    if not tickets:
        rows = '<tr><td colspan=4 class=mut>No tickets — everything looks healthy.</td></tr>'
    else:
        parts = []
        for t in tickets:
            sev = _SEV_PILL.get(t["severity"], "")
            plat = ' <span class="pill">Platform</span>' if not t["tenant_id"] else ""
            status = t["status"]
            sp = "bad" if status == "open" else ("live" if status == "resolved" else "")
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created_at"]))
            action = ""
            if status == "open" and t["tenant_id"]:
                action = (f'<form method=post action="/portal/api/tickets/{t["id"]}/ack"'
                          f' style="margin:0"><button class=ghost type=submit'
                          f' style="margin:0;padding:6px 12px;font-size:13px">'
                          f'Acknowledge</button></form>')
            parts.append(
                f"<tr><td><span class='pill {sev}'>{html.escape(t['severity'])}</span>{plat}</td>"
                f"<td><b>{html.escape(t['title'])}</b>"
                f"<div class='mut small'>{html.escape(t['detail'])[:200]}</div></td>"
                f"<td><span class='pill {sp}'>{html.escape(status)}</span>"
                f"<div class='mut small'>{when}</div></td>"
                f"<td>{action}</td></tr>")
        rows = "".join(parts)
    body = f"""<h1>Support</h1>
<div class=card><h2>Tickets <span class="mut small">open first, then recently resolved</span></h2>
<table><thead><tr><th>Severity</th><th>Issue</th><th>Status</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></div>"""
    return _layout("Support", body, tenant, "support", deps.platform_name)


def _marketing_page(deps: PortalDeps, tenant: Tenant) -> str:
    drafts = deps.tenants.list_drafts(tenant.id)
    if not drafts:
        cards = ('<div class=card><p class=mut>No drafts yet. The marketing agent '
                 'generates fresh promo ideas from your menu and best sellers every week.</p></div>')
    else:
        parts = []
        for d in drafts:
            pill = _CHANNEL_PILL.get(d["channel"], "")
            when = time.strftime("%Y-%m-%d", time.localtime(d["created_at"]))
            parts.append(
                f"""<div class=card><h2>{html.escape(d['title'])}
<span class="pill {pill}">{html.escape(d['channel'])}</span>
<span class="mut small"> · {when} · {html.escape(d['status'])}</span></h2>
<p style="white-space:pre-wrap">{html.escape(d['body'])}</p></div>""")
        cards = "".join(parts)
    body = (f"<h1>Marketing</h1>"
            f"<p class=mut>Promo drafts from your real menu and best sellers. "
            f"Drafts only — nothing is ever sent automatically.</p>{cards}")
    return _layout("Marketing", body, tenant, "marketing", deps.platform_name)


def _usage_page(deps: PortalDeps, tenant: Tenant) -> str:
    rows_data = deps.tenants.get_usage(tenant.id)
    if not rows_data:
        rows = '<tr><td colspan=5 class=mut>No usage recorded yet.</td></tr>'
    else:
        rows = "".join(
            f"<tr><td>{html.escape(r['date'])}</td><td>{r['calls']}</td>"
            f"<td>{r['talk_minutes']:.1f}</td><td>{r['tts_chars']}</td>"
            f"<td>{r['sms_sent']}</td></tr>"
            for r in rows_data)
    body = f"""<h1>Usage</h1>
<div class=card><h2>Daily usage <span class="mut small">newest first</span></h2>
<table><thead><tr><th>Date</th><th>Calls</th><th>Talk min</th><th>TTS chars</th><th>SMS sent</th></tr></thead>
<tbody>{rows}</tbody></table>
<p class="mut small">Talk minutes are wall-clock call durations; TTS chars count spoken reply text.</p></div>"""
    return _layout("Usage", body, tenant, "usage", deps.platform_name)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def _session_user(request: Request, deps: PortalDeps) -> PortalUser | None:
    raw = request.cookies.get(COOKIE_NAME, "")
    data = crypto.read_session_cookie(raw) if raw else None
    if not data:
        return None
    user = deps.tenants.get_user(data.get("uid", ""))
    if not user or user.tenant_id != data.get("tid"):
        return None
    return user


def _set_login_cookie(resp: RedirectResponse, user: PortalUser) -> None:
    resp.set_cookie(
        COOKIE_NAME,
        crypto.make_session_cookie({"uid": user.id, "tid": user.tenant_id}),
        max_age=86400 * 7, httponly=True, samesite="lax", path="/",
    )


def build_portal_router(deps: PortalDeps) -> APIRouter:
    router = APIRouter()

    def page_user(request: Request) -> tuple[PortalUser, Tenant] | RedirectResponse:
        user = _session_user(request, deps)
        if not user:
            return RedirectResponse("/portal/login", status_code=302)
        tenant = deps.tenants.get_tenant(user.tenant_id)
        if not tenant:
            r = RedirectResponse("/portal/login", status_code=302)
            r.delete_cookie(COOKIE_NAME, path="/")
            return r
        return user, tenant

    def api_user(request: Request) -> tuple[PortalUser, Tenant]:
        user = _session_user(request, deps)
        if not user:
            raise HTTPException(status_code=401, detail="login required")
        tenant = deps.tenants.get_tenant(user.tenant_id)
        if not tenant:
            raise HTTPException(status_code=401, detail="login required")
        return user, tenant

    # -- auth pages ------------------------------------------------------
    @router.get("/portal/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if _session_user(request, deps):
            return RedirectResponse("/portal/", status_code=302)
        return _login_page(deps.platform_name, signup=deps.signup_enabled)

    @router.post("/portal/login")
    async def login_post(request: Request):
        form = await request.form()
        user = deps.tenants.verify_user(
            str(form.get("email", "")), str(form.get("password", ""))
        )
        if not user:
            return HTMLResponse(
                _login_page(deps.platform_name, "Invalid email or password.",
                            signup=deps.signup_enabled),
                status_code=401,
            )
        resp = RedirectResponse("/portal/", status_code=302)
        _set_login_cookie(resp, user)
        return resp

    @router.get("/portal/signup", response_class=HTMLResponse)
    async def signup_page(request: Request):
        if not deps.signup_enabled:
            raise HTTPException(status_code=404)
        if _session_user(request, deps):
            return RedirectResponse("/portal/", status_code=302)
        return _signup_page(deps.platform_name)

    @router.post("/portal/signup")
    async def signup_post(request: Request):
        if not deps.signup_enabled:
            raise HTTPException(status_code=404)
        form = await request.form()
        try:
            phone = str(form.get("phone", ""))
            name = str(form.get("restaurant", "")).strip() or "My Restaurant"
            # Claim flow: if a restaurant with this number already exists and
            # has no portal users yet (e.g. seeded by the platform), the new
            # account joins it instead of creating a duplicate tenant.
            tenant = (deps.tenants.get_tenant_by_number(phone)
                      if phone else None)
            if tenant is not None and deps.tenants.count_users(tenant.id) > 0:
                tenant = None
            if tenant is None:
                tenant = deps.tenants.create_tenant(name, phone)
                deps.tenants.set_settings(tenant.id, {
                    "restaurant_name": tenant.name,
                    "pos_profile": "",
                })
            user = deps.tenants.create_user(
                tenant.id, str(form.get("email", "")), str(form.get("password", ""))
            )
        except ValueError as exc:
            return HTMLResponse(_signup_page(deps.platform_name, str(exc)),
                                status_code=400)
        tenant = deps.tenants.get_tenant(tenant.id)
        resp = RedirectResponse("/portal/pos", status_code=302)
        _set_login_cookie(resp, user)
        log.info("portal signup: tenant %s (%s)", tenant.id, tenant.name)
        return resp

    @router.get("/portal/logout")
    async def logout():
        resp = RedirectResponse("/portal/login", status_code=302)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    # -- pages -----------------------------------------------------------
    @router.get("/portal/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _dashboard_page(deps, tenant)

    @router.get("/portal/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: int = 0):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _settings_page(deps, tenant, saved=bool(saved))

    @router.get("/portal/pos", response_class=HTMLResponse)
    async def pos_page(request: Request):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _pos_page(deps, tenant)

    @router.get("/portal/calls", response_class=HTMLResponse)
    async def calls_page(request: Request):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _calls_page(deps, tenant)

    @router.get("/portal/calls/{call_sid}", response_class=HTMLResponse)
    async def call_detail_page(request: Request, call_sid: str):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        if not deps.tenants.get_transcript(tenant.id, call_sid):
            raise HTTPException(status_code=404, detail="call not found")
        return _call_detail_page(deps, tenant, call_sid)

    @router.get("/portal/support", response_class=HTMLResponse)
    async def support_page(request: Request):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _support_page(deps, tenant)

    @router.get("/portal/marketing", response_class=HTMLResponse)
    async def marketing_page(request: Request):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _marketing_page(deps, tenant)

    @router.get("/portal/usage", response_class=HTMLResponse)
    async def usage_page(request: Request):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _usage_page(deps, tenant)

    @router.post("/portal/api/tickets/{ticket_id}/ack")
    async def api_ticket_ack(request: Request, ticket_id: str):
        _, tenant = api_user(request)
        if not deps.tenants.ack_ticket(tenant.id, ticket_id):
            raise HTTPException(status_code=404, detail="ticket not found")
        return RedirectResponse("/portal/support", status_code=302)

    # -- JSON API --------------------------------------------------------
    @router.get("/portal/api/calls")
    async def api_calls(request: Request, limit: int = 30):
        _, tenant = api_user(request)
        return {"calls": deps.tenants.list_calls(tenant.id, min(limit, 100))}

    @router.get("/portal/api/calls/{call_sid}")
    async def api_call_detail(request: Request, call_sid: str):
        _, tenant = api_user(request)
        t = deps.tenants.get_transcript(tenant.id, call_sid)
        if not t:
            raise HTTPException(status_code=404, detail="call not found")
        return t

    @router.get("/portal/api/orders")
    async def api_orders(request: Request, limit: int = 30):
        _, tenant = api_user(request)
        orders = deps.order_store.list_by_tenant(tenant.id, min(limit, 100))
        return {"orders": [
            {"order_id": o.get("order_id"), "order_number": o.get("order_number"),
             "totals": o.get("totals"), "payment": o.get("payment"),
             "saved_at": o.get("saved_at")}
            for o in orders
        ]}

    @router.get("/portal/api/tickets")
    async def api_tickets(request: Request):
        _, tenant = api_user(request)
        return {"tickets": deps.tenants.list_tickets(tenant.id)}

    @router.get("/portal/api/marketing")
    async def api_marketing(request: Request, limit: int = 20):
        _, tenant = api_user(request)
        return {"drafts": deps.tenants.list_drafts(tenant.id, min(limit, 100))}

    @router.get("/portal/api/usage")
    async def api_usage(request: Request, limit: int = 30):
        _, tenant = api_user(request)
        return {"usage": deps.tenants.get_usage(tenant.id, min(limit, 90))}

    def _pos_view(tenant: Tenant) -> dict:
        provider = tenant.setting("pos_profile", "")
        if provider not in POS_PROVIDERS:
            return {"provider": "", "has_secret": False, "values": {}}
        stored = deps.tenants.get_secret(tenant.id, provider)
        values = {}
        for f in POS_PROVIDERS[provider]["fields"]:
            if f.get("secret"):
                values[f["key"]] = _SECRET_MASK if stored.get(f["key"]) else ""
            else:
                values[f["key"]] = stored.get(f["key"], "")
        return {"provider": provider,
                "has_secret": deps.tenants.has_secret(tenant.id, provider),
                "values": values}

    @router.get("/portal/api/pos")
    async def api_pos_get(request: Request):
        _, tenant = api_user(request)
        return _pos_view(tenant)

    def _merged_creds(tenant: Tenant, provider: str, values: dict) -> tuple[dict, str]:
        """Merge submitted values over stored secrets.

        Blank fields -- and the "saved" mask the UI renders for stored
        secrets -- keep the stored value. Without the mask check, clicking
        "Test connection" or "Save credentials" with an untouched secret
        field would send the literal word "saved" as the credential.
        """
        if provider not in POS_PROVIDERS:
            return {}, "unknown provider"
        stored = deps.tenants.get_secret(tenant.id, provider)
        merged = dict(stored)
        for k, v in (values or {}).items():
            v = str(v or "").strip()
            if v and v != _SECRET_MASK:
                merged[k] = v
        for f in POS_PROVIDERS[provider]["fields"]:
            if not f.get("optional") and not merged.get(f["key"]):
                return {}, f"missing {f['label']}"
        return merged, ""

    @router.post("/portal/api/pos")
    async def api_pos_save(request: Request):
        _, tenant = api_user(request)
        body = await request.json()
        provider = str(body.get("provider", ""))
        merged, err = _merged_creds(tenant, provider, body.get("values"))
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        deps.tenants.set_secret(tenant.id, provider, merged)
        deps.tenants.set_settings(tenant.id, {"pos_profile": provider})
        deps.on_config_changed(tenant.id)
        log.info("portal: tenant %s saved %s credentials", tenant.id, provider)
        return {"ok": True}

    @router.post("/portal/api/pos/test")
    async def api_pos_test(request: Request):
        _, tenant = api_user(request)
        body = await request.json()
        provider = str(body.get("provider", ""))
        merged, err = _merged_creds(tenant, provider, body.get("values"))
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        try:
            adapter = deps.build_adapter(tenant, {provider: merged})
            detail = adapter.ping()
        except Exception as exc:  # show the vendor's reason, not a traceback
            log.warning("portal: tenant %s %s test failed: %s", tenant.id, provider, exc)
            return JSONResponse({"ok": False, "error": str(exc)[:300]}, status_code=200)
        nice = detail.get("location_name") or detail.get("merchant_name") or ""
        return {"ok": True, "detail": f"connected to {nice}" if nice else "credentials accepted"}

    @router.post("/portal/api/settings")
    async def api_settings_save(request: Request):
        _, tenant = api_user(request)
        body = await request.json()
        allowed = {f["key"] for f in SETTING_FIELDS} | VOICE_SETTING_KEYS
        values = {k: str(v).strip() for k, v in body.items() if k in allowed}
        # A pasted custom/cloned voice ID wins over the dropdown selection.
        custom_voice = values.pop("voice_id_custom", "")
        if custom_voice:
            values["voice_id"] = custom_voice
        if "voice_id" in values and not voice_catalog.is_known_voice(values["voice_id"]):
            return JSONResponse({"ok": False, "error": "that voice ID doesn't look valid"},
                                status_code=400)
        if "voice_model" in values and not voice_catalog.is_known_model(values["voice_model"]):
            return JSONResponse({"ok": False, "error": "unknown voice model"}, status_code=400)
        if "pickup_minutes" in values:
            try:
                values["pickup_minutes"] = str(max(5, int(values["pickup_minutes"])))
            except ValueError:
                values.pop("pickup_minutes")
        if "tax_rate" in values:
            try:
                r = float(values["tax_rate"])
                values["tax_rate"] = str(r if 0 <= r < 1 else 0.0825)
            except ValueError:
                values.pop("tax_rate")
        if values.get("phone_number"):
            deps.tenants.set_phone_number(tenant.id, values["phone_number"])
        deps.tenants.set_settings(tenant.id, values)
        deps.on_config_changed(tenant.id)
        return {"ok": True}

    @router.post("/portal/api/voice/preview")
    async def api_voice_preview(request: Request):
        """Render a short sample with the selected (not yet saved) voice and
        model so owners can hear it before committing. Session-authenticated;
        rate-limited per user because every preview spends ElevenLabs chars."""
        user, tenant = api_user(request)
        now = time.monotonic()
        bucket = _preview_buckets.get(user.id, [])
        bucket = [t for t in bucket if now - t < 3600]
        if len(bucket) >= 20:
            return JSONResponse(
                {"ok": False, "error": "preview limit reached (20/hour) — try again later"},
                status_code=429)
        body = await request.json()
        voice_id = (str(body.get("voice_id") or "").strip()
                    or tenant.setting("voice_id", "")
                    or voice_catalog.DEFAULT_VOICE_ID)
        model_id = (str(body.get("model_id") or "").strip()
                    or tenant.setting("voice_model", "")
                    or voice_catalog.DEFAULT_MODEL_ID)
        if not voice_catalog.is_known_voice(voice_id):
            return JSONResponse({"ok": False, "error": "that voice ID doesn't look valid"},
                                status_code=400)
        if not voice_catalog.is_known_model(model_id):
            return JSONResponse({"ok": False, "error": "unknown voice model"}, status_code=400)
        text = (f"Hello! Thanks for calling {tenant.name}. "
                "This is how I'll sound when I take your orders.")
        try:
            audio = tts.synthesize(text, voice_id=voice_id, model_id=model_id)
        except TtsError as exc:
            msg = str(exc)
            if "402" in msg:
                msg = ("That voice isn't available on the free ElevenLabs plan. "
                       "Pick another voice, or upgrade ElevenLabs to unlock it.")
            log.warning("portal: tenant %s voice preview failed: %s", tenant.id, exc)
            return JSONResponse({"ok": False, "error": msg[:300]}, status_code=502)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=400)
        bucket.append(now)
        _preview_buckets[user.id] = bucket
        return Response(content=audio, media_type="audio/mpeg",
                        headers={"X-Voice-Id": voice_id, "X-Voice-Model": model_id})

    return router
