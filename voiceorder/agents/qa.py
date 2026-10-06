"""CallQaAgent: deterministic review of recent call transcripts.

Scans the last N hours of calls per tenant with fixed heuristics — no LLM,
no network, no credentials. Flags:

- failed_order (warning): a submit_order tool call that returned ok=False.
- confusion_loop (warning): >= CONFUSION_REPLIES replies matching
  "didn't understand"-style patterns in one call (the AI is stuck).
- customer_frustration (info): the caller used frustration phrases.
- staff_transfer (info): the call was transferred to a human.

One open ticket per tenant (kind='call_quality'): the worst severity wins,
detail lists up to MAX_FLAGS calls with a short evidence quote each.
open_ticket dedupes so a bad day never spams; when a clean 24 h follows,
the ticket auto-resolves, so the next bad day flags fresh.

Optional LLM hook: pass llm_assess(transcript) -> dict to add richer
judgement (e.g. with the Anthropic client once a key is wired). Heuristic
flags always stand on their own; an llm_assess failure is logged and the
heuristic result is kept.

Scheduling is owned by ops (cron). This module only exposes run_qa_review().
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..tenants.store import Tenant, TenantStore

log = logging.getLogger("voiceorder.agents.qa")

LOOKBACK_HOURS = 24
CONFUSION_REPLIES = 3
MAX_FLAGS = 5

LlmAssess = Callable[[dict], dict | None]
GetTranscript = Callable[[Tenant, str], "dict | None"]

CONFUSION_PATTERNS = (
    "didn't catch that",
    "did not catch that",
    "didn't understand",
    "did not understand",
    "not sure what you mean",
    "could you repeat that",
    "say that again",
    "sorry, what was",
    "let me try again",
    "i'm having trouble",
)

FRUSTRATION_PATTERNS = (
    "this is ridiculous",
    "you're useless",
    "you are useless",
    "i want a human",
    "talk to a human",
    "you keep getting it wrong",
    "forget it",
    "cancel everything",
    "talk to a manager",
    "get me a manager",
    "this is terrible",
    "worst service",
    "so stupid",
)

ORDER_TOOL = "submit_order"
TRANSFER_TOOL = "transfer_call"


def _hits(text: str, patterns: tuple[str, ...]) -> str | None:
    low = (text or "").lower()
    for p in patterns:
        if p in low:
            return p
    return None


def _evidence(text: str, limit: int = 140) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def review_call(transcript: dict,
                llm_assess: LlmAssess | None = None) -> list[dict]:
    """Pure per-call review. Returns [{severity, label, evidence}]."""
    turns = transcript.get("turns") or []
    flags: list[dict] = []
    confusion = 0

    for t in turns:
        reply = str(t.get("reply") or "")
        heard = str(t.get("heard") or "")
        tools = t.get("tools") or []

        hit = _hits(reply, CONFUSION_PATTERNS)
        if hit:
            confusion += 1

        fh = _hits(heard, FRUSTRATION_PATTERNS)
        if fh and not any(f["label"] == "customer_frustration" for f in flags):
            flags.append({"severity": "info", "label": "customer_frustration",
                          "evidence": f"caller said {fh!r}: {_evidence(heard)}"})

        for tool in tools:
            name = tool.get("name")
            if name == ORDER_TOOL and not tool.get("ok"):
                flags.append({"severity": "warning", "label": "failed_order",
                              "evidence": f"submit_order failed: {_evidence(reply)}"})
            elif name == TRANSFER_TOOL and not any(
                    f["label"] == "staff_transfer" for f in flags):
                flags.append({"severity": "info", "label": "staff_transfer",
                              "evidence": f"transferred to staff: {_evidence(reply)}"})

    if confusion >= CONFUSION_REPLIES:
        flags.append({"severity": "warning", "label": "confusion_loop",
                      "evidence": f"{confusion} 'didn't understand'-style replies"})

    if llm_assess is not None:
        try:
            extra = llm_assess(transcript) or {}
            for f in extra.get("flags", []) or []:
                if isinstance(f, dict) and f.get("label"):
                    flags.append({
                        "severity": f.get("severity", "info"),
                        "label": f["label"],
                        "evidence": _evidence(str(f.get("evidence", ""))),
                    })
        except Exception as exc:
            log.warning("qa: llm_assess failed, keeping heuristics: %s", exc)

    return flags


def _default_get_transcript(store: TenantStore, tenant: Tenant,
                            since: float) -> "list[dict]":
    out = []
    for row in store.calls_in_window(tenant.id, since):
        t = store.get_transcript(tenant.id, row["call_sid"])
        if t:
            out.append(t)
    return out


def run_qa_review(
    store: TenantStore,
    get_transcript: GetTranscript | None = None,
    now: float | None = None,
    lookback_hours: int = LOOKBACK_HOURS,
    llm_assess: LlmAssess | None = None,
) -> list[dict]:
    """Daily run. Returns newly opened call-quality tickets (usually none)."""
    now = now if now is not None else time.time()
    since = now - lookback_hours * 3600
    new_tickets: list[dict] = []

    fetch: Callable[[Tenant], list[dict]]
    if get_transcript is None:
        fetch = lambda tenant: _default_get_transcript(store, tenant, since)  # noqa: E731
    else:
        fetch = lambda tenant: [  # noqa: E731
            t for t in (get_transcript(tenant, row["call_sid"])
                        for row in store.calls_in_window(tenant.id, since))
            if t
        ]

    for tenant in store.list_tenants():
        flagged: list[tuple[str, dict]] = []
        for transcript in fetch(tenant):
            flags = review_call(transcript, llm_assess=llm_assess)
            if flags:
                flagged.append((transcript["call_sid"], flags))

        if not flagged:
            store.resolve_ticket(tenant.id, "call_quality", now=now)
            continue

        n_warn = sum(1 for _, fs in flagged for f in fs if f["severity"] == "warning")
        severity = "warning" if n_warn else "info"
        detail_lines = []
        for sid, fs in flagged[:MAX_FLAGS]:
            for f in fs:
                detail_lines.append(f"- {sid}: [{f['label']}] {f['evidence']}")
        t = store.open_ticket(
            tenant.id, "call_quality", severity,
            f"{len(flagged)} call{'s' if len(flagged) != 1 else ''} need review"
            + (f" ({n_warn} with problems)" if n_warn else ""),
            "\n".join(detail_lines), now=now)
        if t:
            log.warning("qa: call_quality ticket for tenant %s (%d calls)",
                        tenant.id, len(flagged))
            new_tickets.append(t)
    return new_tickets
