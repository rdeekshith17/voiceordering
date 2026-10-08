"""Tenant self-service portal: login, POS configuration, and a live,
tenant-isolated view of phone-call conversations.

Every data read is scoped by the tenant_id in the signed session cookie, so
a tenant can only ever see their own calls, orders, and settings.
"""
from __future__ import annotations

import csv
import html
import io
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from ..tenants import crypto
from ..tenants.store import PortalUser, Tenant, TenantStore
from ..voice import tts
from ..voice import voices as voice_catalog
from ..voice.tts import TtsError
from . import ui
from .ui import icon

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

# Zones a restaurant can pick (signup + System settings). Each restaurant's
# zone sets the pickup times the AI tells callers and the times in the portal.
# Empty = the server's clock.
TIMEZONES: list[tuple[str, str]] = [
    ("America/New_York", "Eastern Time (New York, Atlanta, Miami)"),
    ("America/Chicago", "Central Time (Chicago, Dallas, Houston)"),
    ("America/Denver", "Mountain Time (Denver, Salt Lake City)"),
    ("America/Phoenix", "Arizona (Phoenix, no daylight saving)"),
    ("America/Los_Angeles", "Pacific Time (Los Angeles, Seattle)"),
    ("America/Anchorage", "Alaska (Anchorage)"),
    ("Pacific/Honolulu", "Hawaii (Honolulu)"),
    ("America/Halifax", "Atlantic Time (Halifax)"),
    ("America/St_Johns", "Newfoundland (St. John's)"),
    ("America/Toronto", "Eastern Time, Canada (Toronto)"),
    ("America/Vancouver", "Pacific Time, Canada (Vancouver)"),
    ("Europe/London", "UK (London)"),
    ("Asia/Kolkata", "India (Kolkata, Hyderabad)"),
    ("Asia/Dubai", "UAE (Dubai)"),
    ("Asia/Singapore", "Singapore"),
    ("Australia/Sydney", "Australia Eastern (Sydney)"),
    ("UTC", "UTC"),
]
TIMEZONE_IDS = {z for z, _ in TIMEZONES}


def _tz_options(selected: str, blank_label: str) -> str:
    opts = f"<option value=''>{_e(blank_label)}</option>" if blank_label else ""
    return opts + "".join(
        f"<option value='{z}'{' selected' if z == selected else ''}>{_e(label)}</option>"
        for z, label in TIMEZONES)


# Preselects the visitor's own zone in a blank time-zone <select> (if offered).
_TZ_GUESS_JS = """<script>(function(){try{
  var s=document.querySelector('select[name=timezone]'); if(!s||s.value) return;
  var z=Intl.DateTimeFormat().resolvedOptions().timeZone;
  if([].some.call(s.options,function(o){return o.value===z;})){s.value=z;
    var n=document.getElementById('tz-guess'); if(n) n.hidden=false;}
}catch(e){}})();</script>"""

# A call still marked live after this long without a turn lost its status webhook.
_LIVE_STALE_SECONDS = 1800


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
# Data helpers
# --------------------------------------------------------------------------
def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _tz(tenant: Tenant) -> tzinfo | None:
    """The tenant's chosen zone, or None for the server's local clock."""
    name = tenant.setting("timezone", "")
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            log.warning("tenant %s: unknown time zone %r", tenant.id, name)
    return None


