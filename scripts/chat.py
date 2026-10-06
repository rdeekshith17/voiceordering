"""Terminal chat with the order agent (Phase 2).

You play the caller; the agent takes your order through the real tool loop.

    ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=<model-id> \\
        python scripts/chat.py [--pos clover_like]

Type 'hangup' to end the call.
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voiceorder.agent.llm import AnthropicClient, MissingCredentials
from voiceorder.agent.loop import AgentSession
from voiceorder.core.cart import Cart
from voiceorder.core.catalog import Catalog
from voiceorder.eval.harness import make_context
from voiceorder.pos_adapters.fake import FakePos
from voiceorder.voice_adapters.text import TextAdapter


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pos", default=os.environ.get("POS_PROFILE", "square_like"))
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", ""))
    args = parser.parse_args()
    if not args.model:
        print("Set ANTHROPIC_MODEL to an Anthropic model id (or pass --model).")
        return 2
    try:
        llm = AnthropicClient()
    except MissingCredentials as exc:
        print(f"Cannot start chat: {exc}")
        return 2

    base = Path(__file__).resolve().parent.parent
    catalog = Catalog.from_json(base / "voiceorder" / "fixtures" / "menu_taqueria.json")
    ctx = make_context(args.pos)
    pos = FakePos(profile=args.pos, catalog=catalog)
    cart = Cart(cart_id=uuid.uuid4().hex[:12], restaurant_id=ctx.restaurant_id,
                idempotency_key=uuid.uuid4().hex)
    session = AgentSession(llm, catalog, pos, ctx, cart, args.model)
    greeting = TextAdapter().call_start_response(ctx)["greeting"]

    print(f"\n--- calling {ctx.restaurant_name} (POS: {args.pos}) ---")
    print(f"AGENT: {greeting}")
    try:
        while True:
            text = input("YOU: ").strip()
            if not text or text.lower() in ("hangup", "quit", "bye"):
                print("AGENT: Thanks for calling! Goodbye.")
                break
            turn = session.handle_caller_message(text)
            print(f"AGENT: {turn['text']}")
            if cart.transferred or cart.state.value == "submitted":
                break
    except (EOFError, KeyboardInterrupt):
        print("\n[call ended]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
