"""Owner app (Phase 4): dashboard, menu manager, backup order screen.

Auth: a signed-in restaurant Admin of this restaurant (the portal session), or
an X-Owner-Secret header with OWNER_SECRET for scripts. The secret is never
accepted in the URL (it leaked into history, logs and shared links). Until
OWNER_SECRET is set, local traffic is allowed with a warning -- the same
convention as VOICE_SECRET.
"""
from __future__ import annotations

import hmac
import html as _html
import logging
from typing import Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from ..core import tools
from ..core.catalog import Catalog
from ..core.ports import RestaurantContext
from ..pos_adapters.fake import FakePos
from .storage import InMemoryCartStore, InMemoryOrderStore

log = logging.getLogger("voiceorder")


@dataclass
class OwnerDeps:
    catalog: Catalog
    pos: FakePos
    cart_store: InMemoryCartStore
    order_store: InMemoryOrderStore
    ctx: RestaurantContext
    owner_secret: str = ""
    # True when the request carries a portal session of this restaurant's admin.
    session_ok: Callable[[Request], bool] | None = None


class AvailabilityRequest(BaseModel):
    item_ref: str
    available: bool


class BackupLine(BaseModel):
    item_ref: str
    quantity: int = 1
    variation_id: str | None = None
    modifier_ids: list[str] = []
    note: str | None = None


class BackupOrderRequest(BaseModel):
    lines: list[BackupLine]
    customer_name: str
    customer_phone: str


def build_owner_router(deps: OwnerDeps) -> APIRouter:
    router = APIRouter()
    if not deps.owner_secret:
        log.warning("OWNER_SECRET is not set; owner endpoints accept unauthenticated local calls")

    def allowed(request: Request) -> bool:
        if deps.session_ok is not None and deps.session_ok(request):
            return True
        if not deps.owner_secret:
            return deps.session_ok is None  # dev mode only when no portal is wired
        header = request.headers.get("x-owner-secret", "")
        return bool(header) and hmac.compare_digest(header, deps.owner_secret)

    def require_owner(request: Request) -> None:
        if not allowed(request):
            raise HTTPException(status_code=401, detail="log in to the portal as the restaurant admin")

    def page(html: str, request: Request):
        if not allowed(request):
            return RedirectResponse("/portal/login", status_code=302)
        return HTMLResponse(html.replace("__RESTAURANT__", _html.escape(deps.ctx.restaurant_name)))

    @router.get("/owner", response_class=HTMLResponse)
    def owner_dashboard(request: Request):
        return page(_DASHBOARD_HTML, request)

    @router.get("/owner/backup", response_class=HTMLResponse)
    def backup_screen(request: Request):
        return page(_BACKUP_HTML, request)

    @router.get("/owner/orders")
    def owner_orders(limit: int = 50, _auth: None = Depends(require_owner)) -> dict:
        return {
            "orders": [
                _present_order(o)
                for o in deps.order_store.list_recent(deps.ctx.restaurant_id, limit=limit)
            ]
        }

    @router.get("/owner/menu")
    def owner_menu(_auth: None = Depends(require_owner)) -> dict:
        return {
            "items": [
                {
                    "ref": item.ref,
                    "name": item.name,
                    "price": item.base_price,
                    "category": item.category,
                    "available": deps.catalog.is_available(item.ref),
                }
                for item in deps.catalog.all_items()
            ]
        }

    @router.post("/owner/menu/availability")
    def set_availability(
        req: AvailabilityRequest, _auth: None = Depends(require_owner)
    ) -> dict:
        if not deps.catalog.set_available(req.item_ref, req.available):
            raise HTTPException(status_code=404, detail=f"unknown item {req.item_ref}")
        action = "restored" if req.available else "86'd"
        log.info("owner %s %s", action, req.item_ref)
        return {"item_ref": req.item_ref, "available": req.available}

    @router.post("/owner/backup/submit")
    def backup_submit(
        req: BackupOrderRequest, _auth: None = Depends(require_owner)
    ) -> dict:
        """Hand-entered order for when the AI is down: same engine, same totals."""
        if not req.lines:
            raise HTTPException(status_code=400, detail="no items on the ticket")
        cart = deps.cart_store.create(deps.ctx.restaurant_id)
        for line in req.lines:
            result = tools.add_item(
                cart,
                catalog=deps.catalog,
                pos=deps.pos,
                ctx=deps.ctx,
                item_ref=line.item_ref,
                quantity=line.quantity,
                variation_id=line.variation_id,
                modifier_ids=line.modifier_ids,
                note=line.note,
            )
            if not result.ok:
                raise HTTPException(
                    status_code=400,
                    detail={"error": result.error_code, "message": result.message},
                )
        tools.get_cart(cart, deps.pos)
        result = tools.submit_order(
            cart,
            deps.pos,
            deps.ctx,
            customer_name=req.customer_name,
            customer_phone=req.customer_phone,
            confirmed=True,
            idempotency_key=cart.idempotency_key,
        )
        if not result.ok:
            raise HTTPException(
                status_code=502,
                detail={"error": result.error_code, "message": result.message},
            )
        order = result.data["order"]
        deps.order_store.save(
            {
                "order_id": order.get("order_id"),
                "restaurant_id": deps.ctx.restaurant_id,
                "call_id": "owner-backup",
                "cart_id": cart.cart_id,
                "order_number": order.get("order_number"),
                "status": order.get("status"),
                "pickup_time": order.get("pickup_time"),
                "totals": order.get("totals"),
                "payment": order.get("payment"),
                "customer_name": cart.customer_name,
                "customer_phone": cart.customer_phone,
                "lines": [line.to_dict() for line in cart.lines],
            }
        )
        log.info("owner backup order %s submitted", order.get("order_number"))
        return {"ok": True, "order": order}

    return router


