"""Platform Super Admin: every restaurant on one dashboard.

Separate from the restaurant portal on purpose:
- its own login (platform_users) and cookie, signed with a different key, so a
  restaurant session can't be used here and an admin session isn't a restaurant one;
- every page and API checks the platform permission it needs (rbac.platform_can);
- every change is written to the audit log; POS credentials are never shown.

There is no "log in as this restaurant" feature: admins change restaurant
settings from here, in the open, and each change is audited.
"""
from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import routing
from ..portal import ui
from ..portal.portal import TIMEZONES
from ..portal.ui import icon
from ..tenants import crypto, rbac
from ..tenants.store import PlatformUser, Tenant, TenantStore, normalize_number, real_transfer_number

log = logging.getLogger("voiceorder.admin")

COOKIE_NAME = "vo_admin"
_SESSION_SECONDS = 12 * 3600
_LIVE_STALE_SECONDS = 1800
_FAILED_LOGINS: dict[str, list[float]] = {}  # email -> recent failure times
_MAX_FAILURES, _FAILURE_WINDOW = 5, 15 * 60

NAV = [
    ("Platform", [
        ("overview", "Overview", "/admin/", "grid"),
        ("restaurants", "Restaurants", "/admin/restaurants", "users"),
        ("operations", "Live operations", "/admin/operations", "phone"),
    ]),
    ("Control", [
        ("rollout", "Feature rollout", "/admin/rollout", "gear"),
        ("usage", "Usage", "/admin/usage", "gauge"),
        ("audit", "Audit log", "/admin/audit", "help"),
    ]),
]
_FLAG_LABELS = {"voice_schedule_enabled": "AI phone controls", "hitl_enabled": "Kitchen approvals",
                "multilingual_enabled": "Multilingual (PR 5)"}


@dataclass
class AdminDeps:
    tenants: TenantStore
    build_adapter: Callable[[Tenant, dict | None], Any] | None = None
    platform_name: str = "VoiceOrderAI"


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _ago(ts: float | None, now: float) -> str:
    if not ts:
        return "never"
    s = int(now - ts)
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def _local(ts: float | None, tenant: Tenant | None = None) -> str:
    if not ts:
        return "—"
    zone = tenant.setting("timezone", "") if tenant else ""
    try:
        d = datetime.fromtimestamp(ts, ZoneInfo(zone)) if zone else datetime.fromtimestamp(ts)
    except Exception:
        d = datetime.fromtimestamp(ts)
    return d.strftime("%b %d · %I:%M %p").replace(" 0", " ")


def _phone(digits: str) -> str:
    d = normalize_number(digits)
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else (digits or "—")


def _page_head(title: str, sub: str, actions: str = "") -> str:
    acts = f'<div class="acts">{actions}</div>' if actions else ""
    return f'<div class="page-h"><div><h2>{_e(title)}</h2><p>{sub}</p></div>{acts}</div>'


_ACTION_JS = r"""
async function act(url, body, confirmText){
  if (confirmText && !confirm(confirmText)) return;
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
  const j = await r.json().catch(()=>({}));
  if (!r.ok || j.ok === false) { alert(j.error || j.detail || ('Failed (' + r.status + ')')); return; }
  location.reload();
}
document.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () =>
  act(b.dataset.act, JSON.parse(b.dataset.body || '{}'), b.dataset.confirm)));
document.querySelectorAll('form[data-post]').forEach(f => f.addEventListener('submit', e => {
  e.preventDefault(); act(f.dataset.post, Object.fromEntries(new FormData(f).entries()), f.dataset.confirm);
}));
"""


def _shell(deps: AdminDeps, admin: PlatformUser, active: str, title: str, body: str) -> str:
    emergency = deps.tenants.platform_emergency()
    return ui.shell(
        title=title, body=body + f"<script>{_ACTION_JS}</script>", tenant_name="Platform admin",
        active=active, platform=deps.platform_name, nav=NAV, home_href="/admin/",
        logout_href="/admin/logout", location=admin.email,
        status_label="AI emergency stop ON" if emergency else "Super Admin", status_ok=not emergency)


def _tenant_summary(deps: AdminDeps, t: Tenant, now: float, calls7: dict, orders7: dict,
                    tickets: dict) -> dict:
    provider = t.setting("pos_profile", "")
    connected = deps.tenants.has_secret(t.id, provider) if provider else False
    flags = deps.tenants.list_flags(t.id)
    return {"tenant": t, "provider": provider or "—", "pos_connected": connected,
            "calls_7d": calls7.get(t.id, 0), "orders_7d": orders7.get(t.id, 0),
            "open_tickets": tickets.get(t.id, 0), "flags": flags}


