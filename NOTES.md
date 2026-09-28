# Pending tasks

Status snapshot against the VoiceOrderAI build plan's roadmap (section 5/6). Each item is tagged with who unblocks it next.

## Phase 0 — Groundwork

- [x] Square adapter scaffold, normalized schema, tests, CI
- [ ] **30-minute Square check** — `scripts/square_sandbox_check.py` is built and tested (fails cleanly without credentials). **Blocked on you**: a free Square Developer sandbox token + location ID, then ~10 minutes to run it and eyeball the sandbox dashboard.
- [ ] **Toast partner application** — a form only you can submit (needs your business details). Say the word if you want help drafting the answers.
- [ ] **Talk to 3–5 restaurant owners** — this is the plan's own "first gate." Tracker + call script built: [Office Hours](https://claude.ai/artifact/X76EkSt5SNtGgACKSBanP7). **Blocked on you**: the actual conversations.

## Phase 1 — Order engine on fakes: **done, gate closed**

75 tests passing (unit + the fixed burrito-call golden script), three fake POS profiles (`square_like`/`clover_like`/`toast_like`) including `down`/`slow`/`sold_out` failure modes, and an enforced test that `core/` imports nothing but the standard library.

## Phase 2 — Text agent and golden callers: **scaffolding done, gate not yet verified**

- [x] Terminal chat interface (`voiceorder/cli.py`)
- [x] Agent system prompt + tool schemas generated from the catalog (`voiceorder/agent/`)
- [x] Two-LLM golden-caller harness (`tests/golden/llm_harness.py`)
- [x] 8 seed golden scripts across corrections, swaps, quantity changes, off-menu requests, prompt injection, hang-ups (`tests/golden/callers.py`) — written blind, not yet validated against a real model
- [ ] **Run the golden-caller suite for real.** Blocked on you: an Anthropic API key from console.anthropic.com (separate billing from Claude Pro — Pro doesn't include API access).
- [ ] Expand to 30+ scripts, per the plan — do this *after* seeing real transcripts, not before (blind scripts are guesses, not evals).

## Phase 3 — Voice reality check: not started

Needs an ElevenLabs (or Vapi) account, a Twilio number, and an ngrok tunnel to your local API. Also gated on Phase 2's suite actually passing first.

## Phase 4 — Owner app and hardening: not started

Restaurant settings, a backup/order-list screen, failure handling exercised via the `slow`/`down`/`sold_out` fake POS modes already built, a transfer stub, and the security pass (section 9). Mostly code-only — doesn't strictly need external accounts, but the plan's own gating puts it after Phase 3.

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
