#!/usr/bin/env python3
"""Transcript-only pass of the voice demo (TTS blocked in this context).

Runs the scripted caller through the real agent stack and writes demo.html
with the full transcript plus the cached greeting audio. Voice clips for the
remaining turns can be rendered later in a normal turn (ElevenLabs TTS).
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

import httpx

BASE = "http://localhost:8000"
CALLER_SCRIPT = [
    "Hi! I'd like to place an order for pickup, please.",
    "Can I get two chicken biryanis?",
    "Also add an order of chicken wings.",
    "Actually, change one of the chicken biryanis to a lamb biryani.",
    "That's everything. What's my total?",
]


def main() -> int:
    out = Path.home() / "workspace/your_files/voiceorderai-voice-demo"
    audio_dir = out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    client = httpx.Client(base_url=BASE, timeout=120.0, trust_env=False)
    health = client.get("/health").json()
    print(f"server: {health['restaurant']} ({health['pos_profile']}, {health['menu_items']} items)")

    start = client.post("/chat/start").json()
    session_id = start["session_id"]
    greeting = start["greeting"]

    turns: list[tuple[str, str]] = [("agent", greeting)]
    for caller_text in CALLER_SCRIPT:
        print(f"caller: {caller_text}")
        resp = client.post("/chat/message", json={"session_id": session_id, "text": caller_text})
        resp.raise_for_status()
        body = resp.json()
        reply = body["reply"]
        print(f"agent: {reply[:100]}... (cart_state={body['cart_state']})")
        turns.insert(len(turns), ("caller", caller_text))
        turns.append(("agent", reply))
        if body.get("transferred") or body.get("cart_state") == "submitted":
            print("WARNING: transferred/submitted; stopping", file=sys.stderr)
            break

    rows = []
    for i, (role, text) in enumerate(turns):
        who = "🤖 VoiceOrderAI" if role == "agent" else "📞 Caller"
        audio = ""
        if i == 0:
            audio = '<br><audio controls src="audio/turn_00_greeting.mp3"></audio>'
        rows.append(
            f'<div class="turn {role}"><div class="who">{who}</div>'
            f"<p>{html.escape(text)}</p>{audio}</div>"
        )
    page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VoiceOrderAI — Voice Demo</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:720px;margin:2em auto;padding:0 1em;color:#222}}
.turn{{border-radius:12px;padding:0.8em 1em;margin:0.8em 0}}
.turn.agent{{background:#f0f4ff}} .turn.caller{{background:#f6f6f6;text-align:right}}
.who{{font-size:0.8em;color:#666;margin-bottom:0.3em}} p{{margin:0.2em 0}}
audio{{width:100%;margin-top:0.4em}} .note{{font-size:0.85em;color:#666}}
</style></head><body>
<h1>🎙️ VoiceOrderAI — live voice demo</h1>
<p class="note">Scripted call through the real ordering agent ({html.escape(health['restaurant'])},
real AI, real menu: {health['menu_items']} items). The greeting below uses the production
ElevenLabs voice; the remaining turns are the verbatim agent replies — full voice clips
render on request.</p>
{''.join(rows)}
<p class="note">Demo only — no order was placed.</p>
</body></html>"""
    (out / "demo.html").write_text(page)
    print(f"\nwrote {out/'demo.html'} ({len(turns)} turns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