# --- pages ---------------------------------------------------------------------------
def _login_page(platform: str, error: str = "") -> str:
    body = f"""<h2>Super Admin</h2><p class="mut" style="margin:0 0 24px">Platform staff only. Restaurant
logins use the <a href="/portal/login">restaurant portal</a>.</p>
{f'<div class="alert bad">{_e(error)}</div>' if error else ''}
<form method=post action="/admin/login">
<label class="f">Email</label><input name=email type=email required autocomplete=username>
<label class="f">Password</label><input name=password type=password required autocomplete=current-password>
<div class="actions"><button class="btn block" type=submit>Log in</button></div></form>"""
    return ui.auth_shell("Super Admin", body, platform)


def _overview(deps: AdminDeps, admin: PlatformUser) -> str:
    s, now = deps.tenants, time.time()
    tenants = s.list_tenants(active_only=False)
    day = now - 86400
    calls = s.platform_calls(day, 1000)
    live = [c for c in calls if c["status"] == "live" and now - (c["updated_at"] or 0) < _LIVE_STALE_SECONDS]
    no_ai = [c for c in calls if (c["meta"] or {}).get("route") in ("forwarded", "voicemail", "closed", "ai_off")]
    missed = [c for c in calls if (c["meta"] or {}).get("transfer_result") not in (None, "completed")]
    orders = sum(s.orders_by_tenant(day).values())
    pending = s.platform_pending_approvals()
    tickets = s.open_tickets_by_tenant()
    pos_problems = sum(n for tid, n in tickets.items() if tid != "*")
    jobs = s.job_status()
    stale = [j for j in jobs if j["last_status"] == "error"]
    kpi = lambda label, value, sub="", warn=False: (
        f'<div class="card kpi{" warn" if warn else ""}"><div class="l">{label}</div>'
        f'<div class="v">{value}</div><span class="d mut">{sub}</span></div>')
    active = sum(1 for t in tenants if t.status == "active")
    kpis = (f'<div class="kpis">'
            + kpi("Restaurants", len(tenants), f"{active} active · {len(tenants) - active} suspended")
            + kpi("Calls (24 h)", len(calls), f"{len(live)} live now")
            + kpi("Orders (24 h)", orders)
            + kpi("Kitchen requests waiting", len(pending), "across all restaurants", warn=bool(pending))
            + '</div><div class="kpis">'
            + kpi("Calls without the AI (24 h)", len(no_ai), "forwarded, voicemail or closed")
            + kpi("Missed transfers (24 h)", len(missed), "staff didn't answer", warn=bool(missed))
            + kpi("Open alerts", pos_problems + tickets.get("*", 0),
                  f"{tickets.get('*', 0)} platform-wide", warn=bool(pos_problems or tickets.get("*")))
            + kpi("Background agents", f"{len(jobs) - len(stale)}/{len(jobs) or 7}",
                  "ok" if not stale else f"{len(stale)} failing", warn=bool(stale))
            + "</div>")
    job_rows = "".join(
        f'<div class="kv"><span>{_e(j["job"])}</span><b><span class="pill {"ok" if j["last_status"] == "ok" else "bad"}">'
        f'{_e(j["last_status"] or "not run")}</span> <span class="mut small">{_ago(j["last_finished_at"], now)}</span></b></div>'
        for j in jobs) or '<p class="mut">Agents haven\'t run yet (AGENTS_ENABLED=1 turns them on).</p>'
    emergency = s.platform_emergency()
    stop = (f'<button class="btn" data-act="/admin/api/emergency" data-body=\'{{"off": false}}\'>'
            f'Resume AI answering everywhere</button>' if emergency else
            f'<button class="btn bad" data-act="/admin/api/emergency" data-body=\'{{"off": true}}\' '
            f'data-confirm="Stop the AI answering calls at EVERY restaurant? Calls will follow each '
            f'restaurant\'s fallback (staff, voicemail or message).">Emergency stop: all AI answering</button>')
    body = (_page_head("Overview", f"All restaurants · data as of {_local(now)}", stop) + kpis
            + f'<div class="grid-2"><div class="card"><div class="card-h"><h3>Background agents</h3></div>'
              f'<div class="card-b">{job_rows}</div></div>'
              f'<div class="card"><div class="card-h"><h3>Needs attention</h3></div><div class="card-b">'
            + "".join(f'<div class="kv"><span>{_e(t.name)}</span><b>{tickets.get(t.id, 0)} open alert(s) '
                      f'<a href="/admin/restaurants/{_e(t.id)}">Open</a></b></div>'
                      for t in tenants if tickets.get(t.id))
            + ('' if any(tickets.get(t.id) for t in tenants) else '<p class="mut">Nothing right now.</p>')
            + "</div></div></div>")
    return _shell(deps, admin, "overview", "Overview", body)


