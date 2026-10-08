# Next phase — PR 0 baseline audit

**Date:** 2026-10-08 · **Commit audited:** `86ecb24` (main, after PR #2) · **Scope:** read-only.
Nothing in production was changed. Every statement below was checked in source, in
the production Neon database (read-only queries), or against the live service.

## 1. Verified architecture

| Area | Where it lives | Notes |
|---|---|---|
| App entry, Twilio webhooks | `voiceorder/api/main.py` | `/twilio/voice`, `/twilio/gather`, `/twilio/turn`, `/twilio/status`; web chat `/chat/*`; tool API `/tools/{name}` |
| Agent + prompt | `voiceorder/agent/loop.py`, `prompt.py` | Anthropic tool-use loop; prompt built once per call (menu + caller section) |
| Order tools | `voiceorder/core/tools.py` | **Seven** tools: `search_menu`, `add_item`, `update_item`, `remove_item`, `get_cart`, `submit_order`, `transfer_call` (plan says six) |
| Cart state machine | `voiceorder/core/cart.py` | `empty → building → read_back → submitted`; read-back required before submit; idempotency key per cart |
| POS adapters | `voiceorder/pos_adapters/` | Square (live), Toast, Clover (untested live), Fake (demo profiles); all take a per-tenant `timezone` |
| DB layer | `voiceorder/db.py` | One wrapper for SQLite or Postgres (`DATABASE_URL`); upsert helper; reconnect on dropped connection |
| Tenancy store | `voiceorder/tenants/store.py` | tenants, settings, encrypted POS secrets, portal users, transcripts, tickets, drafts, usage, customers |
| Orders/carts store | `voiceorder/api/storage.py` | `orders` (JSON payload + tenant_id), `carts` |
| Tenant portal | `voiceorder/portal/portal.py`, `ui.py` | Dashboard, Statistics, Orders, Customers, Live calls, POS, Marketing, Usage, Support, System settings |
| Legacy owner dashboard | `voiceorder/api/owner.py` | Single shared `OWNER_SECRET`; default tenant only |
| Background agents | `voiceorder/agents/*.py`, runners `ops/run_*.py` | 7 agents: billing, fraud, marketing, menusync, onboarding, qa, support |
| Deploy | `deploy/Dockerfile`, `render.yaml` | Render free plan, Docker; `deploy/seed.py` then uvicorn (1 process) |

### Schema (production Neon, 11 tables)
`tenants`, `tenant_settings` (key/value), `tenant_secrets` (Fernet-encrypted JSON),
`tenant_users`, `call_transcripts` (turns as JSON), `orders`, `carts`, `customers`,
`tickets`, `marketing_drafts`, `usage_daily`.

- **No migration system.** Schema is `CREATE TABLE IF NOT EXISTS` at startup plus
  hand-written `ALTER` checks (`orders.tenant_id`). Fine for additive tables; not
  enough for backfills or constraint changes.
- **Tenant = restaurant = one phone number.** There is no location or organization
  entity. `tenants.phone_number` is unique (normalized digits).

### Auth
- Portal: email + password (PBKDF2), HMAC-signed session cookie derived from
  `TENANT_MASTER_KEY`. Every portal query is scoped by the session's tenant.
- **No roles.** Every portal user of a tenant has full owner rights.
- **No platform/admin identity.** `/owner` uses one shared secret and only sees the
  default tenant.
- Tool API (`/tools`, `/call/*`, `/voice/speak`): `X-Voice-Secret`.
- Twilio webhooks: signature validation **enforced in production** (unsigned probe
  to `/twilio/status` returned 403).

### Call state
- Conversation state (`AgentSession`, incl. message history) and in-flight turns live
  in **process memory** (`_twilio_sessions`, `_twilio_turns`). A Render restart or
  deploy mid-call loses the call; the caller hears "I lost track of your call".
- Transcripts are persisted per turn in `call_transcripts`; status callbacks arrive
  (the production call is marked `completed`), so the Twilio number's status
  callback is configured in the Twilio console, not in code.
- Turns run on a background thread; `/twilio/turn` is polled with `<Pause>` +
  `<Redirect>` (the plan's "no blocking handlers" rule is already met).

### Transfer
`transfer_call` returns a bare `<Dial>number</Dial>`: no `timeout`, no `action`
callback, no busy/no-answer fallback.

## 2. Test baseline

```
.venv/bin/python -m pytest -q
1 failed, 279 passed, 4 skipped   (284 collected)
```
- **Failing:** `test_twilio_gather_runs_agent_turn` asserts `<Say>One moment.</Say>`,
  but the filler phrases rotate and play cached audio when a clip exists in
  `.tts_cache/`. Environment-dependent; left as-is (not silently "fixed").
- **Skipped:** the 4 Postgres storage tests (run with `TEST_DATABASE_URL`).
- The summary's "270 tests" figure is out of date.

## 3. Production state (Neon, read-only)

| Item | Finding |
|---|---|
| Tenants | 1 — Hyderabad House, `5622680097`, timezone `America/Chicago` |
| Portal users | 1 |
| POS secret | `square`, written at tenant creation → **copied from Render env vars**, not entered in the portal |
| Orders / calls / customers | 1 / 1 / 1 (the pilot test call) |
| tickets / drafts / usage_daily | **0 / 0 / 0** |
| `transfer_number` | **`+15550134200` — the code's placeholder default (a 555 number)** |
| Live health | `ok`, Square profile, 74 menu items synced |

## 4. Plan vs. reality — discrepancies

1. **"Agents read sandbox SQLite."** More precisely: the agents are cron jobs on the
   old sandbox host (`~/workspace/voiceorder`, see `PRODUCTION.md`). Nothing runs them
   on Render, so **production has no agents at all**. Code-wise they already read
   `main.tenant_store`, which uses `DATABASE_URL` when set. Importing `main` also runs
   app startup side effects (default-tenant seed, Square menu sync).
2. **"Neon re-seed overwrites tenant config."** Fixed in `c74115e`: env values now only
   fill settings that were never set; POS secrets are copied from env only if missing.
3. **"Square account identity mismatch."** Likely cause: the production secret came
   from Render's `SQUARE_ACCESS_TOKEN` + `SQUARE_LOCATION_ID` (the latter defaults to
   `LP080BBB3A045` in `render.yaml`). Needs a "Test connection" on the POS page to
   confirm the location belongs to the token's account. Not verifiable without the
   production key.
4. **Six tools** → seven (`transfer_call`).
5. **Per-branch IANA timezone** (plan §5.2) already exists per tenant
   (`tenant_settings.timezone`, used by pickup times and the portal).
6. **Returning-caller memory** now also confirms saved name + number (PR #2).

## 5. Gaps per planned feature

| Feature | Missing foundations |
|---|---|
| A. HITL kitchen approvals | Persistent call/conversation state; cart revision numbers; roles (kitchen staff); approval tables + state machine; a hold loop in `/twilio/turn`; transfer with fallback |
| B. Multilingual | STT language is Twilio Gather default (`en-US`); Hindi/Telugu support of Gather STT **unverified**; no per-language TTS mapping or aliases |
| C. Super Admin | Platform identity/permission; audit log table; location entity |
| D. AI ON/OFF + schedule | Routing evaluator at `/twilio/voice`; schedule/override tables; a **real** staff transfer number; voicemail/closed-message TwiML |
| Cross-cutting | Migration mechanism; feature-flag table; production agent scheduler |

## 6. Risks

- **Mid-call restarts** drop the conversation (in-memory state). Render's free plan
  also sleeps after 15 idle minutes; the first call after sleep can exceed Twilio's
  webhook timeout.
- **Placeholder transfer number** in production: "talk to a person" dials a 555 number.
- **Returning-caller greeting is synthesized per call** (personalized text can't be
  pre-cached): extra ElevenLabs latency on the first webhook; to be measured in PR 2.
- **Secrets in conversation history:** the Neon password and API keys were pasted in
  chat and should be rotated.
- Seven agents' outputs (usage, tickets, drafts) are empty in production, so the
  portal's Usage/Support/Marketing pages show nothing there.

## 7. Gate 0 status

| Check | Status |
|---|---|
| Live ordering works | **Yes** — production order `QTVBZY` submitted to Square from a real call (10-07) |
| One Square order per confirmed submit | Idempotency key per cart in all adapters; one order for the one call. Not load-tested |
| Production tenant config recoverable | **Partly** — config is in Neon and survives deploys; POS token lives only encrypted under Render's `TENANT_MASTER_KEY` (keep it) |

## 8. Decisions needed before PR 1

1. The real staff number for transfers / AI-OFF fallback at Hyderabad House.
2. Where agents should run in production: Render Cron Jobs (paid), a scheduler
   inside the web process (free, but the free plan sleeps), or an external cron
   hitting a protected endpoint.
3. Confirm build order: PR 1 (schema/migrations + roles + flags + audit) → PR 2
   (AI ON/OFF + schedule) → PR 3 (HITL), as the plan recommends.
