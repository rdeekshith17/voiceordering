# VoiceOrderAI

Phase 1 of the VoiceOrderAI build plan: a voice-driven phone ordering engine for
restaurants, built first against **fake** POS adapters. The LLM only talks and
calls tools; this FastAPI server owns the cart, the prices, and the final order.

## Architecture

```
voiceorder/
  core/            # NEVER imports a vendor SDK (enforced by tests/test_no_vendor_imports.py)
    ports.py       # VoiceAdapter / PosAdapter protocols + shared dataclasses
    catalog.py     # Item, Variation, ModifierGroup, Catalog
    cart.py        # Cart, CartLine, the 5-state order state machine
    matching.py    # search_menu: fuzzy match on names + aliases
    tools.py       # the six order tools + dispatch
  pos_adapters/
    fake.py        # FakePos with square_like / clover_like / toast_like profiles
    square.py      # parked until Phase 5 (real Square adapter)
  voice_adapters/
    text.py        # plain-JSON adapter for chat, tests, and the demo
  voice/           # speech: ElevenLabs TTS (Phase 3) -> MP3, disk-cached
    tts.py         # synthesize() via Secure Vault credential, free-tier-safe defaults
  api/
    main.py        # FastAPI: /call/start, /tools/{name}, /call/end, /health
    storage.py     # CartStore / OrderStore ports + in-memory implementations
  fixtures/
    menu_taqueria.json   # reference menu: 25 items, sizes, modifiers, aliases
```

**State machine:** `empty -> building -> read_back -> confirmed -> submitted`.
`submit_order` refuses unless a read-back happened and the caller said yes.
Any cart mutation after a read-back resets the state to `building`, so a
changed order is always read back again.