def _restaurants(deps: AdminDeps, admin: PlatformUser, q: str, status: str) -> str:
    s, now = deps.tenants, time.time()
    week = now - 7 * 86400
    calls7, orders7, tickets = s.calls_by_tenant(week), s.orders_by_tenant(week), s.open_tickets_by_tenant()
    rows = []
    for t in s.list_tenants(active_only=False):
        if status and t.status != status:
            continue
        if q and q.lower() not in f"{t.name} {t.phone_number} {t.slug}".lower():
            continue
        x = _tenant_summary(deps, t, now, calls7, orders7, tickets)
        on = [_FLAG_LABELS[f].split(" (")[0] for f, v in x["flags"].items() if v]
        alerts = (f' <span class="pill warn">{x["open_tickets"]} alert(s)</span>'
                  if x["open_tickets"] else "")
        rows.append(
            f'<tr><td><a href="/admin/restaurants/{_e(t.id)}"><b>{_e(t.name)}</b></a>'
            f'<div class="mut small">{_e(t.setting("timezone", "") or "no time zone")}</div></td>'
            f'<td class="id">{_e(_phone(t.phone_number))}</td>'
            f'<td>{_e(x["provider"])} <span class="pill {"ok" if x["pos_connected"] else "warn"}">'
            f'{"connected" if x["pos_connected"] else "not connected"}</span></td>'
            f'<td>{x["calls_7d"]}</td><td>{x["orders_7d"]}</td>'
            f'<td class="small">{_e(", ".join(on)) or "<span class=mut>—</span>"}</td>'
            f'<td><span class="pill {"ok" if t.status == "active" else "bad"}">{_e(t.status)}</span>'
            f'{alerts}</td></tr>')
    status_opts = "".join(f'<option value="{v}"{" selected" if status == v else ""}>{l}</option>'
                          for v, l in (("", "All statuses"), ("active", "Active"), ("suspended", "Suspended")))
    tz_opts = "".join(f'<option value="{z}">{_e(l)}</option>' for z, l in TIMEZONES)
    body = _page_head("Restaurants", "Every restaurant on the platform.") + f"""
<form method="get" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px">
<input name="q" value="{_e(q)}" placeholder="Search name or phone…" style="max-width:360px">
<select name="status" style="width:auto">{status_opts}</select>
<button class="btn ghost" type="submit">Filter</button></form>
<div class="card"><div class="tbl-wrap"><table><thead><tr><th>Restaurant</th><th>Phone</th><th>POS</th>
<th>Calls 7d</th><th>Orders 7d</th><th>Features on</th><th>Status</th></tr></thead>
<tbody>{"".join(rows) or '<tr><td colspan=7 class="mut">No restaurants match.</td></tr>'}</tbody></table></div></div>
<form class="card card-b" style="margin-top:32px;max-width:760px" data-post="/admin/api/restaurants">
<h3 class="sec-h">Add a restaurant</h3>
<div class="grid-2" style="gap:0 24px"><div><label class="f">Restaurant name</label><input name="name" required></div>
<div><label class="f">Twilio phone number</label><input name="phone" placeholder="+15622680097"></div>
<div><label class="f">Time zone</label><select name="timezone">{tz_opts}</select></div>
<div><label class="f">Admin email</label><input name="admin_email" type="email" required></div>
<div><label class="f">Temporary admin password</label><input name="admin_password" type="password" minlength="8" required></div></div>
<div class="actions"><button class="btn" type="submit">Create restaurant</button></div></form>"""
    return _shell(deps, admin, "restaurants", "Restaurants", body)


