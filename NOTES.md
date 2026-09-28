# Pending tasks

Status snapshot against the VoiceOrderAI build plan's roadmap (section 5/6). Each item is tagged with who unblocks it next.

## Production hardening (beyond the plan's phases, started at your request)

- [x] **CI**: GitHub Actions (`.github/workflows/ci.yml`) runs the full free test tier on every push/PR, Python 3.11 + 3.12, plus a real `postgres:16` service container so the Postgres-backed tests run for real in CI, not just skip. Verified green: https://github.com/rdeekshith17/voiceordering/actions
- [x] **Real persistence**: `_sessions`/in-memory-only `BackupScreen` replaced with a swappable-store architecture, same fakes-first pattern as the POS/voice adapters:
  - `voiceorder/storage/cart_store.py` — `CartStore` port. `InMemoryCartStore` is the default (what every test uses); `RedisCartStore` is real, selected by setting `VOICEORDER_REDIS_URL`. The live cart now round-trips through `Cart.to_dict()`/`from_dict()` on every request instead of living in a Python dict in the API process.
  - `voiceorder/storage/postgres_backup.py` — `PostgresBackupStore`, selected by setting `VOICEORDER_DATABASE_URL`. Verified against a real throwaway local Postgres during development (not just written blind), and now covered continuously by CI's postgres service container.
  - `core/backup.py` gained a `"confirmed"` entry kind — every submitted order is now logged, not just the unpaid/failed/transferred ones.
  - Neither is required for local dev or `pytest` — both env vars are unset by default, so everything still runs against the in-memory versions with zero setup.
  - `docker-compose.yml` added for running real Postgres + Redis locally (needs Docker Desktop running — it's installed on this machine but wasn't started).
- [ ] Containerize the app itself (Dockerfile) + a proper settings module replacing scattered `os.environ.get` calls — not started yet.

## Plan phases

## Phase 0 — Groundwork

- [x] Square adapter scaffold, normalized schema, tests, CI
- [ ] **30-minute Square check** — `scripts/square_sandbox_check.py` is built and tested (fails cleanly without credentials). **Blocked on you**: a free Square Developer sandbox token + location ID, then ~10 minutes to run it and eyeball the sandbox dashboard.
- [ ] **Toast partner application** — a form only you can submit (needs your business details). Say the word if you want help drafting the answers.
- [ ] **Talk to 3–5 restaurant owners** — this is the plan's own "first gate." Tracker + call script built: [Office Hours](https://claude.ai/artifact/X76EkSt5SNtGgACKSBanP7). **Blocked on you**: the actual conversations.

## Phase 1 — Order engine on fakes: **done, gate closed**

Three fake POS profiles (`square_like`/`clover_like`/`toast_like`) including `down`/`slow`/`sold_out` failure modes, and an enforced test that `core/` imports nothing but the standard library. (Test count folded into Phase 4's total below — same suite, kept growing.)

## Phase 2 — Text agent and golden callers: **scaffolding done, gate not yet verified**

- [x] Terminal chat interface (`voiceorder/cli.py`)
- [x] Agent system prompt + tool schemas generated from the catalog (`voiceorder/agent/`)
- [x] Two-LLM golden-caller harness (`tests/golden/llm_harness.py`)
- [x] 8 seed golden scripts across corrections, swaps, quantity changes, off-menu requests, prompt injection, hang-ups (`tests/golden/callers.py`) — written blind, not yet validated against a real model
- [ ] **Run the golden-caller suite for real.** Blocked on you: an Anthropic API key from console.anthropic.com (separate billing from Claude Pro — Pro doesn't include API access).
- [ ] Expand to 30+ scripts, per the plan — do this *after* seeing real transcripts, not before (blind scripts are guesses, not evals).

## Phase 3 — Voice reality check: not started

Needs an ElevenLabs (or Vapi) account, a Twilio number, and an ngrok tunnel to your local API. Also gated on Phase 2's suite actually passing first.

## Phase 4 — Owner app and hardening: **mostly done, started early**

Built out of plan order — Phases 2/3's remaining gates need things only you can unblock (an API key, then voice/phone accounts), while this was genuinely code-only. 105 tests passing (test count keeps growing — see the persistence section above too).

- [x] Restaurant settings on `RestaurantConfig`: hours, pickup lead time, per-restaurant order limits, transfer number, voice platform choice (`voiceorder/api/main.py`)
- [x] Backup screen: `core/backup.py` + `GET /backup-screen/{restaurant_id}` — logs unpaid (Square link orders), failed (POS down/timeout after retry), and transferred calls
- [x] Failure handling from section 8: `search_menu` tracks misses and flags `suggest_transfer` at 2; `submit_order` retries once via a generic `PosError` and falls back to the backup screen with a reassuring message instead of raising; `transfer_call` is a real 7th tool now, wired into the prompt and schemas
- [x] Large-order and prompt-injection guarantees (already existed from Phase 1) reconfirmed with explicit tests
- [ ] **Tool webhook timeout + one retry** (section 8, row 5) — deliberately deferred: this needs a real HTTP layer between the voice platform and the API, which doesn't exist until Phase 5's webhooks are wired up
- [ ] **Per-caller rate limiting** — deferred: needs `caller_number` plumbed through from a real voice adapter (Phase 5); building it against nothing meaningful now would be untested cruft
- [ ] **Backup-screen endpoint has no auth** — fine while it's just us, but needs real owner-app auth before anyone else sees it (Phase 7 territory)
- [ ] Caller data retention policy and the recording-consent legal wording (section 9) — these are policy/legal decisions, not code

## Phase 5 — Real adapters and webhooks: not started

Needs production Square/Clover/Toast credentials, an ElevenLabs/Vapi account, and Twilio.

## Phase 6 — Pilot at one restaurant: not started

Needs a named pilot restaurant from Phase 0.

## Phase 7 — Scale: not started

## Open questions from the plan (section 10), still open

- Who is the pilot restaurant, and which POS does it use?
- Does a paid payment-link order with modifiers + pickup fulfillment actually display correctly on Square? (this is what `square_sandbox_check.py` answers)
- Will a Square pilot owner accept pay-by-link, or will they want to pay at the counter?
- Which voice platform is the pilot default — decide from the Phase 5 accuracy/latency/cost comparison, not by feel.
- Which LLM runs inside the voice platform — decide from Phase 2's golden scripts.
- Toast partner application: submitted, and what's the timeline?
- How is the restaurant's number forwarded (all calls / overflow / after-hours)? Depends on their phone carrier.
- What accuracy target counts as "pilot ready"? Agree this with the owner before Phase 6.

## Credentials/access you'll need, gathered in one place

| What | Unblocks | Where |
|---|---|---|
| Anthropic API key | Phase 2 golden-caller suite, live chat | console.anthropic.com (separate from Claude Pro) |
| Square sandbox token + location ID | Phase 0 Square check | Square Developer Dashboard (free) |
| ElevenLabs or Vapi account | Phase 3 | their respective sites |
| Twilio account + number | Phase 3, Phase 5 | twilio.com |
| Toast partner application decision | Toast support at all | Toast's partner program |
