"""BillingAgent: daily usage metering per tenant, for future SaaS invoicing.

Rolls one UTC calendar day into the `usage_daily` table (upsert, so reruns
are safe). Every active tenant gets a row — tenants with zero activity get
an explicit zero row so reports are complete.

FIELD MAPPING (all derived from existing tables; nothing new is instrumented):

- calls:         COUNT(*) of call_transcripts rows for the tenant whose
                 started_at falls inside the UTC day.
- talk_minutes:  SUM(MAX(0, updated_at - started_at)) / 60. Wall-clock call
                 duration is the closest existing proxy for talk time; it
                 includes ringing and gather pauses, so it slightly
                 overstates pure conversation time.
- tts_chars:     SUM(LEN(reply)) over every turn of every call that day.
                 Every assistant reply is spoken aloud via ElevenLabs TTS on
                 the phone path, so reply-text length is the best available
                 proxy for synthesized characters. (Also counts webchat and
                 greeting replies; a dedicated TTS log would sharpen this.)
- sms_sent:      0. There is no persistent SMS send log — the Twilio adapter's
                 send_sms() is fire-and-forget. Column reserved for future
                 wiring to an sms_log table.

Day boundary: UTC calendar day. Default day is yesterday (the last complete
day); pass an explicit 'YYYY-MM-DD' for backfills. Scheduling is owned by
ops (cron); this module only exposes rollup_day().
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from ..tenants.store import TenantStore

log = logging.getLogger("voiceorder.agents.billing")


def _day_bounds(day: str) -> tuple[float, float]:
    """UTC [start, end) epoch bounds for a 'YYYY-MM-DD' date. Raises ValueError."""
    dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = dt.timestamp()
    return start, start + 86400


def rollup_day(store: TenantStore, day: str | None = None,
               now: float | None = None) -> list[dict]:
    """Roll one UTC day into usage_daily. Returns the rows written."""
    now = now if now is not None else time.time()
    if day is None:
        day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    start_ts, end_ts = _day_bounds(day)
    written: list[dict] = []

    for tenant in store.list_tenants():
        rows = store.calls_in_window(tenant.id, start_ts, end_ts)
        calls = len(rows)
        talk_minutes = 0.0
        tts_chars = 0
        for r in rows:
            dur = max(0.0, (r["updated_at"] or 0) - (r["started_at"] or 0))
            talk_minutes += dur / 60.0
            try:
                turns = json.loads(r["turns"] or "[]")
            except (ValueError, TypeError):
                turns = []
            for t in turns:
                tts_chars += len(str(t.get("reply", "")))
        talk_minutes = round(talk_minutes, 2)
        store.upsert_usage(tenant.id, day, calls, talk_minutes, tts_chars,
                           sms_sent=0, now=now)
        written.append({"tenant_id": tenant.id, "date": day, "calls": calls,
                        "talk_minutes": talk_minutes, "tts_chars": tts_chars,
                        "sms_sent": 0})
    log.info("billing: rolled up %s for %d tenant(s)", day, len(written))
    return written