def _restaurant(deps: AdminDeps, admin: PlatformUser, t: Tenant) -> str:
    s, now = deps.tenants, time.time()
    provider = t.setting("pos_profile", "")
    secret_fields = []
    if provider:
        try:
            secret_fields = sorted(s.get_secret(t.id, provider).keys())
        except Exception:
            secret_fields = ["(can't be decrypted with this server's key)"]
    flags = s.list_flags(t.id)
    zone = t.setting("timezone", "")
    tz = None
    try:
        tz = ZoneInfo(zone) if zone else None
    except Exception:
        pass
    cfg, _ = s.routing_config(t.id)
    if flags.get("voice_schedule_enabled"):
        d = routing.evaluate(cfg, now, tz, s.platform_emergency())
        routing_line = f"{'AI answering' if d.ai else 'AI off'} · {d.reason}"
    else:
        routing_line = "AI answers every call (AI phone controls off)"
    users = s.list_users(t.id)
    calls = [c for c in s.platform_calls(now - 7 * 86400, 500) if c["tenant_id"] == t.id][:15]
    audit = s.list_audit(t.id, 25)
    tickets = [x for x in s.list_tickets(t.id, include_platform=False) if x["status"] != "resolved"]
    flag_rows = "".join(
        f'<div class="kv"><span>{_e(_FLAG_LABELS.get(f, f))}</span><b>'
        f'<span class="pill {"ok" if v else ""}">{"on" if v else "off"}</span> '
        f'<button class="linkbtn" data-act="/admin/api/restaurants/{_e(t.id)}/flags" '
        f'data-body=\'{json.dumps({"flag": f, "enabled": not v})}\'>Turn {"off" if v else "on"}</button></b></div>'
        for f, v in flags.items())
    user_rows = "".join(
        f'<div class="kv"><span>{_e(u.email)} <span class="pill">{_e(u.role)}</span></span><b>'
        f'<button class="linkbtn" data-act="/admin/api/restaurants/{_e(t.id)}/users/{_e(u.id)}/role" '
        f'data-body=\'{json.dumps({"role": "kitchen" if u.role == "admin" else "admin"})}\'>'
        f'Make {"kitchen" if u.role == "admin" else "admin"}</button></b></div>'
        for u in users) or '<p class="mut">No logins yet.</p>'
    def call_status(c: dict) -> str:  # a "live" call silent for 30 min lost its end event
        stale = c["status"] == "live" and now - (c["updated_at"] or 0) >= _LIVE_STALE_SECONDS
        return "ended" if stale else c["status"]
    call_rows = "".join(
        f'<tr><td class="mut">{_local(c["started_at"], t)}</td><td>{_e((c["meta"] or {}).get("route", "ai"))}</td>'
        f'<td>{_e(call_status(c))}</td></tr>' for c in calls) or '<tr><td class="mut">No calls this week.</td></tr>'
    audit_rows = "".join(
        f'<tr><td class="mut small">{_local(a["at"], t)}</td><td>{_e(a["action"])}</td>'
        f'<td class="mut small">{_e(a["actor_type"])}</td></tr>' for a in audit) or \
        '<tr><td class="mut">No changes recorded.</td></tr>'
    suspended = t.status != "active"
    status_btn = (f'<button class="btn" data-act="/admin/api/restaurants/{_e(t.id)}/status" '
                  f'data-body=\'{{"status": "active"}}\'>Reactivate</button>' if suspended else
                  f'<button class="btn ghost" style="color:var(--bad)" data-act="/admin/api/restaurants/{_e(t.id)}/status" '
                  f'data-body=\'{{"status": "suspended"}}\' data-confirm="Suspend {_e(t.name)}? Callers will hear a '
                  f'closed message and the portal stays reachable.">Suspend</button>')
    stop = (f'<button class="btn ghost" data-act="/admin/api/restaurants/{_e(t.id)}/emergency" '
            f'data-body=\'{{"off": {"false" if cfg.emergency_off else "true"}}}\'>'
            f'{"Resume AI" if cfg.emergency_off else "Turn AI off now"}</button>')
    transfer = real_transfer_number(t.setting("transfer_number", ""))
    body = _page_head(t.name, f'<span class="pill {"bad" if suspended else "ok"}">{_e(t.status)}</span> '
                      f'&nbsp;{_e(_phone(t.phone_number))} · {_e(dict(TIMEZONES).get(zone, "no time zone"))}',
                      status_btn) + f"""
<div class="grid-2"><div class="stack" style="gap:28px">
<div class="card"><div class="card-h"><h3>Calls & AI</h3><span class="r">{stop}</span></div><div class="card-b">
<div class="kv"><span>Right now</span><b>{_e(routing_line)}</b></div>
<div class="kv"><span>Staff transfer number</span><b>{_e(_phone(transfer)) if transfer else '<span class="pill warn">not set</span>'}</b></div>
<div class="kv"><span>Pickup time · tax</span><b>{_e(t.setting("pickup_minutes", "20"))} min · {_e(t.setting("tax_rate", ""))}</b></div></div></div>
<div class="card"><div class="card-h"><h3>Point of sale</h3></div><div class="card-b">
<div class="kv"><span>Provider</span><b>{_e(provider or "—")}</b></div>
<div class="kv"><span>Credentials</span><b>{"saved (" + _e(", ".join(secret_fields)) + "; values hidden)" if secret_fields else '<span class="pill warn">none saved</span>'}</b></div>
<div class="kv"><span>Open alerts</span><b>{len(tickets)}</b></div>
{"".join(f'<p class="small" style="margin:6px 0"><span class="pill bad">{_e(x["severity"])}</span> {_e(x["title"])}</p>' for x in tickets[:5])}
</div></div>
<div class="card"><div class="card-h"><h3>Features</h3></div><div class="card-b">{flag_rows}</div></div>
</div><div class="stack" style="gap:28px">
<div class="card"><div class="card-h"><h3>Staff logins</h3></div><div class="card-b">{user_rows}
<form data-post="/admin/api/restaurants/{_e(t.id)}/users" style="margin-top:18px">
<div class="win"><input name="email" type="email" placeholder="email" required style="flex:1;min-width:180px">
<select name="role" style="width:auto"><option value="kitchen">Kitchen</option><option value="admin">Admin</option></select></div>
<input name="password" type="password" minlength="8" placeholder="Temporary password (8+)" required style="margin-top:10px">
<div class="actions" style="margin-top:12px"><button class="btn ghost sm" type="submit">Add login</button></div></form>
<form data-post="/admin/api/restaurants/{_e(t.id)}/password" style="margin-top:18px">
<div class="win"><select name="user_id" style="width:auto">{"".join(f'<option value="{_e(u.id)}">{_e(u.email)}</option>' for u in users)}</select>
<input name="password" type="password" minlength="8" placeholder="New password" required style="flex:1;min-width:160px">
<button class="btn ghost sm" type="submit">Reset password</button></div></form></div></div>
<div class="card"><div class="card-h"><h3>Recent calls</h3><span class="r mut small">7 days</span></div>
<div class="tbl-wrap"><table><tbody>{call_rows}</tbody></table></div></div>
<div class="card"><div class="card-h"><h3>Audit history</h3></div>
<div class="tbl-wrap"><table><tbody>{audit_rows}</tbody></table></div></div>
</div></div>"""
    return _shell(deps, admin, "restaurants", t.name, body)


