"""FraudWatchdog: toll-fraud / abuse monitoring for the phone platform.

Phone lines are a classic toll-fraud target: compromised or malicious
callers can ring up Twilio charges with long calls, repeated dialing, or
off-hours probing. This agent sweeps recent call records per tenant and
opens a tenant ticket (kind='fraud_suspected') when several weak signals
coincide — no single signal ever fires on its own unless it is extreme.

CONSERVATIVE THRESHOLDS (tuned against false positives; a busy dinner
rush must never page):

- lookback window:            24 h (configurable)
- high volume:                >= 100 calls/tenant in the window
- strong volume (single-signal): >= 300 calls in the window
- long no-order calls:         calls lasting >= 600 s with no successful
                               submit_order tool call, >= 5 of them
- strong no-order (single-signal): >= 20 of them
- repeat caller:               same from_number dials >= 10 times
- strong repeat (single-signal): same from_number dials >= 30 times
- night probing:               >= 3 calls started 02:00-05:00 UTC

FIRING RULE: ticket opens when >= 2 distinct signals fire, OR one signal
hits its "strong" level. Severity is always 'warning' — a human reviews
before any number gets blocked or any account is touched.

Privacy: phone numbers are masked (first 2 + last 2 digits kept) in every
ticket and log line. Secrets are never present in call records, but the
support agent's token redaction is reused defensively on ticket detail.

Scheduling is owned by ops (cron). This module only exposes
run_fraud_watch().
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable

from ..tenants.store import Tenant, TenantStore

log = logging.getLogger("voiceorder.agents.fraud")

_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")

# --- thresholds -------------------------------------------------------------
LOOKBACK_HOURS = 24
VOLUME_THRESHOLD = 100
VOLUME_STRONG = 300
LONG_CALL_SECONDS = 600
NO_ORDER_THRESHOLD = 5
NO_ORDER_STRONG = 20
REPEAT_CALLER_THRESHOLD = 10
REPEAT_CALLER_STRONG = 30
NIGHT_THRESHOLD = 3
NIGHT_START_HOUR = 2  # UTC
NIGHT_END_HOUR = 5  # UTC (exclusive)

GetCalls = Callable[[str], "list[dict]"]


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("[redacted]", str(text))


def mask_number(number: str) -> str:
    """Mask a phone number for tickets/logs: keep first 2 + last 2 digits.

    '+15551234567' -> '+15******67'. Unparseable/empty -> '[unknown]'.
    """
    digits = "".join(c for c in (number or "") if c.isdigit())
    if not digits:
        return "[unknown]"
    prefix = "+" if str(number).strip().startswith("+") else ""
    if len(digits) <= 4:
        return prefix + "*" * len(digits)
    return f"{prefix}{digits[:2]}{'*' * (len(digits) - 4)}{digits[-2:]}"


def _duration(row: dict) -> float:
    return max(0.0, (row.get("updated_at") or 0) - (row.get("started_at") or 0))


def _turns(row: dict) -> list[dict]:
    try:
        turns = json.loads(row.get("turns") or "[]")
    except (ValueError, TypeError):
        return []
    return turns if isinstance(turns, list) else []


def _order_succeeded(row: dict) -> bool:
    for t in _turns(row):
        for tool in t.get("tools") or []:
            if tool.get("name") == "submit_order" and tool.get("ok"):
                return True
    return False


def _night_hour(ts: float) -> bool:
    h = time.gmtime(ts).tm_hour
    return NIGHT_START_HOUR <= h < NIGHT_END_HOUR


def analyze_calls(calls: list[dict]) -> dict:
    """Pure signal detection. Returns {signal_name: evidence_str} for fired signals."""
    signals: dict[str, str] = {}
    if not calls:
        return signals

    # 1. raw volume
    n = len(calls)
    if n >= VOLUME_THRESHOLD:
        signals["high_volume"] = f"{n} calls in the window"
    if n >= VOLUME_STRONG:
        signals["high_volume_strong"] = f"{n} calls (extreme volume)"

    # 2. long calls that produced no order
    no_order = [c for c in calls
                if _duration(c) >= LONG_CALL_SECONDS and not _order_succeeded(c)]
    if len(no_order) >= NO_ORDER_THRESHOLD:
        signals["long_no_order_calls"] = (
            f"{len(no_order)} calls >= {LONG_CALL_SECONDS // 60} min with no order")
    if len(no_order) >= NO_ORDER_STRONG:
        signals["long_no_order_strong"] = (
            f"{len(no_order)} long no-order calls (extreme)")

    # 3. repeat callers
    by_caller: dict[str, int] = {}
    for c in calls:
        num = (c.get("from_number") or "").strip()
        if num:
            by_caller[num] = by_caller.get(num, 0) + 1
    top = max(by_caller.values(), default=0)
    if top >= REPEAT_CALLER_THRESHOLD:
        masked = {mask_number(k): v for k, v in by_caller.items()
                  if v >= REPEAT_CALLER_THRESHOLD}
        signals["repeat_caller"] = ", ".join(
            f"{k} x{v}" for k, v in sorted(masked.items(), key=lambda kv: -kv[1]))
    if top >= REPEAT_CALLER_STRONG:
        signals["repeat_caller_strong"] = f"one caller dialed {top}x (extreme)"

    # 4. night probing
    night = [c for c in calls if _night_hour(c.get("started_at") or 0)]
    if len(night) >= NIGHT_THRESHOLD:
        signals["night_probing"] = (
            f"{len(night)} calls started 02:00-05:00 UTC")

    return signals


def _should_fire(signals: dict) -> bool:
    strong = {s for s in signals if s.endswith("_strong")}
    weak = {s for s in signals if not s.endswith("_strong")}
    return bool(strong) or len(weak) >= 2


def run_fraud_watch(
    store: TenantStore,
    now: float | None = None,
    lookback_hours: int = LOOKBACK_HOURS,
) -> list[dict]:
    """Hourly run. Returns newly opened fraud tickets (usually none)."""
    now = now if now is not None else time.time()
    since = now - lookback_hours * 3600
    new_tickets: list[dict] = []

    for tenant in store.list_tenants():
        rows = store.calls_in_window(tenant.id, since)
        signals = analyze_calls(rows)
        if not signals:
            continue
        if not _should_fire(signals):
            log.debug("fraud: tenant %s signals below threshold: %s",
                      tenant.id, sorted(signals))
            continue
        detail = "\n".join(f"- {name}: {_redact(ev)}"
                           for name, ev in sorted(signals.items()))
        t = store.open_ticket(
            tenant.id, "fraud_suspected", "warning",
            f"Possible call abuse ({len(signals)} signals)",
            detail, now=now)
        if t:
            log.warning("fraud: ticket opened for tenant %s: %s",
                        tenant.id, sorted(signals))
            new_tickets.append(t)
    return new_tickets