def _present_order(order: dict) -> dict:
    saved_at = order.get("saved_at")
    return {
        "order_number": order.get("order_number"),
        "status": order.get("status"),
        "pickup_time": order.get("pickup_time"),
        "total": (order.get("totals") or {}).get("total"),
        "customer_name": order.get("customer_name"),
        "customer_phone": order.get("customer_phone"),
        "lines": [
            f"{line.get('quantity')}x {line.get('item_name')}"
            for line in order.get("lines", [])
        ],
        "via": order.get("call_id"),
        "saved_at": (
            datetime.fromtimestamp(saved_at).strftime("%-I:%M %p") if saved_at else ""
        ),
    }


_DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>__RESTAURANT__ owner</title>
<style>body{font-family:system-ui;max-width:900px;margin:2em auto;padding:0 1em}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:.4em;text-align:left}
.out{background:#fee}.btn{padding:.3em .8em;cursor:pointer}</style></head><body>
<h2>__RESTAURANT__ &mdash; owner dashboard</h2>
<p><a href="#" id="backupLink">Backup order screen</a> (take orders by hand if the AI is down)</p>
<h3>Recent orders</h3><div id="orders">loading&hellip;</div>
<h3>Menu</h3><div id="menu">loading&hellip;</div>
<script>
document.getElementById('backupLink').href='/owner/backup';
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
async function get(p){const r=await fetch(p);if(!r.ok)throw new Error(await r.text());return r.json();}
async function post(p,body){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error(await r.text());return r.json();}
async function loadOrders(){
  const j=await get('/owner/orders');
  if(!j.orders.length){document.getElementById('orders').innerHTML='<i>no orders yet</i>';return;}
  document.getElementById('orders').innerHTML='<table><tr><th>#</th><th>Time</th><th>Customer</th><th>Items</th><th>Total</th><th>Status</th><th>Via</th></tr>'+
    j.orders.map(o=>'<tr><td>'+esc(o.order_number)+'</td><td>'+esc(o.saved_at)+'</td><td>'+esc(o.customer_name||'')+'</td><td>'+esc(o.lines.join(', '))+'</td><td>$'+esc(o.total)+'</td><td>'+esc(o.status)+'</td><td>'+esc(o.via)+'</td></tr>').join('')+'</table>';
}
async function loadMenu(){
  const j=await get('/owner/menu');
  document.getElementById('menu').innerHTML='<table><tr><th>Item</th><th>Price</th><th>Status</th><th></th></tr>'+
    j.items.map(i=>'<tr class="'+(i.available?'':'out')+'"><td>'+esc(i.name)+'</td><td>$'+i.price.toFixed(2)+'</td><td>'+(i.available?'available':"86'd")+'</td>'+
    '<td><button class="btn" data-ref="'+esc(i.ref)+'" data-avail="'+(!i.available)+'">'+(i.available?'86':'restore')+'</button></td></tr>').join('')+'</table>';
  document.querySelectorAll('#menu [data-ref]').forEach(b=>b.onclick=()=>toggleAvail(b.dataset.ref,b.dataset.avail==='true'));
}
async function toggleAvail(ref,avail){await post('/owner/menu/availability',{item_ref:ref,available:avail});loadMenu();}
loadOrders();loadMenu();setInterval(loadOrders,10000);
</script></body></html>"""

_BACKUP_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>__RESTAURANT__ backup orders</title>
<style>body{font-family:system-ui;max-width:900px;margin:2em auto;padding:0 1em}
#items button{margin:.2em;padding:.5em .8em;cursor:pointer}#ticket{border:1px solid #ccc;border-radius:8px;padding:1em;margin-top:1em}
#result{margin-top:1em;font-weight:bold}</style></head><body>
<h2>__RESTAURANT__ &mdash; backup order screen</h2>
<p>For when the AI is down. Orders go through the same engine, so totals and tax match.</p>
<div id="items">loading&hellip;</div>
<div id="ticket"><h3>Ticket</h3><div id="lines"><i>nothing yet</i></div>
<p>Name <input id="cname" placeholder="customer name"> Phone <input id="cphone" placeholder="555-123-4567"></p>
<button onclick="submitOrder()" style="padding:.6em 2em">Fire order</button></div>
<div id="result"></div>
<script>
let ticket={};
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':String(s);return d.innerHTML;}
async function get(p){const r=await fetch(p);if(!r.ok)throw new Error(await r.text());return r.json();}
async function init(){
  const j=await get('/owner/menu');
  const byCat={};
  j.items.forEach(i=>{(byCat[i.category]=byCat[i.category]||[]).push(i);});
  document.getElementById('items').innerHTML=Object.keys(byCat).map(c=>
    '<h4>'+esc(c)+'</h4>'+byCat[c].map(i=>'<button '+(i.available?'':'disabled')+
      ' data-ref="'+esc(i.ref)+'" data-name="'+esc(i.name)+'">'+esc(i.name)+' $'+i.price.toFixed(2)+(i.available?'':" (86'd)")+'</button>').join('')
  ).join('');
  document.querySelectorAll('#items [data-ref]').forEach(b=>b.onclick=()=>add(b.dataset.ref,b.dataset.name));
}
function add(ref,name){ticket[ref]=ticket[ref]||{name:name,qty:0};ticket[ref].qty++;render();}
function render(){
  const refs=Object.keys(ticket);
  document.getElementById('lines').innerHTML=refs.length?refs.map(r=>ticket[r].qty+'x '+esc(ticket[r].name)).join('<br>'):'<i>nothing yet</i>';
}
async function submitOrder(){
  const lines=Object.keys(ticket).map(r=>({item_ref:r,quantity:ticket[r].qty}));
  const body={lines:lines,customer_name:document.getElementById('cname').value,customer_phone:document.getElementById('cphone').value};
  const el=document.getElementById('result');el.textContent='sending…';
  try{
    const r=await fetch('/owner/backup/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(!r.ok)throw new Error((j.detail&&j.detail.message)||JSON.stringify(j.detail));
    el.textContent='Order #'+j.order.order_number+' fired! Total $'+j.order.totals.total+'. '+j.order.payment.instructions;
    ticket={};render();
  }catch(e){el.textContent='Error: '+e.message;}
}
init();
</script></body></html>"""