**POS profiles** (each fake copies one real POS's rules):

| profile      | unpaid orders visible | payment step  | extra rule enforced by the fake |
|--------------|----------------------|---------------|---------------------------------|
| square_like  | no                   | payment link  | order counts as received only once paid |
| clover_like  | yes                  | pay at pickup | modifiers must be linked to the item |
| toast_like   | yes                  | pay at pickup | totals only come from a price quote call |

**Failure modes** on the fake (`mode=`): `ok`, `slow`, `down`, `sold_out`
— used by Phase 4 hardening tests.

## Quickstart

```bash
cd ~/workspace/voiceorder
pip install -e ".[dev]"

# run the test suite (unit + tool tests on all 3 profiles + API flow + lint rule)
pytest -q

# start the server
uvicorn voiceorder.api.main:app --port 8000

# in another terminal, watch a full simulated phone order
python scripts/demo_call.py
```

Environment knobs: `POS_PROFILE` (square_like|clover_like|toast_like),
`RESTAURANT_NAME`, `TRANSFER_NUMBER`, `PICKUP_MINUTES`, `TAX_RATE`,
`VOICE_SECRET` (when set, every endpoint requires an `X-Voice-Secret` header),
`TTS_ENABLED=0` (disables voice synthesis), `TTS_CACHE_DIR` (MP3 cache location).

## Voice (Phase 3)

The agent's replies can be spoken with ElevenLabs instead of just read:

- `POST /voice/speak` `{"text": "..."}` → `audio/mpeg` (MP3)
- the `/chat` demo page plays every agent reply aloud, with a 🔊/🔇 toggle

The server authenticates to ElevenLabs through the connected credential in the
Secure Vault — no key in code or env. A disk cache (`.tts_cache/`, keyed by
text + voice + model) makes repeated greetings free after the first render,
each request is capped at 400 chars, and the free-tier-safe defaults are the
Sarah voice with `eleven_flash_v2_5` (some premade voices and
`eleven_multilingual_v2` need a paid plan for API use).

## Twilio voice (Phase 5a)

Real phone calls via the Gather loop: Twilio answers, plays the ElevenLabs
greeting, and posts each caller utterance to `/twilio/gather`, which runs one
agent turn and plays the reply back.

- `POST /twilio/voice` — incoming call → greeting `<Play>` + `<Gather>`
- `POST /twilio/gather` — `SpeechResult` → agent turn → reply `<Play>` + `<Gather>`
- `POST /twilio/status` — call ended → the cart is closed out
- `GET /voice/audio/{key}.mp3` — cached clip for Twilio `<Play>` (plain GET)

Webhooks authenticate with Twilio's request signature (`X-Twilio-Signature`),
not our voice secret. Set `TWILIO_AUTH_TOKEN` + `PUBLIC_BASE_URL` (the public
https URL Twilio calls, e.g. your ngrok/cloudflared URL) to enforce it — until
both are set the server logs a warning and accepts unsigned webhooks (dev only).
`transfer_call` becomes a real `<Dial>` to `TRANSFER_NUMBER`, and a submitted
order triggers a best-effort SMS receipt when `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, and `TWILIO_PHONE_NUMBER` are set.

Twilio console webhook URLs (voice): `https://<public>/twilio/voice`,
status callback: `https://<public>/twilio/status`. Phone calls also need the
agent brain: `ANTHROPIC_API_KEY` + `ANTHROPIC_MODEL`, same as `/chat`.

## API

- `POST /call/start` `{called_number, caller_id}` → `{call_id, cart_id, greeting, ...}`
- `POST /tools/{search_menu|add_item|update_item|remove_item|get_cart|submit_order|transfer_call}`
  `{call_id, cart_id, arguments}` → `{ok, message, data, error_code}`
- `POST /call/end` `{call_id, cart_id, duration_seconds, transcript}` → call record
- `GET /health`
- `GET /chat` — demo web page to talk to the agent in a browser
- `POST /chat/start`, `POST /chat/message` — chat backend (needs an LLM key)
- `POST /voice/speak` — ElevenLabs TTS: `{"text"}` → MP3 (Phase 3)

## Phase 1 gate

- [x] all tool tests pass against all three fake profiles
- [x] lint rule proves `core/` imports no vendor SDK
- [x] text-mode golden script (the burrito call) runs end-to-end over HTTP

## Phase 2 — text agent + golden evals

- `voiceorder/agent/` — the order-taking agent: `prompt.py` builds the system
  prompt and JSON tool definitions from the catalog; `llm.py` is the LLM client
  abstraction (Anthropic implementation; needs `ANTHROPIC_API_KEY`);
  `loop.py` runs the tool-calling loop against the engine.
- `voiceorder/eval/scripts/` — 32 golden caller scripts (YAML): corrections,
  swaps, quantity changes, unknown items, off-menu requests, prompt injection,
  hang-ups, sold-out, required choices, transfers, size variations.
- `voiceorder/eval/` — harness (`harness.py`), grader (`grader.py`), and the
  runner (`runner.py`). Every run saves a full transcript; the grader requires
  the final cart to equal the expected cart exactly, a clean read-back before
  every submit, and correct transfer behavior.

```bash
# talk to the agent in your terminal (you play the caller)
ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=<model-id> python scripts/chat.py

# run the golden evals: every script x every POS profile x 5 runs
ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=<model-id> \
  python -m voiceorder.eval.runner --runs 5 --out eval/out

# or open the demo chat page in a browser (same keys required)
open http://localhost:8000/chat
```

Phase 2 gate: every script ends with exactly the expected cart, 5 runs each,
on all three profiles.

## Phase 4 — owner app + hardening

- **Owner dashboard** (`GET /owner`): recent orders (auto-refresh), menu manager
  with 86/restore buttons, link to the backup screen.
- **Backup order screen** (`GET /owner/backup`): hand-enter orders when the AI
  is down. Orders run through the same engine, so totals and tax match.
- **Runtime menu availability**: `catalog.set_available()` lets the owner 86 or
  restore items without a restart; the POS rejects 86'd items at fire time.
- **Failure modes**: new `flaky` POS mode (fails once, then recovers) plus tests
  proving retries never duplicate an order (idempotency key), a dead POS parks
  the order for staff instead of losing it, and 86'ing mid-call fails safe.
- **Security pass**: `OWNER_SECRET` on all owner endpoints (header or `?key=`);
  `VOICE_SECRET_PREVIOUS` keeps the old voice secret valid during rotation;
  automated test proves no card-shaped digits ever appear in tool output
  (the engine never takes card numbers — Square-style payment goes by texted link).

```bash
OWNER_SECRET=... uvicorn voiceorder.api.main:app --port 8000
open http://localhost:8000/owner?key=...
```

Phase 4 gate: backup screen fires a real order; failure-mode tests green on all
three profiles; security tests green.

## What's next (per plan)

- **Phase 2:** text agent + caller-LLM harness, 32 golden scripts, prompt/tool defs
  generated from the catalog. Built 2026-09-28; full 5-runs-x-3-profiles eval still
  needs `ANTHROPIC_API_KEY` + `ANTHROPIC_MODEL` (not yet provided).
- **Phase 3:** ElevenLabs voice reality check via ngrok tunnel (throwaway).
- **Phase 4:** owner app, backup screen, failure-mode tests, security pass.
- **Phase 5:** real adapters — needs Square/Clover sandbox creds, Twilio number,
  ElevenLabs/Vapi keys (webhook auth is already stubbed via `VOICE_SECRET`).
- **Phase 6:** pilot. **Phase 7:** scale.
