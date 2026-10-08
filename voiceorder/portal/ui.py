"""Shared look for the tenant portal: styles, icons, page shell, and charts.

Pages in portal.py build their body HTML and wrap it with `shell()` (signed
in) or `auth_shell()` (login / signup). Everything interpolated here is
escaped by the caller or by these helpers.
"""
from __future__ import annotations

import html

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600'
    '&family=Space+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">'
)

CSS = """
:root{
  --side:#0e1013;--side-2:#1b1e23;--side-line:#23272e;--side-txt:#e9ebee;--side-mut:#8b919b;
  --bg:#f6f8fa;--card:#ffffff;--line:#e4e7ec;--line-2:#eef0f3;--txt:#14171c;--mut:#5b6575;
  --acc:#0fb36b;--acc-ink:#0b7a4a;--acc-soft:#e3f7ec;--acc-line:#bfead2;
  --warn:#b45309;--warn-soft:#fdf1dc;--bad:#c2410c;--bad-soft:#fde7dd;--gray-soft:#eef1f5;
  --font:"Space Grotesk",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--txt);font-family:var(--font);font-size:15px;
  -webkit-font-smoothing:antialiased}
a{color:var(--acc-ink);text-decoration:none}
a:hover{text-decoration:underline}
.mono{font-family:var(--mono)}
.mut{color:var(--mut)}.small{font-size:13px}

/* ---- shell ---- */
.app{display:flex;min-height:100vh}
.side{width:300px;flex:none;background:var(--side);color:var(--side-txt);display:flex;
  flex-direction:column;position:sticky;top:0;height:100vh;transition:width .18s ease}
.brand{display:flex;align-items:center;gap:14px;padding:28px 28px 18px;font-weight:700;
  font-size:22px;letter-spacing:-.01em;color:#fff}
.brand:hover{text-decoration:none}
.logo{width:40px;height:40px;border-radius:9px;background:var(--acc);color:#fff;flex:none;
  display:grid;place-items:center;font-size:22px;font-weight:700}
.navs{flex:1;overflow-y:auto;padding:4px 20px 20px}
.nav-h{font-family:var(--mono);font-size:12px;letter-spacing:.06em;color:var(--side-mut);
  text-transform:uppercase;margin:26px 8px 10px}
.nav-a{display:flex;align-items:center;gap:14px;padding:12px 14px 12px 34px;border-radius:9px;
  color:#c9cdd3;font-size:16px;position:relative;margin:2px 0}
.nav-a:hover{background:var(--side-2);color:#fff;text-decoration:none}
.nav-a.on{background:var(--side-2);color:#fff}
.nav-a.on::before{content:"";position:absolute;left:14px;top:50%;width:7px;height:7px;
  margin-top:-3.5px;border-radius:50%;background:var(--acc)}
.nav-a .count{margin-left:auto;font-family:var(--mono);font-size:12px;color:var(--acc)}
.nav-a svg{flex:none;opacity:.9}
.side-foot{border-top:1px solid var(--side-line);padding:18px 24px;display:flex;align-items:center;gap:12px}
.av{width:42px;height:42px;border-radius:9px;background:var(--side-2);color:#d6d9de;flex:none;
  display:grid;place-items:center;font-family:var(--mono);font-size:13px;font-weight:600}
.side-foot .who{min-width:0;flex:1}
.side-foot .who b{display:block;font-size:15px;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.side-foot .who span{font-size:13px;color:var(--side-mut)}
.side-foot a.out{color:var(--side-mut);display:grid;place-items:center;width:34px;height:34px;border-radius:8px}
.side-foot a.out:hover{background:var(--side-2);color:#fff}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.top{height:88px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;
  gap:20px;padding:0 40px;position:sticky;top:0;z-index:4}
.top h1{margin:0;font-size:26px;font-weight:700;letter-spacing:-.01em;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.icon-btn{width:38px;height:38px;border-radius:8px;border:0;background:transparent;color:var(--txt);
  display:grid;place-items:center;cursor:pointer;flex:none}
.icon-btn:hover{background:var(--gray-soft)}
.top .right{margin-left:auto;display:flex;align-items:center;gap:14px}
.badge{display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:12.5px;
  letter-spacing:.04em;text-transform:uppercase;padding:8px 14px;border-radius:7px;
  background:var(--acc-soft);color:var(--acc-ink);border:1px solid var(--acc-line);white-space:nowrap}
.badge::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--acc)}
.badge.warn{background:var(--warn-soft);color:var(--warn);border-color:#f5d9a8}
.badge.warn::before{background:#e59a1a}
.top .av{background:var(--gray-soft);color:#4a5462;border-radius:50%}
.content{padding:40px 40px 72px;max-width:1240px;width:100%}

/* collapsed sidebar (desktop) */
body.collapsed .side{width:84px}
body.collapsed .brand span,body.collapsed .nav-h,body.collapsed .nav-a .lbl,
body.collapsed .nav-a .count,body.collapsed .side-foot .who,body.collapsed .side-foot a.out{display:none}
body.collapsed .brand{padding:28px 22px 18px}
body.collapsed .navs{padding:4px 14px}
body.collapsed .nav-a{padding:12px;justify-content:center}
body.collapsed .nav-a.on::before{left:4px}
body.collapsed .side-foot{padding:18px 21px}

/* ---- page parts ---- */
.page-h{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:28px}
.page-h h2{font-size:34px;margin:0 0 8px;letter-spacing:-.015em}
.page-h p{margin:0;color:var(--mut)}
.page-h .acts{margin-left:auto;display:flex;gap:12px;flex-wrap:wrap}
.eyebrow{font-family:var(--mono);font-size:13px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut)}
.row-between{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px}
.card-h{display:flex;align-items:center;gap:12px;padding:24px 28px;border-bottom:1px solid var(--line)}
.card-h h3{margin:0;font-size:19px;font-weight:600}
.card-h .r{margin-left:auto}
.card-b{padding:24px 28px}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:24px;margin:20px 0 40px}
.kpi{padding:24px 26px;min-height:150px}
.kpi .l{color:var(--mut);font-size:16px}
.kpi .v{font-size:36px;font-weight:700;letter-spacing:-.02em;margin-top:22px;display:flex;
  align-items:baseline;gap:12px;flex-wrap:wrap}
.kpi .d{font-size:13px;font-weight:600}
.kpi.ok{background:var(--acc-soft);border-color:var(--acc-line)}
.kpi.ok .l{color:var(--acc-ink)}
.kpi.warn{background:var(--warn-soft);border-color:#f5d9a8}
.kpi.warn .l{color:var(--warn)}
.status-line{display:flex;align-items:center;gap:10px;font-size:20px;font-weight:700;margin-top:30px;
  text-transform:uppercase;letter-spacing:.01em}
.dot{width:8px;height:8px;border-radius:50%;background:var(--acc);flex:none;display:inline-block}
.dot.warn{background:#e59a1a}.dot.gray{background:#b5bcc7}
.up{color:var(--acc-ink)}.down{color:var(--bad)}.amber{color:var(--warn)}
.grid-main{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(0,1fr);gap:40px;align-items:start}
.grid-2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:40px;align-items:start}
.stack{display:flex;flex-direction:column;gap:40px}
.grid-main>*,.grid-2>*,.stack>*,.kpis>*{min-width:0}

table{width:100%;border-collapse:collapse}
th{font-family:var(--mono);font-weight:500;font-size:13px;letter-spacing:.04em;text-transform:uppercase;
  color:var(--mut);text-align:left;padding:18px 24px;background:#f8fafc;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:22px 24px;border-bottom:1px solid var(--line-2);vertical-align:middle}
tr:last-child td{border-bottom:0}
td.id{font-family:var(--mono);font-size:14px;white-space:nowrap}
.tbl-wrap{overflow-x:auto}
.pill{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;
  padding:6px 11px;border-radius:6px;background:var(--gray-soft);color:#4a5462;white-space:nowrap}
.pill.ok{background:var(--acc-soft);color:var(--acc-ink)}
.pill.warn{background:var(--warn-soft);color:var(--warn)}
.pill.bad{background:var(--bad-soft);color:var(--bad)}
.empty{padding:36px 28px;color:var(--mut);text-align:center}

.btn{display:inline-flex;align-items:center;gap:10px;padding:12px 22px;border-radius:8px;border:1px solid transparent;
  background:var(--acc);color:#fff;font:600 16px var(--font);cursor:pointer;text-decoration:none;white-space:nowrap}
.btn:hover{filter:brightness(.96);text-decoration:none}
.btn.ghost{background:#fff;color:var(--txt);border-color:var(--line);box-shadow:0 1px 2px rgba(16,24,40,.06)}
.btn.ghost:hover{background:#f9fafb}
.btn.sm{padding:8px 14px;font-size:14px}
.btn:disabled{opacity:.55;cursor:default}
.btn.block{width:100%;justify-content:center}

label.f{display:block;font-weight:500;font-size:16px;margin:24px 0 10px}
label.f:first-child{margin-top:0}
.help{font-size:13px;color:var(--mut);margin:8px 0 0}
input,select,textarea{width:100%;padding:14px 18px;border-radius:8px;border:1px solid var(--line);
  background:#fff;color:var(--txt);font:16px var(--font)}
input:focus,select:focus,textarea:focus{outline:2px solid var(--acc-line);border-color:var(--acc)}
textarea{min-height:150px;resize:vertical}
.alert{padding:16px 22px;border-radius:9px;margin:20px 0;font-size:15px;line-height:1.5;
  background:#f8fafc;border:1px solid var(--line);color:var(--mut)}
.alert.ok{background:var(--acc-soft);border-color:var(--acc-line);color:var(--acc-ink)}
.alert.bad{background:var(--bad-soft);border-color:#f6c9b5;color:var(--bad)}
.alert.warn{background:var(--warn-soft);border-color:#f5d9a8;color:var(--warn)}
.seg{display:flex;gap:16px;flex-wrap:wrap;margin:6px 0 24px}
.seg button{padding:12px 24px;border-radius:8px;border:1px solid var(--line);background:#fff;color:var(--txt);
  font:500 17px var(--font);cursor:pointer;box-shadow:0 1px 2px rgba(16,24,40,.06)}
.seg button.on{background:var(--acc);border-color:var(--acc);color:#fff}
.kv{display:flex;justify-content:space-between;gap:16px;padding:22px 0;border-bottom:1px solid var(--line)}
.kv:first-child{padding-top:4px}
.kv:last-child{border-bottom:0}
.kv b{text-align:right;font-weight:600}
.sec-h{font-size:22px;font-weight:600;margin:0 0 22px}
.actions{display:flex;gap:16px;flex-wrap:wrap;margin-top:32px}

/* lists */
.call{display:flex;align-items:center;gap:16px;padding:18px 0;border-bottom:1px solid var(--line)}
.call .ic{width:52px;height:52px;border-radius:9px;background:var(--gray-soft);color:#6b7584;
  display:grid;place-items:center;flex:none}
.call .who{flex:1;min-width:0}
.call .who b{display:block;font-size:17px;font-weight:600}
.call .meta{font-family:var(--mono);font-size:12px;letter-spacing:.03em;color:var(--mut);margin-top:6px;
  text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.call.live{background:var(--acc-soft);border:1px solid var(--acc-line);border-radius:10px;padding:20px;margin:0 0 6px}
.call.live .ic{background:#cfeedd;color:var(--acc)}
.call.live .meta{color:var(--acc-ink)}
.bar{height:4px;border-radius:9px;background:#d8efe3;margin-top:12px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--acc);border-radius:9px}
.call .go{font-size:14px;font-weight:600;white-space:nowrap}
.rank{display:flex;justify-content:space-between;gap:16px;padding:24px 0;border-bottom:1px solid var(--line);font-size:17px}
.rank:last-child{border-bottom:0}
.rank span:last-child{color:var(--mut)}

.dark{background:var(--side);color:#fff;border-radius:12px;padding:44px 38px}
.dark .eyebrow{color:#9aa1ab}
.dark h3{font-size:30px;margin:16px 0 40px;letter-spacing:-.01em}
.dark .nums{display:flex;gap:56px;flex-wrap:wrap}
.dark .n{font-size:38px;font-weight:700;letter-spacing:-.02em}
.dark .n.g{color:#22c47c}.dark .n.o{color:#f59e0b}
.dark .nl{font-family:var(--mono);font-size:13px;letter-spacing:.06em;color:#9aa1ab;margin-top:10px;text-transform:uppercase}

/* charts */
.legend{display:flex;gap:18px;font-size:14px;color:var(--mut)}
.legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:middle}
.chart-x{display:flex;justify-content:space-between;font-family:var(--mono);font-size:13px;color:var(--mut);margin-top:14px}
.bars{display:flex;align-items:flex-end;gap:14px;height:340px;padding-top:30px;
  background:linear-gradient(var(--line-2) 1px,transparent 1px) 0 30px/100% 25%}
.bars .col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;min-width:0}
.bars .val{font-family:var(--mono);font-size:13px;color:#4a5462;margin-bottom:10px}
.bars .b{width:100%;max-width:44px;background:var(--acc);border-radius:2px;min-height:2px}
.bars-x{display:flex;gap:14px;margin-top:14px}
.bars-x span{flex:1;text-align:center;font-family:var(--mono);font-size:12.5px;color:var(--mut);min-width:0;overflow:hidden}

/* transcript */
.bubble{max-width:78%;padding:14px 18px;border-radius:12px;margin:10px 0;line-height:1.5}
.bubble .w{font-family:var(--mono);font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);margin-bottom:6px}
.bubble.caller{background:var(--gray-soft)}
.bubble.ai{background:var(--acc-soft);margin-left:auto}

details.faq{border-bottom:1px solid var(--line);padding:24px 0}
details.faq summary{cursor:pointer;font-size:18px;font-weight:500;list-style:none;display:flex;gap:12px;align-items:center}
details.faq summary::-webkit-details-marker{display:none}
details.faq summary::before{content:"";border-left:8px solid var(--txt);border-top:6px solid transparent;
  border-bottom:6px solid transparent;transition:transform .15s}
details.faq[open] summary::before{transform:rotate(90deg)}
details.faq p{color:var(--mut);margin:14px 0 0 20px;line-height:1.6}

/* auth */
.auth{min-height:100vh;display:grid;place-items:center;padding:24px 16px;background:var(--bg)}
.auth .card{width:100%;max-width:460px;padding:40px}
.auth .brand{color:var(--txt);padding:0 0 28px}
.auth h2{font-size:28px;margin:0 0 6px}

@media(max-width:1100px){
  .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
  .grid-main,.grid-2{grid-template-columns:minmax(0,1fr)}
}
@media(max-width:860px){
  .side{position:fixed;left:0;top:0;z-index:20;transform:translateX(-100%);transition:transform .2s ease;width:280px}
  body.nav-open .side{transform:none;box-shadow:0 0 0 100vmax rgba(0,0,0,.35)}
  body.collapsed .side{width:280px}
  body.collapsed .brand span,body.collapsed .nav-h,body.collapsed .nav-a .lbl,body.collapsed .nav-a .count,
  body.collapsed .side-foot .who,body.collapsed .side-foot a.out{display:revert}
  .top{padding:0 16px;height:72px}
  .top h1{font-size:20px}
  .top .badge{display:none}
  .content{padding:24px 16px 56px}
  .page-h h2{font-size:28px}
  .kpis{grid-template-columns:minmax(0,1fr);gap:16px}
  .grid-main,.grid-2,.stack{gap:24px}
  .card-h,.card-b{padding:20px}
  th,td{padding:14px 16px}
  .dark{padding:30px 24px}
  .bars{height:240px;gap:6px}.bars-x{gap:6px}
}
"""

