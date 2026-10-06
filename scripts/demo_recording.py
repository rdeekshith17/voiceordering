#!/usr/bin/env python3
"""Build a playable voice demo of a VoiceOrderAI phone order.

Runs a scripted caller conversation through the REAL agent stack
(localhost /chat API -> Anthropic agent -> live Square catalog), renders
every agent reply with the production ElevenLabs TTS pipeline, and writes
a self-contained demo page.

Safety: the script never confirms the order, so no real Square order is
created. The cart is abandoned open, exactly like a real caller hanging up.

Usage:
    python scripts/demo_recording.py [--out DIR]

Costs (disclosed): a handful of Anthropic chat turns + ~1.5k ElevenLabs
chars against the free tier. TTS clips are disk-cached, so re-runs are free.
"""
from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from voiceorder.voice import tts as tts_mod

BASE = "http://localhost:8000"

# Scripted caller turns. Kept simple and linear; the final turn asks for the
# total instead of confirming, so no order is ever submitted.
CALLER_SCRIPT = [
    "Hi! I'd like to place an order for pickup, please.",
    "Can I get two chicken biryanis?",
    "Also add an order of chicken wings.",
    "Actually, change one of the chicken biryanis to a lamb biryani.",
    "That's everything. What's my total?",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / "workspace/your_files/voiceorderai-voice-demo"))
    args = ap.parse_args()
    out = Path(args.out)
    audio_dir = out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    # trust_env=False: sandbox proxy env vars break localhost for httpx.
    client = httpx.Client(base_url=BASE, timeout=120.0, trust_env=False)
    try:
        health = client.get("/health").json()
    except Exception as exc:
        print(f"cannot reach {BASE}: {exc}", file=sys.stderr)
        return 1
    print(f"server: {health['restaurant']} ({health['pos_profile']}, {health['menu_items']} items)")

    start = client.post("/chat/start").json()
    session_id = start["session_id"]
    greeting = start["greeting"]

    turns: list[tuple[str, str, str]] = []  # (role, text, audio_file)

    def add_agent_turn(text: str, fname: str) -> None:
        cleaned = " ".join(text.split())
        if len(cleaned) > 380:
            # keep TTS under the per-call cap; cut at a sentence boundary
            cut = cleaned[:380]
            for sep in (". ", "! ", "? "):
                i = cut.rfind(sep)
                if i > 200:
                    cut = cut[: i + 1]
                    break
            cleaned = cut
        mp3 = tts_mod.synthesize(cleaned)
        dest = audio_dir / fname
        dest.write_bytes(mp3)
        turns.append(("agent", text, f"audio/{fname}"))

    print("synthesizing greeting...")
    add_agent_turn(greeting, "turn_00_greeting.mp3")

    for i, caller_text in enumerate(CALLER_SCRIPT, start=1):
        print(f"caller: {caller_text}")
        turns.append(("caller", caller_text, ""))
        resp = client.post("/chat/message", json={"session_id": session_id, "text": caller_text})
        resp.raise_for_status()
        body = resp.json()
        reply = body["reply"]
        print(f"agent: {reply[:120]}...")
        print(f"  (cart_state={body['cart_state']})")
        add_agent_turn(reply, f"turn_{i:02d}_agent.mp3")
        if body.get("transferred") or body.get("cart_state") == "submitted":
            print("WARNING: call transferred or order submitted; stopping script", file=sys.stderr)
            break

    # Combined MP3: naive frame concatenation (fine for playback).
    combined = out / "demo_full.mp3"
    with open(combined, "wb") as f:
        for role, _text, af in turns:
            if role == "agent":
                f.write((out / af).read_bytes())

    total_chars = sum(len(t[1]) for t in turns if t[0] == "agent")
    rows = []
    for role, text, af in turns:
        who = "🤖 VoiceOrderAI" if role == "agent" else "📞 Caller"
        audio = f'<br><audio controls src="{af}"></audio>' if af else ""
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
<p class="note">Recorded {health['restaurant']} ordering agent (real AI, real menu: """
    page += f"""{health['menu_items']} items). Agent replies use the production
ElevenLabs voice pipeline. Play all: <audio controls src="demo_full.mp3"></audio></p>
{''.join(rows)}
<p class="note">Demo only — no order was placed. ~{total_chars} TTS chars.</p>
</body></html>"""
    (out / "demo.html").write_text(page)
    print(f"\nwrote {out/'demo.html'} ({len(turns)} turns, ~{total_chars} TTS chars)")
    print(f"full audio: {combined}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