def _operations(deps: AdminDeps, admin: PlatformUser) -> str:
    s, now = deps.tenants, time.time()
    names = {t.id: t for t in s.list_tenants(active_only=False)}
    calls = s.platform_calls(now - 86400, 500)
    live = [c for c in calls if c["status"] == "live" and now - (c["updated_at"] or 0) < _LIVE_STALE_SECONDS]
    attention = [c for c in calls if (c["meta"] or {}).get("route") in ("voicemail", "closed", "ai_off")
                 or (c["meta"] or {}).get("transfer_result") not in (None, "completed")]
    pending = s.platform_pending_approvals()

    def name(tid):
        t = names.get(tid)
        return f'<a href="/admin/restaurants/{_e(tid)}">{_e(t.name if t else tid)}</a>'
    live_rows = "".join(f'<tr><td>{name(c["tenant_id"])}</td><td class="mut">{_local(c["started_at"], names.get(c["tenant_id"]))}</td>'
                        f'<td>{_e((c["meta"] or {}).get("route", "ai"))}</td></tr>' for c in live) \
        or '<tr><td class="mut">No live calls.</td></tr>'
    pend_rows = "".join(f'<tr><td>{name(a["tenant_id"])}</td><td>{_e(a["request_text"])}</td>'
                        f'<td class="mut">{max(0, int(a["deadline_at"] - now))}s left</td></tr>' for a in pending) \
        or '<tr><td class="mut">No kitchen requests waiting.</td></tr>'
    att_rows = "".join(
        f'<tr><td>{name(c["tenant_id"])}</td><td class="mut">{_local(c["started_at"], names.get(c["tenant_id"]))}</td>'
        f'<td>{_e((c["meta"] or {}).get("route", ""))}</td><td>{_e((c["meta"] or {}).get("transfer_result", ""))}</td>'
        f'<td class="mut small">{_e((c["meta"] or {}).get("reason", ""))}</td></tr>' for c in attention[:50]) \
        or '<tr><td class="mut">Nothing in the last 24 hours.</td></tr>'
    body = _page_head("Live operations", f"Across all restaurants · refreshed {_local(now)}",
                      '<button class="btn ghost" onclick="location.reload()">Refresh</button>') + f"""
<div class="grid-2"><div class="card"><div class="card-h"><h3>Live calls</h3><span class="r pill ok">{len(live)}</span></div>
<div class="tbl-wrap"><table><tbody>{live_rows}</tbody></table></div></div>
<div class="card"><div class="card-h"><h3>Kitchen requests waiting</h3><span class="r pill warn">{len(pending)}</span></div>
<div class="tbl-wrap"><table><tbody>{pend_rows}</tbody></table></div></div></div>
<div class="card" style="margin-top:28px"><div class="card-h"><h3>Calls handled without the AI · missed transfers (24 h)</h3></div>
<div class="tbl-wrap"><table><thead><tr><th>Restaurant</th><th>When</th><th>Route</th><th>Transfer</th><th>Why</th></tr></thead>
<tbody>{att_rows}</tbody></table></div></div>"""
    return _shell(deps, admin, "operations", "Live operations", body)