# Lucide-style 24px stroke icons (MIT). Paths only; wrapped by icon().
_ICONS = {
    "grid": '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/>'
            '<rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
    "chart": '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M7 16v-3"/><path d="M11 16v-6"/>'
             '<path d="M15 16v-4"/><path d="M19 16V8"/><path d="m7 9 4-4 4 3 5-5"/>',
    "bag": '<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4Z"/><path d="M3 6h18"/>'
           '<path d="M16 10a4 4 0 0 1-8 0"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>'
             '<path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "phone": '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 '
             '19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 '
             '1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.91.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/>',
    "pos": '<rect x="3" y="10" width="11" height="11" rx="1"/><path d="M14 14h7v7h-7z"/>'
           '<rect x="14" y="3" width="7" height="7" rx="1"/><path d="M3 10V5a2 2 0 0 1 2-2h5v7"/>',
    "megaphone": '<path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
    "gauge": '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    "help": '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    "gear": '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 '
            '0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 '
            '0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 '
            '2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 '
            '2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 '
            '2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>'
            '<circle cx="12" cy="12" r="3"/>',
    "panel": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18"/><path d="m16 15-3-3 3-3"/>',
    "menu": '<path d="M4 6h16"/><path d="M4 12h16"/><path d="M4 18h16"/>',
    "arrow": '<path d="M7 17 17 7"/><path d="M7 7h10v10"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>'
            '<path d="M17 21v-8H7v8"/><path d="M7 3v5h8"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    "play": '<path d="m6 3 14 9-14 9V3z"/>',
    "back": '<path d="m15 18-6-6 6-6"/>',
}