def _dt(ts: float, tz: tzinfo | None) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def _day_start(ts: float, tz: tzinfo | None) -> float:
    return _dt(ts, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _hm(d: datetime) -> str:
    return d.strftime("%I:%M %p").lstrip("0")


def _clock(ts: float | None, tz: tzinfo | None, kind: str = "time") -> str:
    """A timestamp as HTML: server-formatted in the tenant zone, or a <time>
    the browser fills in with the viewer's clock when no zone is set."""
    if not ts:
        return "—"
    if tz is None:
        return f'<time data-ts="{float(ts):.0f}" data-f="{kind}"></time>'
    d = _dt(ts, tz)
    if kind == "date":
        return d.strftime("%b %d, %Y")
    if kind == "datetime":
        return f"{d.strftime('%b %d')} · {_hm(d)}"
    return _hm(d)


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _dur(seconds: float) -> str:
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _load_orders(deps: PortalDeps, tenant: Tenant, limit: int = 5000) -> list[dict]:
    try:
        return deps.order_store.list_by_tenant(tenant.id, limit)
    except Exception:
        log.warning("portal: order load failed for tenant %s", tenant.id, exc_info=True)
        return []


def _order_total(o: dict) -> float:
    try:
        return float((o.get("totals") or {}).get("total") or 0)
    except (TypeError, ValueError):
        return 0.0


def _order_items(o: dict) -> str:
    parts = []
    for line in o.get("lines") or []:
        name = str(line.get("item_name") or "").strip()
        if not name:
            continue
        qty = int(line.get("quantity") or 1)
        parts.append(f"{name} (×{qty})" if qty > 1 else name)
    return ", ".join(parts)


def _order_no(o: dict) -> str:
    ref = o.get("order_number") or (o.get("order_id") or "")[:8]
    return f"#{ref}" if ref else "—"


def _order_status(o: dict) -> tuple[str, str]:
    """(label, pill class) for an order's POS status."""
    s = str(o.get("status") or "submitted").lower()
    if s in ("completed", "ready", "paid", "closed", "submitted"):
        cls = "ok"
    elif s in ("preparing", "in_progress", "open", "accepted"):
        cls = "warn"
    elif s in ("failed", "canceled", "cancelled", "rejected", "error"):
        cls = "bad"
    else:
        cls = ""
    return s.replace("_", " "), cls


def _payment_label(o: dict) -> str:
    return "Pay link" if (o.get("payment") or {}).get("kind") == "link" else "At pickup"


def _delta(cur: float, prev: float) -> str:
    if prev <= 0:
        return ""
    pct = (cur - prev) / prev * 100
    return f'<span class="d {"up" if pct >= 0 else "down"}">{pct:+.1f}%</span>'


def _is_live(call: dict, now: float) -> bool:
    return call.get("status") == "live" and now - (call.get("updated_at") or 0) < _LIVE_STALE_SECONDS


def _pos_state(deps: PortalDeps, tenant: Tenant) -> tuple[str, bool, str]:
    """(provider label, connected, environment) for the tenant's POS."""
    provider = tenant.setting("pos_profile", "")
    if provider not in POS_PROVIDERS:
        return (provider.replace("_", " ").title() if provider else "", False, "")
    try:
        creds = deps.tenants.get_secret(tenant.id, provider)
    except Exception:
        creds = {}
    return POS_PROVIDERS[provider]["label"], bool(creds), str(creds.get("environment", ""))


def _month_usage(deps: PortalDeps, tenant: Tenant, now: float, tz: tzinfo | None) -> dict:
    month = _dt(now, tz).strftime("%Y-%m")
    rows = [r for r in deps.tenants.get_usage(tenant.id, 31) if str(r["date"]).startswith(month)]
    return {
        "calls": sum(int(r["calls"] or 0) for r in rows),
        "talk_minutes": sum(float(r["talk_minutes"] or 0) for r in rows),
        "tts_chars": sum(int(r["tts_chars"] or 0) for r in rows),
        "sms_sent": sum(int(r["sms_sent"] or 0) for r in rows),
    }


def _page(deps: PortalDeps, tenant: Tenant, active: str, title: str, body: str) -> str:
    now = time.time()
    live = sum(1 for c in deps.tenants.list_calls(tenant.id, 50) if _is_live(c, now))
    label, connected, _ = _pos_state(deps, tenant)
    return ui.shell(
        title=title, body=body, tenant_name=tenant.name, active=active,
        platform=deps.platform_name, live_calls=live,
        status_label="Live workspace" if connected else "Setup needed",
        status_ok=connected,
    )


def _page_head(title: str, sub: str, actions: str = "") -> str:
    acts = f'<div class="acts">{actions}</div>' if actions else ""
    return f'<div class="page-h"><div><h2>{_e(title)}</h2><p>{sub}</p></div>{acts}</div>'


def _orders_table(orders: list[dict], tz: tzinfo | None, with_time: bool) -> str:
    if not orders:
        return '<div class="empty">No orders yet — they appear here as calls come in.</div>'
    rows = []
    for o in orders:
        label, cls = _order_status(o)
        items = _order_items(o)
        when = f"<td class='mut'>{_clock(o.get('saved_at'), tz, 'datetime')}</td>" if with_time else ""
        search = f"{_order_no(o)} {items}".lower()
        rows.append(
            f'<tr data-status="{_e(label)}" data-q="{_e(search)}">'
            f'<td class="id">{_e(_order_no(o))}</td>'
            f'<td>{_e(items) or "<span class=mut>—</span>"}</td>'
            f'<td>{_money(_order_total(o))}</td>'
            f'<td><span class="pill {cls}">{_e(label)}</span></td>{when}</tr>')
    head = "<th>Time</th>" if with_time else ""
    return (f'<div class="tbl-wrap"><table><thead><tr><th>Order ID</th><th>Items</th><th>Total</th>'
            f'<th>Status</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------
def _login_page(platform: str, error: str = "", signup: bool = True) -> str:
    body = f"""<h2>Log in</h2><p class="mut" style="margin:0 0 24px">Manage your restaurant's AI phone ordering.</p>
{f'<div class="alert bad">{_e(error)}</div>' if error else ''}
<form method=post action="/portal/login">
<label class="f">Email</label><input name=email type=email required autocomplete=email>
<label class="f">Password</label><input name=password type=password required autocomplete=current-password>
<div class="actions"><button class="btn block" type=submit>Log in</button></div></form>
{"<p class='mut small' style='margin:20px 0 0'>New here? <a href='/portal/signup'>Create your restaurant account</a></p>" if signup else ""}"""
    return ui.auth_shell("Log in", body, platform)


def _signup_page(platform: str, error: str = "") -> str:
    body = f"""<h2>Create your restaurant account</h2>
<p class="mut" style="margin:0 0 24px">Takes a minute. You'll connect your POS next.</p>
{f'<div class="alert bad">{_e(error)}</div>' if error else ''}
<form method=post action="/portal/signup">
<label class="f">Restaurant name</label><input name=restaurant required placeholder="Hyderabad House">
<label class="f">Twilio phone number</label><input name=phone placeholder="+15622680097">
<p class="help">The Twilio number customers call — calls to it route to you.</p>
<label class="f">Time zone</label><select name=timezone required>{_tz_options("", "Choose your restaurant's time zone")}</select>
<p class="help">Pickup times the AI tells your callers use this zone.</p>
<label class="f">Your email</label><input name=email type=email required autocomplete=email>
<label class="f">Password (8+ characters)</label><input name=password type=password required minlength=8 autocomplete=new-password>
<div class="actions"><button class="btn block" type=submit>Create account</button></div></form>
<p class="mut small" style="margin:20px 0 0"><a href="/portal/login">Already have an account? Log in</a></p>
{_TZ_GUESS_JS}"""
    return ui.auth_shell("Sign up", body, platform)


def _dashboard_page(deps: PortalDeps, tenant: Tenant) -> str:
    tz, now = _tz(tenant), time.time()
    orders = _load_orders(deps, tenant)
    d30, d60 = now - 30 * 86400, now - 60 * 86400
    cur = [o for o in orders if (o.get("saved_at") or 0) >= d30]
    prev = [o for o in orders if d60 <= (o.get("saved_at") or 0) < d30]
    rev, rev_prev = sum(map(_order_total, cur)), sum(map(_order_total, prev))

    calls = deps.tenants.list_calls(tenant.id, 6)
    live = [c for c in calls if _is_live(c, now)]
    calls_today = deps.tenants.calls_in_window(tenant.id, _day_start(now, tz))
    pos_label, connected, _ = _pos_state(deps, tenant)

    if connected:
        pos_card = (f'<div class="card kpi ok"><div class="l">POS connection</div>'
                    f'<div class="status-line"><span class="dot"></span>{_e(pos_label)} active</div></div>')
    else:
        pos_card = (f'<div class="card kpi warn"><div class="l">POS connection</div>'
                    f'<div class="status-line"><span class="dot warn"></span>Not connected</div>'
                    f'<p class="small" style="margin:12px 0 0"><a href="/portal/pos">Connect your POS →</a></p></div>')
    live_note = (f'<span class="d amber">{len(live)} live now</span>' if live else "")
    kpis = f"""<div class="kpis">
<div class="card kpi"><div class="l">Total revenue</div><div class="v">{_money(rev)}</div>{_delta(rev, rev_prev)}</div>
<div class="card kpi"><div class="l">AI voice orders</div><div class="v">{len(cur)} {_delta(len(cur), len(prev))}</div></div>
<div class="card kpi"><div class="l">Calls today</div><div class="v">{len(calls_today)} {live_note}</div></div>
{pos_card}</div>"""

    recent = f"""<div class="card"><div class="card-h"><h3>Recent AI orders</h3>
<a class="r" href="/portal/orders">View all {icon("arrow", 16)}</a></div>
{_orders_table(orders[:5], tz, with_time=False)}</div>"""

    # Orders per day: the last 7 local days against the 7 before them.
    today = _dt(now, tz).date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    per_day: dict = {}
    for o in orders:
        if (o.get("saved_at") or 0) >= now - 15 * 86400:
            k = _dt(o["saved_at"], tz).date()
            per_day[k] = per_day.get(k, 0) + 1
    this_week = [per_day.get(d, 0) for d in days]
    last_week = [per_day.get(d - timedelta(days=7), 0) for d in days]
    trend = f"""<div class="card"><div class="card-h"><h3>Orders trend</h3>
<div class="legend r"><span><i style="background:var(--acc)"></i>This week</span>
<span><i style="background:#cdd3db"></i>Last week</span></div></div>
<div class="card-b">{ui.line_chart(this_week, last_week, [d.strftime("%a").upper() for d in days])}</div></div>"""

    call_rows = []
    for c in calls[:4]:
        who = _e(c["from_number"] or "Unknown caller")
        if _is_live(c, now):
            call_rows.append(
                f'<div class="call live"><div class="ic"><span class="dot"></span></div>'
                f'<div class="who"><b>{who}</b><div class="meta">Live · {c["turn_count"]} turns</div>'
                f'<div class="mut small" style="margin-top:6px">{_e(c["last_reply"][:90])}</div></div>'
                f'<a class="go" href="/portal/calls/{_e(c["call_sid"])}">Listen in</a></div>')
        else:
            length = _dur((c["updated_at"] or 0) - (c["started_at"] or 0))
            status = "ended" if c["status"] == "live" else c["status"]  # live but stale
            call_rows.append(
                f'<div class="call"><div class="ic">{icon("phone", 20)}</div>'
                f'<div class="who"><b>{who}</b><div class="meta">{_e(status)} · {length}</div></div>'
                f'<a class="go" href="/portal/calls/{_e(c["call_sid"])}">Details</a></div>')
    calls_card = f"""<div class="card"><div class="card-h"><h3>Live call activity</h3>
<span class="r dot{'' if live else ' gray'}" title="{'Calls in progress' if live else 'No live calls'}"></span></div>
<div class="card-b">{"".join(call_rows) or '<p class="mut" style="margin:0">No calls yet.</p>'}
<a class="btn ghost block" style="margin-top:20px" href="/portal/calls">View call history {icon("arrow", 16)}</a></div></div>"""

    drafts = deps.tenants.list_drafts(tenant.id, 100)
    month = _month_usage(deps, tenant, now, tz)
    latest = _e(drafts[0]["title"]) if drafts else "No drafts yet"
    marketing = f"""<a class="dark" href="/portal/marketing" style="display:block;text-decoration:none">
<div class="eyebrow">Marketing · latest draft</div><h3>{latest}</h3>
<div class="nums"><div><div class="n g">{len(drafts)}</div><div class="nl">Drafts</div></div>
<div><div class="n o">{month["sms_sent"]}</div><div class="nl">SMS sent this month</div></div></div></a>"""

    date_line = _dt(now, tz).strftime("%B %d, %Y").replace(" 0", " ")
    body = f"""<div class="row-between"><div class="eyebrow">Overview · {date_line}</div>
<div class="mut small">Revenue and orders: last 30 days</div></div>{kpis}
<div class="grid-main"><div class="stack">{recent}{trend}</div>
<div class="stack">{calls_card}{marketing}</div></div>"""
    return _page(deps, tenant, "dash", "Dashboard", body)


_RANGES = {7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days"}


def _statistics_page(deps: PortalDeps, tenant: Tenant, days: int) -> str:
    days = days if days in _RANGES else 7
    tz, now = _tz(tenant), time.time()
    start = _day_start(now, tz) - (days - 1) * 86400
    prev_start = start - days * 86400
    orders = _load_orders(deps, tenant)
    cur = [o for o in orders if (o.get("saved_at") or 0) >= start]
    prev = [o for o in orders if prev_start <= (o.get("saved_at") or 0) < start]
    rev, rev_prev = sum(map(_order_total, cur)), sum(map(_order_total, prev))
    aov = rev / len(cur) if cur else 0.0
    aov_prev = rev_prev / len(prev) if prev else 0.0

    calls = deps.tenants.calls_in_window(tenant.id, start)
    call_sids = {c["call_sid"] for c in calls}
    converted = sum(1 for o in cur if o.get("call_id") in call_sids)
    conv = f"{converted / len(calls) * 100:.1f}%" if calls else "—"

    kpis = f"""<div class="kpis">
<div class="card kpi"><div class="l">Total revenue</div><div class="v">{_money(rev)}</div>{_delta(rev, rev_prev)}</div>
<div class="card kpi"><div class="l">AI voice orders</div><div class="v">{len(cur)} {_delta(len(cur), len(prev))}</div></div>
<div class="card kpi"><div class="l">Order conversion</div><div class="v">{conv}</div>
<span class="d mut">{converted} of {len(calls)} calls</span></div>
<div class="card kpi"><div class="l">Average order value</div><div class="v">{_money(aov)} {_delta(aov, aov_prev)}</div></div>
</div>"""

    # Orders per day (or per week for the 90-day view).
    first_day = _dt(start, tz).date()
    counts: dict = {}
    for o in cur:
        k = _dt(o["saved_at"], tz).date()
        counts[k] = counts.get(k, 0) + 1
    if days <= 30:
        buckets = [first_day + timedelta(days=i) for i in range(days)]
        values = [counts.get(d, 0) for d in buckets]
        if days == 7:
            labels = [d.strftime("%a").upper() for d in buckets]
        else:
            labels = [str(d.day) if i % 5 == 0 else "" for i, d in enumerate(buckets)]
    else:
        weeks = (days + 6) // 7
        buckets = [first_day + timedelta(days=7 * i) for i in range(weeks)]
        values = [sum(counts.get(b + timedelta(days=j), 0) for j in range(7)) for b in buckets]
        labels = [f"{b.strftime('%b')} {b.day}" if i % 2 == 0 else "" for i, b in enumerate(buckets)]
    by_day = f"""<div class="card"><div class="card-h"><h3>Orders by {"week" if days > 30 else "day"}</h3>
<span class="r pill ok">{_e(_RANGES[days])}</span></div>
<div class="card-b">{ui.bar_chart(values, labels) if cur else '<div class="empty">No orders in this period.</div>'}</div></div>"""

    agg: dict[str, int] = {}
    for o in cur:
        for line in o.get("lines") or []:
            name = str(line.get("item_name") or "").strip()
            if name:
                agg[name] = agg.get(name, 0) + int(line.get("quantity") or 1)
    top = sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ranks = "".join(f'<div class="rank"><span>{_e(n)}</span><span>{q} ordered</span></div>' for n, q in top)
    items = f"""<div class="card"><div class="card-h"><h3>Most ordered items</h3>
<span class="r eyebrow">{_e(_RANGES[days])}</span></div>
<div class="card-b" style="padding-top:4px;padding-bottom:4px">{ranks or '<div class="empty">No items ordered yet.</div>'}</div></div>"""

    ended = [c for c in calls if c["status"] != "live"]
    avg_len = (sum((c["updated_at"] or 0) - (c["started_at"] or 0) for c in ended) / len(ended)
               if ended else 0)
    turn_counts, transfers = [], 0
    for c in calls:
        try:
            turns = json.loads(c["turns"] or "[]")
        except (TypeError, ValueError):
            turns = []
        turn_counts.append(len(turns))
        if any(t.get("name") == "transfer_call" for turn in turns for t in turn.get("tools") or []):
            transfers += 1
    avg_turns = sum(turn_counts) / len(turn_counts) if turn_counts else 0
    perf = f"""<div class="card"><div class="card-h"><h3>AI performance</h3></div><div class="card-b">
<div class="kv"><span>Calls handled</span><b>{len(calls)}</b></div>
<div class="kv"><span>Average call duration</span><b>{_dur(avg_len) if ended else "—"}</b></div>
<div class="kv"><span>Average turns per call</span><b>{avg_turns:.1f}</b></div>
<div class="kv"><span>Transferred to staff</span><b>{transfers}{f" · {transfers / len(calls) * 100:.0f}%" if calls else ""}</b></div>
</div></div>"""

    at_pickup = sum(1 for o in cur if _payment_label(o) == "At pickup")
    by_status: dict[str, int] = {}
    for o in cur:
        by_status[_order_status(o)[0]] = by_status.get(_order_status(o)[0], 0) + 1

    def share(n: int) -> str:
        return f"{n / len(cur) * 100:.0f}% · {n} orders" if cur else "0 orders"

    status_rows = "".join(f'<div class="kv"><span>Status: {_e(s)}</span><b>{share(n)}</b></div>'
                          for s, n in sorted(by_status.items(), key=lambda kv: -kv[1]))
    fulfil = f"""<div class="card"><div class="card-h"><h3>Order fulfillment</h3></div><div class="card-b">
<div class="kv"><span>Pay at pickup</span><b>{share(at_pickup)}</b></div>
<div class="kv"><span>Pay by link</span><b>{share(len(cur) - at_pickup)}</b></div>{status_rows}
</div></div>"""

    opts = "".join(f'<option value="{d}"{" selected" if d == days else ""}>{l}</option>'
                   for d, l in _RANGES.items())
    actions = (f'<select aria-label="Date range" style="width:auto" '
               f'onchange="location.href=\'/portal/statistics?days=\'+this.value">{opts}</select>'
               f'<a class="btn ghost" href="/portal/api/orders.csv?days={days}">{icon("download", 18)}Export</a>')
    body = (_page_head("Statistics", "Revenue, ordering patterns, and AI performance.", actions)
            + kpis + f'<div class="grid-2">{by_day}{items}{perf}{fulfil}</div>')
    return _page(deps, tenant, "stats", "Statistics", body)


_ORDERS_JS = """
(function(){
  var q=document.getElementById('q'), st=document.getElementById('st'), n=document.getElementById('n');
  var rows=[].slice.call(document.querySelectorAll('#orders tbody tr'));
  function run(){
    var term=q.value.trim().toLowerCase(), want=st.value, shown=0;
    rows.forEach(function(r){
      var ok=(!term || r.dataset.q.indexOf(term)>=0) && (!want || r.dataset.status===want);
      r.style.display=ok?'':'none'; if(ok) shown++;
    });
    n.textContent=shown+(shown===1?' order':' orders');
  }
  q.addEventListener('input', run); st.addEventListener('change', run); run();
})();
"""


def _orders_page(deps: PortalDeps, tenant: Tenant) -> str:
    tz = _tz(tenant)
    orders = _load_orders(deps, tenant, 200)
    statuses = sorted({_order_status(o)[0] for o in orders})
    opts = "".join(f'<option value="{_e(s)}">{_e(s.title())}</option>' for s in statuses)
    actions = f'<a class="btn ghost" href="/portal/api/orders.csv">{icon("download", 18)}Export orders</a>'
    body = (_page_head("Orders", "All voice orders, from the first call to the final pickup.", actions)
            + f"""<div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin-bottom:28px">
<div style="position:relative;flex:1;min-width:220px;max-width:430px">
<span style="position:absolute;left:16px;top:50%;transform:translateY(-50%);color:var(--mut)">{icon("search", 18)}</span>
<input id="q" placeholder="Search orders or items…" style="padding-left:46px"></div>
<select id="st" style="width:auto;min-width:180px"><option value="">All statuses</option>{opts}</select>
<span class="mut" id="n"></span>{'<span class="mut small">· newest 200</span>' if len(orders) >= 200 else ''}</div>
<div class="card" id="orders">{_orders_table(orders, tz, with_time=True)}</div>
<script>{_ORDERS_JS}</script>""")
    return _page(deps, tenant, "orders", "Orders", body)


def _settings_page(deps: PortalDeps, tenant: Tenant, user: PortalUser | None = None,
                   saved: bool = False) -> str:
    cur_tz = tenant.setting("timezone", "")
    tz_label = dict(TIMEZONES).get(cur_tz, "")
    now_there = _hm(_dt(time.time(), _tz(tenant))) if cur_tz else ""
    tz_block = (
        "<label class='f'>Time zone</label>"
        f"<select name='timezone'>{_tz_options(cur_tz, 'Not set (uses the server clock)')}</select>"
        "<p class='help'>Pickup times the AI tells callers, text receipts, and every time in this"
        " portal use this zone." + (f" It's {now_there} there now." if now_there else "") + "</p>"
        + ("" if cur_tz else "<p class='help' id='tz-guess' hidden>Suggested from your browser."
           " Click Save changes to apply it.</p>"))
    fields = ""
    for f in SETTING_FIELDS:
        val = tenant.setting(f["key"], "")
        if f["key"] == "phone_number" and not val:
            val = tenant.phone_number
        fields += (f"<label class='f'>{f['label']}</label>"
                   f"<input name='{f['key']}' value='{_e(val)}'"
                   f" placeholder='{_e(f.get('placeholder', ''))}'>"
                   + (f"<p class='help'>{f['help']}</p>" if f.get("help") else ""))
        if f["key"] == "restaurant_name":
            fields += tz_block
    cur_voice = tenant.setting("voice_id", "") or voice_catalog.DEFAULT_VOICE_ID
    cur_model = tenant.setting("voice_model", "") or voice_catalog.DEFAULT_MODEL_ID
    known_ids = {v[0] for v in voice_catalog.ELEVENLABS_VOICES}
    custom_voice = "" if cur_voice in known_ids else cur_voice
    voice_name = next((n for vid, n, *_ in voice_catalog.ELEVENLABS_VOICES if vid == cur_voice),
                      "Custom voice")
    voice_opts = "".join(
        f"<option value='{vid}'{' selected' if vid == cur_voice else ''}>"
        f"{_e(name)} — {gender}, {age}, {accent}</option>"
        for vid, name, gender, age, accent in voice_catalog.ELEVENLABS_VOICES
    )
    model_opts = "".join(
        f"<option value='{mid}'{' selected' if mid == cur_model else ''}>"
        f"{_e(label)}</option>"
        for mid, label in voice_catalog.ELEVENLABS_MODELS
    )
    pos_label, connected, _ = _pos_state(deps, tenant)
    phone = tenant.setting("phone_number", "") or tenant.phone_number
    body = _page_head("System settings", "Restaurant details, time zone, and the AI voice.") + f"""
{'<div class="alert ok">Saved.</div>' if saved else ''}
<div id=settings-msg></div>
<div class="grid-main"><form id=settings-form>
<h3 class="sec-h">Restaurant details</h3>{fields}
<h3 class="sec-h" style="margin-top:44px">AI voice</h3>
<p class="mut small" style="margin:-10px 0 0">The voice callers hear when they phone your restaurant.
Preview a voice before saving — each preview uses a few dozen characters of your ElevenLabs monthly budget.</p>
<label class="f">Voice</label>
<select name='voice_id'>{voice_opts}</select>
<label class="f">Or a custom / cloned voice ID</label>
<input name='voice_id_custom' value='{_e(custom_voice)}' placeholder='Paste a voice ID — overrides the selection above'>
<label class="f">Model</label>
<select name='voice_model'>{model_opts}</select>
<div style='margin-top:16px'>
<button class="btn ghost" type=button id=voice-preview>{icon("play", 16)}<span>Preview voice</span></button>
<audio id=voice-preview-audio controls style='display:none;margin-top:12px;width:100%'></audio>
<div id=voice-preview-msg></div></div>
<div class="actions"><button class="btn" type=submit>{icon("save", 18)}Save changes</button></div></form>
<div><h3 class="sec-h">Workspace</h3>
<div class="kv"><span>Restaurant</span><b>{_e(tenant.name)}</b></div>
<div class="kv"><span>Phone number</span><b>{_e(phone) or "—"}</b></div>
<div class="kv"><span>Time zone</span><b>{_e(tz_label) or '<span class="pill warn">Not set</span>'}</b></div>
<div class="kv"><span>Point of sale</span><b>{_e(pos_label) or "—"} <span class="pill {'ok' if connected else 'warn'}">{'Connected' if connected else 'Not connected'}</span></b></div>
<div class="kv"><span>AI voice</span><b>{_e(voice_name)}</b></div>
<div class="kv"><span>Voice provider</span><b>Twilio</b></div>
{f'<div class="kv"><span>Signed in as</span><b>{_e(user.email)}</b></div>' if user else ''}
<a class="btn ghost" style="margin-top:24px" href="/portal/logout">{icon("logout", 18)}Log out</a></div></div>
{_TZ_GUESS_JS if not cur_tz else ""}
<script>
document.getElementById('settings-form').addEventListener('submit', async e => {{
  e.preventDefault();
  const fd = new FormData(e.target);
  const values = Object.fromEntries(fd.entries());
  const r = await fetch('/portal/api/settings', {{method:'POST',
    headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(values)}});
  const j = await r.json().catch(() => ({{}}));
  if (j.ok) location.href = '/portal/settings?saved=1';
  else {{
    const m = document.getElementById('settings-msg');
    m.innerHTML = '<div class="alert bad"></div>';
    m.firstChild.textContent = j.error || 'Save failed';
  }}
}});
document.getElementById('voice-preview').addEventListener('click', async () => {{
  const btn = document.getElementById('voice-preview');
  const lbl = btn.querySelector('span');
  const msg = document.getElementById('voice-preview-msg');
  const audio = document.getElementById('voice-preview-audio');
  btn.disabled = true; lbl.textContent = 'Rendering…';
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
    msg.innerHTML = '<div class="alert bad"></div>';
    msg.firstChild.textContent = err.message;
  }}
  btn.disabled = false; lbl.textContent = 'Preview voice';
}});
</script>"""
    return _page(deps, tenant, "settings", "System settings", body)


def _pos_page(deps: PortalDeps, tenant: Tenant) -> str:
    tabs = "".join(
        "<button type=button class='tab' data-p='%s'>%s</button>" % (p, spec["label"])
        for p, spec in POS_PROVIDERS.items())
    pickup = tenant.setting("pickup_minutes", "") or "20"
    # NOTE: plain string (not f-string): the JS below is full of ${...}
    # template literals that would collide with f-string braces.
    body = _page_head("POS setup", "Manage your restaurant's point-of-sale connection.") + """
<div class="grid-main"><div>
<h3 class="sec-h">Choose your point of sale</h3>
<div class="seg">__TABS__</div>
<div id=status></div>
<form id=pos-form><div id=fields></div>
<div class="actions"><button class="btn" type=submit id=save>__SAVE_ICON__<span>Save configuration</span></button>
<button class="btn ghost" type=button id=test>__CHECK_ICON__<span>Test connection</span></button></div></form>
<div class="alert" style="margin-top:36px">When a customer calls your Twilio number, the AI takes the order and
submits it to the POS you connect here as an <b>open, visible order</b> your staff see immediately.
Credentials are encrypted before they're stored and are never shown back.</div>
</div>
<div><h3 class="sec-h">Connection overview</h3>
<div class="kv"><span>Provider</span><b id=ov-provider>—</b></div>
<div class="kv"><span>Environment</span><b id=ov-env>—</b></div>
<div class="kv"><span>Payment</span><b>At pickup</b></div>
<div class="kv"><span>Pickup time</span><b>__PICKUP__ minutes</b></div>
<div class="kv"><span>Connection</span><b id=ov-conn><span class="pill">—</span></b></div>
</div></div>
<script>
const PROVIDERS = __PROVIDERS__;
let current = null, configured = null;
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
function alertBox(kind, text) { return '<div class="alert ' + kind + '">' + esc(text) + '</div>'; }
async function load() {
  const r = await fetch('/portal/api/pos'); const j = await r.json();
  current = j.provider || 'square'; configured = j;
  render(); select(current);
}
function render() {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('on', t.dataset.p === current));
  const st = document.getElementById('status');
  const ok = configured && configured.provider && configured.has_secret;
  st.innerHTML = ok
    ? alertBox('ok', '✓ ' + PROVIDERS[configured.provider].label + ' connected.')
    : alertBox('warn', 'No POS connected yet. Choose your provider and add its credentials.');
  document.getElementById('ov-provider').textContent = ok ? PROVIDERS[configured.provider].label : 'None';
  const env = ok ? (configured.values.environment || 'production') : '';
  document.getElementById('ov-env').textContent = env ? env[0].toUpperCase() + env.slice(1) : '—';
  document.getElementById('ov-conn').innerHTML = ok
    ? '<span class="pill ok">Connected</span>' : '<span class="pill warn">Not connected</span>';
}
function fieldHtml(f, v) {
  const shown = f.secret ? (v ? '••••••••' : '') : (v || '');
  let input;
  if (f.options) {
    input = '<select name="' + f.key + '">' + f.options.map(o =>
      '<option value="' + o + '"' + (o === (v || f.default) ? ' selected' : '') + '>' +
      o[0].toUpperCase() + o.slice(1) + '</option>').join('') + '</select>';
  } else {
    input = '<input name="' + f.key + '" value="' + esc(shown) + '"' +
      (f.secret ? ' type=password autocomplete=new-password' : '') +
      ' placeholder="' + esc(f.placeholder || '') + '"' +
      ((f.secret && v) ? ' data-keep=1' : '') + '>';
  }
  let h = '<label class="f">' + f.label + (f.optional ? ' <span class=mut>(optional)</span>' : '') + '</label>' + input;
  if (f.help) h += '<p class="help">' + f.help + '</p>';
  if (f.secret && v) h += '<p class="help">Saved &mdash; leave as is to keep it.</p>';
  return h;
}
function select(p) {
  current = p;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.p === current));
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
function busy(btn, text) { btn.disabled = !!text; btn.querySelector('span').textContent = text || btn.dataset.l; }
document.querySelectorAll('#save,#test').forEach(b => b.dataset.l = b.querySelector('span').textContent);
document.getElementById('pos-form').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('save');
  busy(btn, 'Saving…');
  const r = await fetch('/portal/api/pos', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({provider: current, values: await collect()})});
  const j = await r.json();
  busy(btn);
  if (j.ok) { await load(); document.getElementById('status').innerHTML = alertBox('ok', '✓ Saved.'); }
  else document.getElementById('status').innerHTML = alertBox('bad', j.error || 'Save failed');
});
document.getElementById('test').addEventListener('click', async () => {
  const btn = document.getElementById('test');
  busy(btn, 'Testing…');
  const r = await fetch('/portal/api/pos/test', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({provider: current, values: await collect()})});
  const j = await r.json();
  busy(btn);
  document.getElementById('status').innerHTML = j.ok
    ? alertBox('ok', '✓ Connection works' + (j.detail ? ' — ' + j.detail : '') + '.')
    : alertBox('bad', '✗ ' + (j.error || 'Connection failed'));
});
load();
</script>
""".replace("__TABS__", tabs).replace("__PROVIDERS__", json.dumps(POS_PROVIDERS)) \
        .replace("__PICKUP__", _e(pickup)).replace("__SAVE_ICON__", icon("save", 18)) \
        .replace("__CHECK_ICON__", icon("check", 18))
    return _page(deps, tenant, "pos", "POS setup", body)


_CALLS_JS = """
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
function dur(c) { const s = Math.max(0, Math.round(c.updated_at - c.started_at)); return s >= 60 ? Math.floor(s/60) + 'm ' + String(s%60).padStart(2,'0') + 's' : s + 's'; }
async function load() {
  const r = await fetch('/portal/api/calls'); if (!r.ok) return;
  const j = await r.json();
  const tb = document.getElementById('rows');
  const now = Date.now() / 1000;
  if (!j.calls.length) { tb.innerHTML = '<tr><td colspan=6 class="mut">No calls yet. They appear here the moment a customer phones in.</td></tr>'; return; }
  tb.innerHTML = j.calls.map(c => {
    const live = c.status === 'live' && now - c.updated_at < __STALE__;
    return `<tr>
    <td><span class="pill ${live ? 'ok' : ''}">${live ? 'live' : esc(c.status === 'live' ? 'ended' : c.status)}</span></td>
    <td><b>${esc(c.from_number || 'Unknown caller')}</b></td>
    <td class="mut">${new Date(c.started_at*1000).toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'})}</td>
    <td class="mut">${live ? '—' : dur(c)}</td>
    <td>${c.turn_count}</td>
    <td><a href="/portal/calls/${encodeURIComponent(c.call_sid)}">${live ? 'Listen in' : 'Details'}</a></td></tr>`;
  }).join('');
}
load(); setInterval(load, 3000);
"""


def _calls_page(deps: PortalDeps, tenant: Tenant) -> str:
    body = _page_head("Live calls", "Every call the AI answers, live and recent. Updates every few seconds.") + """
<div class="card"><div class="tbl-wrap"><table><thead><tr><th>Status</th><th>Caller</th><th>Started</th>
<th>Length</th><th>Turns</th><th></th></tr></thead>
<tbody id=rows><tr><td colspan=6 class="mut">Loading…</td></tr></tbody></table></div></div>
<script>""" + _CALLS_JS.replace("__STALE__", str(_LIVE_STALE_SECONDS)) + "</script>"
    return _page(deps, tenant, "calls", "Live calls", body)


def _call_detail_page(deps: PortalDeps, tenant: Tenant, call_sid: str) -> str:
    body = f"""<p style="margin:0 0 20px"><a href="/portal/calls">{icon("back", 16)} All calls</a></p>
<div class="page-h"><div><h2>Call transcript</h2><p id=head>Loading…</p></div></div>
<div class="card"><div class="card-b" id=turns></div></div>
<script>
const SID = {json.dumps(call_sid)};
function esc(s) {{ const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }}
async function load() {{
  const r = await fetch('/portal/api/calls/' + encodeURIComponent(SID));
  if (r.status === 404) {{ document.getElementById('head').textContent = 'Call not found.'; return; }}
  const j = await r.json();
  document.getElementById('head').innerHTML =
    `<span class="pill ${{j.status==='live'?'ok':''}}">${{esc(j.status)}}</span>
     &nbsp;From ${{esc(j.from_number||'unknown caller')}} · started
     ${{new Date(j.started_at*1000).toLocaleString()}}`;
  const el = document.getElementById('turns');
  el.innerHTML = j.turns.length ? j.turns.map(t => `
    <div class="bubble caller"><div class=w>Caller · ${{new Date(t.ts*1000).toLocaleTimeString()}}</div>
      ${{t.heard ? esc(t.heard) : '<i class=mut>(no speech captured)</i>'}}</div>
    <div class="bubble ai"><div class=w>AI${{t.tools&&t.tools.length?' · '+esc(t.tools.map(x=>x.name).join(', ')):''}}</div>
      ${{esc(t.reply)}}</div>`).join('')
    : '<p class=mut style="margin:0">No conversation yet.</p>';
}}
load(); setInterval(load, 2000);
</script>"""
    return _page(deps, tenant, "calls", "Call", body)


_SEV_PILL = {"critical": "bad", "warning": "warn", "info": "ok"}
_CHANNEL_PILL = {"sms": "ok", "social": "", "in-store": "warn"}

_FAQ = [
    ("Where can I review an AI conversation?",
     "Open <a href='/portal/calls'>Live calls</a> and choose Details on any call. You'll see the "
     "full transcript, which updates live while the call is in progress."),
    ("Which point-of-sale providers are available?",
     "Square, Toast, and Clover. Connect yours under <a href='/portal/pos'>POS setup</a>; orders "
     "arrive there as open orders your staff can see right away."),
    ("When is customer payment collected?",
     "At pickup by default. If your POS account supports payment links, the caller's text receipt "
     "includes a link to pay ahead."),
    ("Does this workspace send real SMS messages?",
     "Only order receipts to the caller, and only when Twilio messaging is configured. Marketing "
     "drafts are never sent automatically."),
    ("How do I change the voice callers hear?",
     "Go to <a href='/portal/settings'>System settings</a> → AI voice, preview a voice, and save."),
]


def _support_page(deps: PortalDeps, tenant: Tenant, sent: bool = False) -> str:
    tz = _tz(tenant)
    tickets = deps.tenants.list_tickets(tenant.id)
    if not tickets:
        rows = '<tr><td colspan=3 class="mut">No alerts — everything looks healthy.</td></tr>'
    else:
        parts = []
        for t in tickets:
            sev = _SEV_PILL.get(t["severity"], "")
            plat = ' <span class="pill">Platform</span>' if not t["tenant_id"] else ""
            status = t["status"]
            sp = "bad" if status == "open" else ("ok" if status == "resolved" else "")
            action = ""
            if status == "open" and t["tenant_id"]:
                action = (f'<form method=post action="/portal/api/tickets/{_e(t["id"])}/ack"'
                          f' style="margin:8px 0 0"><button class="btn ghost sm" type=submit>'
                          f'Acknowledge</button></form>')
            parts.append(
                f"<tr><td><span class='pill {sev}'>{_e(t['severity'])}</span>{plat}</td>"
                f"<td><b>{_e(t['title'])}</b>"
                f"<div class='mut small' style='margin-top:4px'>{_e(t['detail'][:200])}</div></td>"
                f"<td><span class='pill {sp}'>{_e(status)}</span>"
                f"<div class='mut small' style='margin-top:6px'>{_clock(t['created_at'], tz, 'datetime')}</div>"
                f"{action}</td></tr>")
        rows = "".join(parts)
    faqs = "".join(f'<details class="faq"><summary>{q}</summary><p>{a}</p></details>' for q, a in _FAQ)
    body = _page_head("Support", "Help for your restaurant and voice-ordering workspace.") + f"""
<div class="grid-main"><div class="stack">
<div class="card"><div class="card-h"><h3>Alerts &amp; requests</h3><span class="r mut small">Open first</span></div>
<div class="tbl-wrap"><table><thead><tr><th>Severity</th><th>Issue</th><th>Status</th></tr></thead>
<tbody>{rows}</tbody></table></div></div>
<div><h3 class="sec-h">Frequently asked questions</h3>{faqs}</div></div>
<div><h3 class="sec-h">Contact support</h3>
{'<div class="alert ok">Request received. It is logged under Alerts &amp; requests.</div>' if sent else ''}
<form method=post action="/portal/support/request">
<label class="f">Subject</label><input name=subject required maxlength=120 placeholder="How can we help?">
<label class="f">Message</label><textarea name=message required maxlength=2000 placeholder="Describe the issue…"></textarea>
<div class="actions"><button class="btn" type=submit>{icon("send", 18)}Send request</button></div></form></div>
</div>"""
    return _page(deps, tenant, "support", "Support", body)


def _marketing_page(deps: PortalDeps, tenant: Tenant) -> str:
    tz, now = _tz(tenant), time.time()
    drafts = deps.tenants.list_drafts(tenant.id)
    if not drafts:
        cards = ('<div class="card"><div class="empty">No drafts yet. The marketing agent writes '
                 'fresh promo ideas from your menu and best sellers every week.</div></div>')
    else:
        cards = "".join(
            f"""<div class="card"><div class="card-h"><h3>{_e(d['title'])}</h3>
<span class="r pill {_CHANNEL_PILL.get(d['channel'], '')}">{_e(d['channel'])}</span></div>
<div class="card-b"><p style="white-space:pre-wrap;margin:0;line-height:1.6">{_e(d['body'])}</p>
<p class="mut small" style="margin:16px 0 0">{_clock(d['created_at'], tz, 'date')} · {_e(d['status'])}</p></div></div>"""
            for d in drafts)
    month_start = _dt(now, tz).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    this_month = sum(1 for d in drafts if (d["created_at"] or 0) >= month_start)
    usage = _month_usage(deps, tenant, now, tz)
    side = f"""<div class="dark"><div class="eyebrow">Marketing performance</div>
<h3>{"Drafts ready to use" if drafts else "Drafts on the way"}</h3>
<div class="nums"><div><div class="n g">{this_month}</div><div class="nl">Drafts this month</div></div>
<div><div class="n o">{usage["sms_sent"]}</div><div class="nl">SMS sent this month</div></div></div>
<p style="color:#9aa1ab;margin:36px 0 0;line-height:1.6;font-size:14px">Drafts only. Nothing is ever sent
automatically; copy a draft into your SMS tool, social post, or in-store sign.</p></div>"""
    body = (_page_head("Marketing", "Promo drafts written from your real menu and best sellers.")
            + f'<div class="grid-main"><div class="stack" style="gap:24px">{cards}</div><div>{side}</div></div>')
    return _page(deps, tenant, "marketing", "Marketing", body)


# ElevenLabs free plan allowance; shown as the reference for voice characters.
_TTS_FREE_CHARS = 10_000


def _usage_page(deps: PortalDeps, tenant: Tenant) -> str:
    tz, now = _tz(tenant), time.time()
    local = _dt(now, tz)
    month = _month_usage(deps, tenant, now, tz)
    next_month = (local.replace(day=28) + timedelta(days=4)).replace(day=1)
    rows_data = deps.tenants.get_usage(tenant.id)
    if not rows_data:
        rows = '<tr><td colspan=5 class="mut">No usage recorded yet.</td></tr>'
    else:
        rows = "".join(
            f"<tr><td class='id'>{_e(r['date'])}</td><td>{r['calls']}</td>"
            f"<td>{r['talk_minutes']:.1f}</td><td>{r['tts_chars']:,}</td>"
            f"<td>{r['sms_sent']}</td></tr>"
            for r in rows_data)
    pct = min(month["tts_chars"] / _TTS_FREE_CHARS * 100, 100)
    body = _page_head("Usage", f"Calls, voice minutes, and messages for {local.strftime('%B %Y')}.",
                      f'<span class="pill ok">{_e(local.strftime("%b %Y"))}</span>') + f"""
<div class="grid-2" style="gap:24px">
<div class="card card-b"><h3 class="sec-h" style="margin-bottom:24px">Voice minutes</h3>
<div class="kpi" style="padding:0;min-height:0"><div class="v" style="margin:0">{month["talk_minutes"]:,.1f}
<span class="mut" style="font-size:18px;font-weight:400">minutes this month</span></div></div></div>
<div class="card card-b"><h3 class="sec-h" style="margin-bottom:24px">AI calls</h3>
<div class="kpi" style="padding:0;min-height:0"><div class="v" style="margin:0">{month["calls"]:,}
<span class="mut" style="font-size:18px;font-weight:400">calls this month</span></div></div></div>
</div>
<h3 class="sec-h" style="margin-top:48px">This billing period</h3>
<div class="kv"><span>Billing cycle</span><b>{local.strftime("%B")} 1 – {(next_month - timedelta(days=1)).day}, {local.year}</b></div>
<div class="kv"><span>Restaurant locations</span><b>1</b></div>
<div class="kv"><span>SMS messages sent</span><b>{month["sms_sent"]:,}</b></div>
<div class="kv" style="display:block"><div class="row-between"><span>Voice characters (ElevenLabs)</span>
<b>{month["tts_chars"]:,} / {_TTS_FREE_CHARS:,}</b></div>
<div class="bar" style="height:6px;margin-top:14px;background:var(--gray-soft)"><i style="width:{pct:.1f}%"></i></div>
<p class="help">Measured against the ElevenLabs free-plan monthly allowance.</p></div>
<div class="kv"><span>Next reset</span><b>{next_month.strftime("%B")} 1, {next_month.year}</b></div>
<div class="card" style="margin-top:48px"><div class="card-h"><h3>Daily usage</h3><span class="r mut small">Newest first</span></div>
<div class="tbl-wrap"><table><thead><tr><th>Date</th><th>Calls</th><th>Talk min</th><th>Voice chars</th><th>SMS sent</th></tr></thead>
<tbody>{rows}</tbody></table></div></div>
<p class="mut small">Talk minutes are wall-clock call durations; voice characters count spoken reply text.</p>"""
    return _page(deps, tenant, "usage", "Usage", body)


def _csv_cell(value: Any) -> str:
    """Neutralize spreadsheet formulas in caller-supplied text."""
    s = str(value if value is not None else "")
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


def _orders_csv(deps: PortalDeps, tenant: Tenant, days: int) -> str:
    tz, now = _tz(tenant), time.time()
    orders = _load_orders(deps, tenant)
    if days > 0:
        start = _day_start(now, tz) - (days - 1) * 86400
        orders = [o for o in orders if (o.get("saved_at") or 0) >= start]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["order", "placed_at", "items", "total", "status", "payment",
                "customer_name", "customer_phone", "pickup_time"])
    for o in orders:
        placed = _dt(o["saved_at"], tz).strftime("%Y-%m-%d %H:%M") if o.get("saved_at") else ""
        w.writerow([_csv_cell(v) for v in (
            _order_no(o), placed, _order_items(o), f"{_order_total(o):.2f}",
            _order_status(o)[0], _payment_label(o), o.get("customer_name"),
            o.get("customer_phone"), o.get("pickup_time"))])
    return buf.getvalue()


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
                raise ValueError("A restaurant account already uses this phone number. "
                                 "Log in instead, or sign up with a different number.")
            if tenant is None:
                tenant = deps.tenants.create_tenant(name, phone)
                deps.tenants.set_settings(tenant.id, {
                    "restaurant_name": tenant.name,
                    "pos_profile": "",
                })
            user = deps.tenants.create_user(
                tenant.id, str(form.get("email", "")), str(form.get("password", ""))
            )
            zone = str(form.get("timezone", ""))
            if zone in TIMEZONE_IDS:
                deps.tenants.set_settings(tenant.id, {"timezone": zone})
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

    @router.get("/portal/statistics", response_class=HTMLResponse)
    async def statistics_page(request: Request, days: int = 7):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _statistics_page(deps, tenant, days)

    @router.get("/portal/orders", response_class=HTMLResponse)
    async def orders_page(request: Request):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _orders_page(deps, tenant)

    @router.get("/portal/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: int = 0):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        user, tenant = res
        return _settings_page(deps, tenant, user, saved=bool(saved))

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
    async def support_page(request: Request, sent: int = 0):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        _, tenant = res
        return _support_page(deps, tenant, sent=bool(sent))

    @router.post("/portal/support/request")
    async def support_request(request: Request):
        res = page_user(request)
        if isinstance(res, RedirectResponse):
            return res
        user, tenant = res
        form = await request.form()
        subject = str(form.get("subject", "")).strip()[:120]
        message = str(form.get("message", "")).strip()[:2000]
        if not subject or not message:
            return RedirectResponse("/portal/support", status_code=302)
        # Unique kind: open_ticket dedupes per kind, but every request stands alone.
        deps.tenants.open_ticket(tenant.id, f"support_request:{uuid.uuid4().hex[:8]}", "info",
                                 f"Support request: {subject}", f"From {user.email}: {message}")
        log.info("portal: tenant %s opened a support request", tenant.id)
        return RedirectResponse("/portal/support?sent=1", status_code=302)

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

    @router.get("/portal/api/orders.csv")
    async def api_orders_csv(request: Request, days: int = 0):
        _, tenant = api_user(request)
        stamp = time.strftime("%Y%m%d")
        return Response(
            content=_orders_csv(deps, tenant, max(0, min(days, 3650))),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="orders-{stamp}.csv"'},
        )

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
        allowed = {f["key"] for f in SETTING_FIELDS} | VOICE_SETTING_KEYS | {"timezone"}
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
        if values.get("timezone") and values["timezone"] not in TIMEZONE_IDS:
            return JSONResponse({"ok": False, "error": "unknown time zone"}, status_code=400)
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
            try:
                deps.tenants.set_phone_number(tenant.id, values["phone_number"])
            except ValueError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
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
