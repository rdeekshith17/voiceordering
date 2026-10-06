"""Real Toast adapter: the order engine talks to Toast's REST API.

Stdlib only (urllib) -- no Toast SDK, and core/ stays vendor-free by
construction. The app owns the conversational menu (local catalog); Toast is
the order backend: orders are created as unpaid takeout orders so staff see
them immediately, and the customer pays at pickup -- mirroring the
toast_like fake profile.

Toast specifics:
  - Auth: OAuth2 client credentials. POST
    /authentication/v1/authentication/login with
    {"clientId", "clientSecret", "userAccessType": "TOAST_MACHINE_CLIENT"}
    returns {"token": {"accessToken": ...}}. The adapter logs in lazily,
    caches the token, and re-logs-in once on a 401.
  - Every call carries ``Toast-Restaurant-External-ID: <restaurant guid>``.
  - Hosts: https://ws-api.toasttab.com (production),
            https://ws-sandbox-api.toasttab.com (sandbox).
  - Menu: GET /menus/v2/menus -> Menu -> MenuGroup -> MenuItem ->
    ModifierGroup -> ModifierOption. sync_catalog() indexes items (and their
    modifier options) by name so submit() can resolve cart lines to Toast
    GUIDs. A cart line with no Toast match raises PosError telling the owner
    to align the menus -- Toast orders cannot carry fully ad-hoc items.
  - Orders: POST /orders/v2/orders with a takeout diningOption guid
    (TOAST_TAKEOUT_DINING_GUID), one check, selections with resolved GUIDs,
    customer name/phone, and no payments. Unmapped modifiers are folded into
    specialInstructions rather than dropped silently.
  - The toast_like fake requires a fresh price quote before submit; this
    adapter keeps that rule (quote() records the cart, submit() enforces it)
    so the engine's behavior is identical across real and fake.
  - externalId carries our idempotency key for traceability; primary
    idempotency is the adapter's local cache (same as the other adapters).

Credentials arrive at runtime, never in code:
  TOAST_CLIENT_ID / TOAST_CLIENT_SECRET   OAuth client credentials
  TOAST_RESTAURANT_GUID                   restaurant location GUID
  TOAST_TAKEOUT_DINING_GUID               takeout dining-option GUID (optional
                                          but recommended; without it the
                                          order is created with no dining
                                          option and staff route it manually)
  TOAST_ENVIRONMENT                       "sandbox" (default) or "production"

Set POS_PROFILE=toast to use this adapter.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from .. import net
from ..core.cart import Cart
from ..core.catalog import Catalog
from ..core.ports import (
    PaymentStep,
    PosCapabilities,
    PosError,
    PosOrder,
    RestaurantContext,
    Totals,
)

_HOSTS = {
    "sandbox": "https://ws-sandbox-api.toasttab.com",
    "production": "https://ws-api.toasttab.com",
}


class ToastPosAdapter:
    """Toast Orders + Menus APIs over plain HTTPS."""

    capabilities = PosCapabilities(
        unpaid_orders_visible=True, payment_links=False, pay_at_pickup=True
    )

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        restaurant_guid: str,
        *,
        environment: str = "sandbox",
        takeout_dining_guid: str = "",
        catalog: Catalog,
        tax_rate: float = 0.0825,
        pickup_minutes: int = 20,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError(
                "ToastPosAdapter needs TOAST_CLIENT_ID and TOAST_CLIENT_SECRET"
            )
        if not restaurant_guid:
            raise ValueError("ToastPosAdapter needs a TOAST_RESTAURANT_GUID")
        if environment not in _HOSTS:
            raise ValueError(f"unknown Toast environment: {environment}")
        self._client_id = client_id
        self._client_secret = client_secret
        self._restaurant_guid = restaurant_guid
        self._base = _HOSTS[environment]
        self._takeout_dining_guid = takeout_dining_guid
        self._catalog = catalog
        self._tax_rate = tax_rate
        self._pickup_minutes = pickup_minutes
        self._token: str | None = None
        self._orders: dict[str, PosOrder] = {}  # idempotency_key -> order
        self._quoted: set[str] = set()  # cart ids with a fresh price quote
        self._item_guids: dict[str, str] = {}  # item name.lower() -> Toast guid
        self._modifier_guids: dict[str, dict[str, str]] = {}  # item name -> {mod name -> guid}

    # -- PosAdapter ---------------------------------------------------------
    def ping(self) -> dict:
        """Connectivity check: OAuth login (validates client credentials)."""
        self._ensure_token()
        return {"ok": True}

    def sync_catalog(self, restaurant: RestaurantContext) -> Catalog:
        """Pull the Toast menu and index items/modifiers by name.

        The conversational menu stays local (refs, aliases, agent wording);
        this index is what lets submit() resolve cart lines to Toast GUIDs.
        """
        menus = self._request("GET", "/menus/v2/menus")
        items, modifiers = {}, {}
        for menu in menus if isinstance(menus, list) else menus.get("menus", []):
            for group in menu.get("menuGroups", []) or []:
                for item in group.get("menuItems", []) or []:
                    name = (item.get("name") or "").strip().lower()
                    guid = item.get("guid")
                    if name and guid:
                        items[name] = guid
                    mod_map: dict[str, str] = {}
                    for mgroup in item.get("modifierGroups", []) or []:
                        for opt in mgroup.get("modifierOptions", []) or []:
                            mname = (opt.get("name") or "").strip().lower()
                            mguid = opt.get("guid")
                            if mname and mguid:
                                mod_map[mname] = mguid
                    if name:
                        modifiers[name] = mod_map
        self._item_guids = items
        self._modifier_guids = modifiers
        return self._catalog

    def is_available(self, item_ref: str) -> bool:
        return self._catalog.is_available(item_ref)

    def quote(self, cart: Cart) -> Totals:
        subtotal = round(cart.subtotal, 2)
        tax = round(subtotal * self._tax_rate, 2)
        self._quoted.add(cart.cart_id)
        return Totals(
            subtotal=subtotal,
            tax=tax,
            total=round(subtotal + tax, 2),
            currency=self._catalog.currency,
        )

    def submit(self, cart: Cart, idempotency_key: str) -> PosOrder:
        if idempotency_key in self._orders:
            return self._orders[idempotency_key]
        if cart.cart_id not in self._quoted:
            raise PosError("toast requires a price quote before an order can post")
        for line in cart.lines:
            if not self.is_available(line.item_ref):
                raise PosError(
                    f"item {line.item_ref} is sold out; the order cannot fire"
                )
        selections = [self._selection(line) for line in cart.lines]
        toast_order = self._create_order(cart, idempotency_key, selections)
        guid = str(toast_order.get("guid") or "")
        if not guid:
            raise PosError("Toast returned no order guid")
        pickup = (datetime.now() + timedelta(minutes=self._pickup_minutes)).strftime(
            "%-I:%M %p"
        )
        order = PosOrder(
            order_id=guid,
            order_number=guid.replace("-", "")[-6:].upper(),
            status="received",  # visible to staff, unpaid
            pickup_time=pickup,
            totals=self.quote(cart),
        )
        self._orders[idempotency_key] = order
        return order

    def payment_step(self, order: PosOrder) -> PaymentStep:
        return PaymentStep(
            kind="pay_at_pickup",
            instructions="You can pay when you pick up.",
        )

    # -- order construction ---------------------------------------------------
    def _selection(self, line) -> dict:
        key = line.item_name.strip().lower()
        guid = self._item_guids.get(key)
        if not guid:
            raise PosError(
                f"'{line.item_name}' is not mapped to a Toast menu item "
                "(run sync_catalog after aligning the Toast menu names)"
            )
        mod_map = self._modifier_guids.get(key, {})
        modifiers = []
        unmapped = []
        for mid, mname in zip(line.modifier_ids, line.modifier_names):
            mguid = mod_map.get((mname or "").strip().lower())
            if mguid:
                modifiers.append(
                    {
                        "entityType": "ModifierSelection",
                        "item": {"guid": mguid},
                        "quantity": 1,
                    }
                )
            else:
                unmapped.append(mname)
        instructions = []
        if unmapped:
            instructions.append("with " + ", ".join(unmapped))
        if line.variation_name:
            instructions.append(line.variation_name)
        if line.note:
            instructions.append(line.note)
        return {
            "entityType": "MenuItemSelection",
            "item": {"guid": guid},
            "quantity": line.quantity,
            "modifiers": modifiers,
            "specialInstructions": "; ".join(instructions) or None,
        }

    def _create_order(
        self, cart: Cart, idempotency_key: str, selections: list[dict]
    ) -> dict:
        promised = (datetime.now() + timedelta(minutes=self._pickup_minutes)).isoformat()
        first, _, last = (cart.customer_name or "").partition(" ")
        check: dict = {
            "entityType": "Check",
            "selections": selections,
            "customer": {
                "firstName": first or "VoiceOrder",
                "lastName": last or "Pickup",
                "phone": cart.customer_phone or "",
            },
            "payments": [],
        }
        body: dict = {
            "entityType": "Order",
            "externalId": idempotency_key,
            "promisedDate": promised,
            "checks": [check],
        }
        if self._takeout_dining_guid:
            body["diningOption"] = {"guid": self._takeout_dining_guid}
        return self._request("POST", "/orders/v2/orders", body)

    # -- Toast REST -------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._ensure_token()}",
            "Toast-Restaurant-External-ID": self._restaurant_guid,
            "Content-Type": "application/json",
        }

    def _ensure_token(self) -> str:
        if not self._token:
            self._token = self._login()
        return self._token

    def _login(self) -> str:
        body = {
            "clientId": self._client_id,
            "clientSecret": self._client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._base + "/authentication/v1/authentication/login",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with net.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise PosError(f"Toast login failed: {self._toast_detail(exc)}") from exc
        except OSError as exc:
            raise PosError(f"Toast unreachable: {exc}") from exc
        token = (payload.get("token") or {}).get("accessToken")
        if not token:
            raise PosError("Toast login returned no access token")
        return str(token)

    def _request(
        self, method: str, path: str, body: dict | None = None
    ) -> dict | list:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self._base + path, data=data, headers=self._headers(), method=method
        )
        try:
            with net.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and self._token:
                # Token expired mid-session: log in once more and retry once.
                self._token = None
                req = urllib.request.Request(
                    self._base + path, data=data, headers=self._headers(), method=method
                )
                try:
                    with net.urlopen(req, timeout=30) as resp:
                        return json.loads(resp.read() or b"{}")
                except urllib.error.HTTPError as retry_exc:
                    raise PosError(
                        f"Toast rejected the request: {self._toast_detail(retry_exc)}"
                    ) from retry_exc
            raise PosError(f"Toast rejected the request: {self._toast_detail(exc)}") from exc
        except OSError as exc:
            raise PosError(f"Toast unreachable: {exc}") from exc

    @staticmethod
    def _toast_detail(exc: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read()[:2000] or b"{}")
            message = payload.get("message") or payload.get("detail")
            if message:
                return f"HTTP {exc.code} {message}"
            errors = payload.get("errors")
            if errors:
                return f"HTTP {exc.code} {errors[0] if isinstance(errors, list) else errors}"
        except (ValueError, OSError):
            pass
        return f"HTTP {exc.code}"