def icon(name: str, size: int = 22) -> str:
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{_ICONS[name]}</svg>')


NAV = [
    ("Command", [
        ("dash", "Dashboard", "/portal/", "grid"),
        ("stats", "Statistics", "/portal/statistics", "chart"),
        ("orders", "Orders", "/portal/orders", "bag"),
        ("customers", "Customers", "/portal/customers", "users"),
        ("calls", "Live calls", "/portal/calls", "phone"),
    ]),
    ("Management", [
        ("pos", "POS setup", "/portal/pos", "pos"),
        ("marketing", "Marketing", "/portal/marketing", "megaphone"),
        ("usage", "Usage", "/portal/usage", "gauge"),
    ]),
    ("Settings", [
        ("support", "Support", "/portal/support", "help"),
        ("settings", "System settings", "/portal/settings", "gear"),
    ]),
]


def initials(name: str) -> str:
    words = [w for w in name.replace("&", " ").split() if w[:1].isalnum()]
    letters = "".join(w[0] for w in words[:2]) or name[:2]
    return html.escape(letters.upper())


# Restores the collapsed sidebar before paint and wires the toggle button.
_SHELL_JS = """
(function(){
  var b=document.body, mq=window.matchMedia('(max-width:860px)');
  try{ if(localStorage.getItem('vo_side')==='1') b.classList.add('collapsed'); }catch(e){}
  document.getElementById('side-toggle').addEventListener('click', function(){
    if(mq.matches){ b.classList.toggle('nav-open'); return; }
    b.classList.toggle('collapsed');
    try{ localStorage.setItem('vo_side', b.classList.contains('collapsed')?'1':'0'); }catch(e){}
  });
  document.addEventListener('click', function(e){
    if(b.classList.contains('nav-open') && !e.target.closest('.side') && !e.target.closest('#side-toggle'))
      b.classList.remove('nav-open');
  });
  // Render <time data-ts> in the viewer's own clock when no tenant zone is set.
  document.querySelectorAll('time[data-ts]').forEach(function(t){
    if(t.textContent) return;
    var d=new Date(parseFloat(t.dataset.ts)*1000);
    var f=t.dataset.f;
    t.textContent = f==='date' ? d.toLocaleDateString([], {month:'short', day:'numeric', year:'numeric'})
      : f==='datetime' ? d.toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'})
      : d.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
  });
})();
"""