def _rollout(deps: AdminDeps, admin: PlatformUser) -> str:
    s = deps.tenants
    tenants = s.list_tenants(active_only=False)
    head = "".join(f"<th>{_e(_FLAG_LABELS[f])}</th>" for f in rbac.KNOWN_FLAGS)
    platform = s.saved_flags("*")  # absent = never switched = allowed
    killed = {f: platform.get(f) is False or rbac.env_killed(f) for f in rbac.KNOWN_FLAGS}
    confirm_kill = 'data-confirm="Turn this feature off for EVERY restaurant?"'
    kill_cells = "".join(
        f'<td><span class="pill {"bad" if killed[f] else "ok"}">{"killed" if killed[f] else "allowed"}</span><br>'
        f'<button class="linkbtn" data-act="/admin/api/flags/global" data-body=\'{json.dumps({"flag": f, "enabled": killed[f]})}\' '
        f'{"" if killed[f] else confirm_kill}>'
        f'{"Allow again" if killed[f] else "Kill everywhere"}</button></td>' for f in rbac.KNOWN_FLAGS)
    rows = "".join(
        f'<tr><td><a href="/admin/restaurants/{_e(t.id)}">{_e(t.name)}</a></td>'
        + "".join(
            f'<td><button class="btn {"" if v else "ghost "}sm" data-act="/admin/api/restaurants/{_e(t.id)}/flags" '
            f'data-body=\'{json.dumps({"flag": f, "enabled": not v})}\'>{"On" if v else "Off"}</button></td>'
            for f, v in s.list_flags(t.id).items())
        + "</tr>" for t in tenants)
    body = _page_head("Feature rollout", "Turn features on one restaurant at a time; kill switches override everyone.") + f"""
<div class="card"><div class="tbl-wrap"><table><thead><tr><th>Restaurant</th>{head}</tr></thead>
<tbody><tr style="background:#fbfbfc"><td><b>Platform switch</b></td>{kill_cells}</tr>{rows}</tbody></table></div></div>
<p class="mut small">Environment kill switch: <code>DISABLED_FEATURES</code> overrides everything here.</p>"""
    return _shell(deps, admin, "rollout", "Feature rollout", body)


def _usage(deps: AdminDeps, admin: PlatformUser) -> str:
    s, now = deps.tenants, time.time()
    month = datetime.fromtimestamp(now).strftime("%Y-%m")
    usage = s.usage_by_tenant(month)
    rows = "".join(
        f'<tr><td><a href="/admin/restaurants/{_e(t.id)}">{_e(t.name)}</a></td>'
        f'<td>{u.get("calls", 0)}</td><td>{u.get("talk_minutes", 0):.1f}</td><td>{u.get("tts_chars", 0):,}</td>'
        f'<td>{u.get("sms_sent", 0)}</td><td class="mut small">{_ago(u.get("computed_at"), now)}</td></tr>'
        for t in s.list_tenants(active_only=False) for u in [usage.get(t.id, {})])
    total = {k: sum(u.get(k, 0) for u in usage.values()) for k in ("calls", "talk_minutes", "tts_chars", "sms_sent")}
    body = _page_head("Usage", f"{datetime.fromtimestamp(now).strftime('%B %Y')} · measured by the billing agent (daily)") + f"""
<div class="card"><div class="tbl-wrap"><table><thead><tr><th>Restaurant</th><th>Calls</th><th>Talk min</th>
<th>Voice chars</th><th>SMS</th><th>Last rollup</th></tr></thead><tbody>{rows}
<tr style="font-weight:700"><td>Total</td><td>{total["calls"]}</td><td>{total["talk_minutes"]:.1f}</td>
<td>{total["tts_chars"]:,}</td><td>{total["sms_sent"]}</td><td></td></tr></tbody></table></div></div>
<p class="mut small">Counts only. Costs depend on your Twilio, ElevenLabs and Anthropic plans and aren't estimated here.</p>"""
    return _shell(deps, admin, "usage", "Usage", body)


def _audit(deps: AdminDeps, admin: PlatformUser, tenant_id: str, action: str) -> str:
    s = deps.tenants
    names = {t.id: t.name for t in s.list_tenants(active_only=False)}
    events = s.list_audit(tenant_id or None, 200, action_prefix=action)
    t_opts = '<option value="">All restaurants</option>' + "".join(
        f'<option value="{_e(i)}"{" selected" if i == tenant_id else ""}>{_e(n)}</option>' for i, n in names.items())
    rows = "".join(
        f'<tr><td class="mut small">{_local(e["at"])}</td><td>{_e(names.get(e["tenant_id"], "platform") if e["tenant_id"] else "platform")}</td>'
        f'<td><b>{_e(e["action"])}</b><div class="mut small">{_e(e["resource"])}</div></td>'
        f'<td class="small">{_e(e["actor_type"])} <span class="mut">{_e(e["actor_id"][:12])}</span></td>'
        f'<td class="small mono" style="max-width:340px;word-break:break-word">{_e(json.dumps(e["after"]))[:200]}</td></tr>'
        for e in events) or '<tr><td colspan=5 class="mut">No events.</td></tr>'
    body = _page_head("Audit log", "Every change, newest first. Secrets are never stored here.") + f"""
<form method="get" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px">
<select name="tenant" style="width:auto">{t_opts}</select>
<input name="action" value="{_e(action)}" placeholder="Action starts with… e.g. admin., flag., webhook." style="max-width:340px">
<button class="btn ghost" type="submit">Filter</button></form>
<div class="card"><div class="tbl-wrap"><table><thead><tr><th>When</th><th>Restaurant</th><th>Action</th><th>By</th><th>New value</th></tr></thead>
<tbody>{rows}</tbody></table></div></div>"""
    return _shell(deps, admin, "audit", "Audit log", body)


