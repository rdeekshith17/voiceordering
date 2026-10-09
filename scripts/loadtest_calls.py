#!/usr/bin/env python3
"""Concurrent phone-call load test against the real app (no Twilio, no AI spend).

Starts the app in-process on a local port, replaces the AI with a fake that
waits like a real model (default 1.5 s per reply) and walks each simulated
call through the real Twilio webhooks: /twilio/voice -> /twilio/gather ->
/twilio/turn polls -> /twilio/status. Reports webhook response times
(Twilio gives up after ~15 s) and checks every call finished cleanly.

    DATABASE_URL=postgresql://user@localhost/loadtest python scripts/loadtest_calls.py --calls 40

Use a throwaway database: it creates calls, carts and sessions there.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import statistics
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=int, default=30, help="simultaneous calls")
    ap.add_argument("--turns", type=int, default=3, help="caller turns per call")
    ap.add_argument("--ai-seconds", type=float, default=1.5, help="fake AI latency per reply")
    ap.add_argument("--port", type=int, default=8077)
    args = ap.parse_args()
    if not os.environ.get("DATABASE_URL") and not os.environ.get("VOICEORDER_DB"):
        print("set DATABASE_URL (or VOICEORDER_DB) to a throwaway database")
        return 2
    os.environ.update({"PUBLIC_BASE_URL": "https://loadtest.invalid", "ANTHROPIC_MODEL": "fake",
                       "POS_PROFILE": "square_like", "SQUARE_MENU_SYNC": "0", "TTS_ENABLED": "0",
                       "AGENTS_ENABLED": "0"})
    os.environ.pop("TWILIO_AUTH_TOKEN", None)

    import httpx
    import uvicorn

    from voiceorder.agent.llm import LLMResponse
    from voiceorder.api import main as app_main

    class FakeAI:
        """Stateless: search on a fresh caller line, add after a search, else talk."""

        def complete(self, *, system, messages, tools, model):
            time.sleep(args.ai_seconds)
            last = messages[-1]["content"]
            if isinstance(last, str):
                return LLMResponse(tool_calls=[{"id": "s", "name": "search_menu",
                                                "arguments": {"query": "chicken taco"}}])
            if last and last[0].get("tool_call_id") == "s":
                return LLMResponse(tool_calls=[{"id": "a", "name": "add_item",
                                                "arguments": {"item_ref": "T1", "quantity": 1}}])
            return LLMResponse(text="Got it. Anything else?")

    fake = FakeAI()
    app_main._chat_llm_client = lambda: fake
    app_main._speak_to_url = lambda *a, **k: None
    server = uvicorn.Server(uvicorn.Config(app_main.app, port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{args.port}"
    for _ in range(100):
        try:
            httpx.get(base + "/health")
            break
        except Exception:
            time.sleep(0.1)

    timings: dict[str, list[float]] = {"voice": [], "gather": [], "turn": [], "status": []}
    failures: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(args.calls)

    def timed(client, kind, url, data):
        t0 = time.perf_counter()
        r = client.post(url, data=data)
        with lock:
            timings[kind].append(time.perf_counter() - t0)
        if r.status_code != 200:
            raise RuntimeError(f"{kind} HTTP {r.status_code}")
        return r.text

    def one_call(i: int) -> None:
        sid = f"CAload{i:04d}{int(time.time())}"
        form = {"CallSid": sid, "From": f"+1415555{i:04d}", "To": "+15550000000"}
        try:
            with httpx.Client(base_url=base, timeout=30) as c:
                start.wait()
                if "<Gather" not in timed(c, "voice", "/twilio/voice", form):
                    raise RuntimeError("no greeting")
                for n in range(args.turns):
                    xml = timed(c, "gather", "/twilio/gather", {**form, "SpeechResult": f"turn {n}"})
                    for _ in range(60):
                        m = re.search(r'<Redirect method="POST">([^<]+)</Redirect>', xml)
                        if not m:
                            break
                        time.sleep(1.0)  # Twilio's <Pause length="1"/>
                        xml = timed(c, "turn", html.unescape(m.group(1)).replace(
                            "https://loadtest.invalid", ""), form)
                    if "<Gather" not in xml:
                        raise RuntimeError(f"turn {n} didn't come back to listening")
                timed(c, "status", "/twilio/status", {**form, "CallStatus": "completed"})
        except Exception as exc:
            with lock:
                failures.append(f"{sid}: {exc}")

    t0 = time.perf_counter()
    threads = [threading.Thread(target=one_call, args=(i,)) for i in range(args.calls)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    wall = time.perf_counter() - t0
    server.should_exit = True

    print(f"{args.calls} simultaneous calls x {args.turns} turns, fake AI {args.ai_seconds}s/reply, "
          f"{wall:.1f}s wall time, {len(failures)} failed")
    for kind, values in timings.items():
        if values:
            v = sorted(values)
            p95 = v[min(len(v) - 1, int(len(v) * 0.95))]
            print(f"  {kind:7} n={len(v):4}  p50={statistics.median(v) * 1000:6.0f} ms  "
                  f"p95={p95 * 1000:6.0f} ms  max={v[-1] * 1000:6.0f} ms")
    for f in failures[:10]:
        print("  FAIL", f)
    slow = [x for xs in timings.values() for x in xs if x > 10]
    print("RESULT:", "OK" if not failures and not slow else "PROBLEMS",
          f"(webhooks over 10 s: {len(slow)})")
    return 0 if not failures and not slow else 1


if __name__ == "__main__":
    sys.exit(main())