def shell(*, title: str, body: str, tenant_name: str, active: str, platform: str,
          live_calls: int = 0, status_label: str = "", status_ok: bool = True,
          location: str = "Primary location", visible: set[str] | None = None) -> str:
    """Signed-in page: dark sidebar + white top bar + light content area."""
    groups = []
    for heading, links in NAV:
        items = []
        for key, label, href, ic in links:
            if visible is not None and key not in visible:
                continue
            count = (f'<span class="count">{live_calls}</span>'
                     if key == "calls" and live_calls else "")
            items.append(
                f'<a class="nav-a{" on" if key == active else ""}" href="{href}" title="{label}">'
                f'{icon(ic)}<span class="lbl">{label}</span>{count}</a>')
        if items:
            groups.append(f'<div class="nav-h">{heading}</div>{"".join(items)}')
    tname = html.escape(tenant_name)
    av = initials(tenant_name)
    badge = (f'<span class="badge{"" if status_ok else " warn"}">{html.escape(status_label)}</span>'
             if status_label else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · {html.escape(platform)}</title>{FONTS}
<style>{CSS}</style></head><body>
<div class="app">
<aside class="side">
  <a class="brand" href="/portal/"><span class="logo">{html.escape(platform[:1])}</span><span>{html.escape(platform)}</span></a>
  <nav class="navs">{"".join(groups)}</nav>
  <div class="side-foot"><div class="av">{av}</div>
    <div class="who"><b>{tname}</b><span>{html.escape(location)}</span></div>
    <a class="out" href="/portal/logout" title="Log out">{icon("logout", 18)}</a></div>
</aside>
<div class="main">
  <header class="top">
    <button class="icon-btn" id="side-toggle" type="button" aria-label="Toggle sidebar">{icon("panel")}</button>
    <h1>{tname}</h1>
    <div class="right">{badge}<div class="av" title="{tname}">{av}</div></div>
  </header>
  <main class="content">{body}</main>
</div></div>
<script>{_SHELL_JS}</script></body></html>"""


def auth_shell(title: str, body: str, platform: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · {html.escape(platform)}</title>{FONTS}
<style>{CSS}</style></head><body>
<div class="auth"><div class="card">
<div class="brand"><span class="logo">{html.escape(platform[:1])}</span><span>{html.escape(platform)}</span></div>
{body}</div></div></body></html>"""


# --------------------------------------------------------------------------
# Charts (server-rendered, no JS)
# --------------------------------------------------------------------------
def line_chart(this: list[float], last: list[float], labels: list[str]) -> str:
    """Area line for `this` over a faint line for `last`; same length lists."""
    w, h, pad = 600.0, 220.0, 12.0
    n = max(len(this), 2)
    top = max(this + last + [1])

    def pts(vals: list[float]) -> list[tuple[float, float]]:
        return [(i * w / (n - 1), h - pad - (v / top) * (h - 2 * pad)) for i, v in enumerate(vals)]

    def path(p: list[tuple[float, float]]) -> str:
        return " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(p))

    cur, prev = pts(this), pts(last)
    area = f"{path(cur)} L{w:.1f},{h:.1f} L0,{h:.1f} Z"
    grid = "".join(f'<line x1="0" x2="{w}" y1="{y}" y2="{y}" stroke="#eef0f3" stroke-width="1"/>'
                   for y in (pad, h / 3, 2 * h / 3, h - pad))
    xs = "".join(f"<span>{html.escape(l)}</span>" for l in labels)
    return f"""<svg viewBox="0 0 {w:.0f} {h:.0f}" preserveAspectRatio="none" width="100%" height="230"
 role="img" aria-label="Orders per day, this week versus last week">{grid}
<defs><linearGradient id="ga" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#0fb36b" stop-opacity=".22"/>
<stop offset="1" stop-color="#0fb36b" stop-opacity=".03"/></linearGradient></defs>
<path d="{path(prev)}" fill="none" stroke="#cdd3db" stroke-width="2" stroke-dasharray="5 5" vector-effect="non-scaling-stroke"/>
<path d="{area}" fill="url(#ga)"/>
<path d="{path(cur)}" fill="none" stroke="#14904f" stroke-width="2.2" vector-effect="non-scaling-stroke"/>
</svg><div class="chart-x">{xs}</div>"""


def bar_chart(values: list[float], labels: list[str], fmt=lambda v: f"{v:g}") -> str:
    top = max(values + [1])
    cols = "".join(
        f'<div class="col"><span class="val">{html.escape(fmt(v))}</span>'
        f'<div class="b" style="height:{max(v / top * 85, 0.6):.1f}%"></div></div>'
        for v in values)
    xs = "".join(f"<span>{html.escape(l)}</span>" for l in labels)
    return f'<div class="bars">{cols}</div><div class="bars-x">{xs}</div>'