# --- router -------------------------------------------------------------------------------
def build_admin_router(deps: AdminDeps) -> APIRouter:
    router = APIRouter()
    s = deps.tenants

    def session_admin(request: Request) -> PlatformUser | None:
        raw = request.cookies.get(COOKIE_NAME, "")
        data = crypto.read_session_cookie(raw, purpose="admin") if raw else None
        if not data or data.get("kind") != "platform":
            return None
        return s.get_platform_user(str(data.get("pid", "")))

    def page_admin(request: Request, permission: str) -> PlatformUser | RedirectResponse:
        admin = session_admin(request)
        if admin is None:
            return RedirectResponse("/admin/login", status_code=302)
        if not rbac.platform_can(admin.role, permission):
            raise HTTPException(status_code=403, detail="not allowed")
        return admin

    def api_admin(request: Request, permission: str) -> PlatformUser:
        admin = session_admin(request)
        if admin is None:
            raise HTTPException(status_code=401, detail="admin login required")
        if not rbac.platform_can(admin.role, permission):
            raise HTTPException(status_code=403, detail="not allowed")
        return admin

    def tenant_or_404(tenant_id: str) -> Tenant:
        t = s.get_tenant(tenant_id)
        if t is None:
            raise HTTPException(status_code=404, detail="restaurant not found")
        return t

    # -- auth --------------------------------------------------------------------
    @router.get("/admin/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if session_admin(request):
            return RedirectResponse("/admin/", status_code=302)
        return _login_page(deps.platform_name)

    @router.post("/admin/login")
    async def login(request: Request):
        form = await request.form()
        email = str(form.get("email", "")).strip().lower()
        now = time.time()
        recent = [t for t in _FAILED_LOGINS.get(email, []) if now - t < _FAILURE_WINDOW]
        if len(recent) >= _MAX_FAILURES:
            s.audit("platform", email, None, "admin.login_locked", "admin")
            return HTMLResponse(_login_page(deps.platform_name, "Too many attempts. Try again in 15 minutes."),
                                status_code=429)
        admin = s.verify_platform_user(email, str(form.get("password", "")))
        if admin is None:
            _FAILED_LOGINS[email] = recent + [now]
            s.audit("platform", email, None, "admin.login_failed", "admin")
            return HTMLResponse(_login_page(deps.platform_name, "Invalid email or password."), status_code=401)
        _FAILED_LOGINS.pop(email, None)
        s.audit("platform", admin.id, None, "admin.login", "admin", None, {"email": admin.email})
        resp = RedirectResponse("/admin/", status_code=302)
        resp.set_cookie(COOKIE_NAME, crypto.make_session_cookie(
            {"pid": admin.id, "kind": "platform"}, ttl_seconds=_SESSION_SECONDS, purpose="admin"),
            max_age=_SESSION_SECONDS, httponly=True, samesite="strict", path="/admin")
        return resp

    @router.get("/admin/logout")
    async def logout():
        resp = RedirectResponse("/admin/login", status_code=302)
        resp.delete_cookie(COOKIE_NAME, path="/admin")
        return resp

    # -- pages -------------------------------------------------------------------
    def page(permission: str, render: Callable[[PlatformUser, Request], str]):
        async def handler(request: Request):
            res = page_admin(request, permission)
            return res if isinstance(res, RedirectResponse) else HTMLResponse(render(res, request))
        return handler

    router.get("/admin/", response_class=HTMLResponse)(
        page("tenants.view", lambda a, r: _overview(deps, a)))
    router.get("/admin/restaurants", response_class=HTMLResponse)(
        page("tenants.view", lambda a, r: _restaurants(deps, a, r.query_params.get("q", ""),
                                                       r.query_params.get("status", ""))))
    router.get("/admin/operations", response_class=HTMLResponse)(
        page("diagnostics.view", lambda a, r: _operations(deps, a)))
    router.get("/admin/rollout", response_class=HTMLResponse)(
        page("flags.edit", lambda a, r: _rollout(deps, a)))
    router.get("/admin/usage", response_class=HTMLResponse)(
        page("tenants.view", lambda a, r: _usage(deps, a)))
    router.get("/admin/audit", response_class=HTMLResponse)(
        page("audit.view", lambda a, r: _audit(deps, a, r.query_params.get("tenant", ""),
                                                r.query_params.get("action", ""))))

    @router.get("/admin/restaurants/{tenant_id}", response_class=HTMLResponse)
    async def restaurant_page(request: Request, tenant_id: str):
        res = page_admin(request, "tenants.view")
        if isinstance(res, RedirectResponse):
            return res
        return _restaurant(deps, res, tenant_or_404(tenant_id))

    # -- actions (JSON) ------------------------------------------------------------
    @router.post("/admin/api/restaurants")
    async def create_restaurant(request: Request):
        admin = api_admin(request, "tenants.edit")
        body = await request.json()
        name = str(body.get("name", "")).strip()
        zone = str(body.get("timezone", ""))
        if not name:
            return JSONResponse({"ok": False, "error": "restaurant name is required"}, status_code=400)
        if zone and zone not in dict(TIMEZONES):
            return JSONResponse({"ok": False, "error": "unknown time zone"}, status_code=400)
        try:
            t = s.create_tenant(name, str(body.get("phone", "")))
            s.set_settings(t.id, {"restaurant_name": name, "pos_profile": "", "timezone": zone})
            user = s.create_user(t.id, str(body.get("admin_email", "")),
                                 str(body.get("admin_password", "")), role="admin")
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        s.audit("platform", admin.id, t.id, "admin.restaurant_created", f"tenant:{t.id}", None,
                {"name": name, "phone": t.phone_number, "admin_email": user.email})
        return {"ok": True, "id": t.id}

    @router.post("/admin/api/restaurants/{tenant_id}/status")
    async def set_status(request: Request, tenant_id: str):
        admin = api_admin(request, "tenants.edit")
        tenant_or_404(tenant_id)
        try:
            s.set_tenant_status(tenant_id, str((await request.json()).get("status", "")), admin.id)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True}

    @router.post("/admin/api/restaurants/{tenant_id}/flags")
    async def set_flag(request: Request, tenant_id: str):
        admin = api_admin(request, "flags.edit")
        tenant_or_404(tenant_id)
        body = await request.json()
        try:
            s.set_flag(tenant_id, str(body.get("flag", "")), bool(body.get("enabled")), actor=admin.id)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True}

    @router.post("/admin/api/flags/global")
    async def set_global_flag(request: Request):
        admin = api_admin(request, "flags.edit")
        body = await request.json()
        try:
            s.set_flag("*", str(body.get("flag", "")), bool(body.get("enabled")), actor=admin.id)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True}

    @router.post("/admin/api/emergency")
    async def platform_emergency(request: Request):
        admin = api_admin(request, "flags.edit")
        s.set_routing_emergency("*", bool((await request.json()).get("off")), admin.id)
        return {"ok": True}

    @router.post("/admin/api/restaurants/{tenant_id}/emergency")
    async def tenant_emergency(request: Request, tenant_id: str):
        admin = api_admin(request, "tenants.edit")
        tenant_or_404(tenant_id)
        s.set_routing_emergency(tenant_id, bool((await request.json()).get("off")), admin.id)
        return {"ok": True}

    @router.post("/admin/api/restaurants/{tenant_id}/users")
    async def add_user(request: Request, tenant_id: str):
        admin = api_admin(request, "tenants.edit")
        tenant_or_404(tenant_id)
        body = await request.json()
        try:
            u = s.create_user(tenant_id, str(body.get("email", "")), str(body.get("password", "")),
                              role=str(body.get("role", "kitchen")))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        s.audit("platform", admin.id, tenant_id, "admin.user_added", f"user:{u.id}", None,
                {"email": u.email, "role": u.role})
        return {"ok": True}

    @router.post("/admin/api/restaurants/{tenant_id}/users/{user_id}/role")
    async def set_role(request: Request, tenant_id: str, user_id: str):
        admin = api_admin(request, "tenants.edit")
        tenant_or_404(tenant_id)
        role = str((await request.json()).get("role", ""))
        try:
            if not s.set_user_role(tenant_id, user_id, role):
                raise HTTPException(status_code=404, detail="login not found")
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        s.audit("platform", admin.id, tenant_id, "admin.user_role", f"user:{user_id}", None, {"role": role})
        return {"ok": True}

    @router.post("/admin/api/restaurants/{tenant_id}/password")
    async def reset_password(request: Request, tenant_id: str):
        admin = api_admin(request, "tenants.edit")
        tenant_or_404(tenant_id)
        body = await request.json()
        try:
            if not s.reset_user_password(tenant_id, str(body.get("user_id", "")),
                                         str(body.get("password", "")), admin.id):
                raise HTTPException(status_code=404, detail="login not found")
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return {"ok": True}

    @router.get("/admin/api/health")
    async def health(request: Request):
        api_admin(request, "diagnostics.view")
        now = time.time()
        return {"restaurants": len(s.list_tenants(active_only=False)),
                "platform_emergency": s.platform_emergency(),
                "pending_approvals": len(s.platform_pending_approvals()),
                "jobs": s.job_status(), "as_of": now}

    return router
