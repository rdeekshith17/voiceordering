# VoiceOrderAI: Security, Guardrails & Production-Readiness Audit

**Repo:** github.com/rdeekshith17/voiceordering (`main`, 41 commits) · **Date:** June 2026 · **Type:** read-only. Nothing in the repo was changed.
**How it was checked:** full code and config read-through, git-history secret scan, the test suite run (358 passed, 1 failed, 7 skipped), and a dependency vulnerability scan (`pip-audit`). The live app was not touched.

---

## 1. Summary

| Severity | Count | Main themes |
|---|---|---|
| Critical | 2 | Phone-call security checks switch off quietly when a setting is missing. Render free plan sleeps and drops calls. |
| High | 7 | Public demo and signup are on by default. Other secrets also fail open. No Postgres backups. Toast/Clover can double-post orders. No AI-call limits or timeouts. Encryption key can be lost. |
| Medium | 12 | SMS opt-out, logins can't be revoked, login lockout, unbounded notes, PII kept forever, health check, unpinned dependencies, missing rate limits, and more |
| Low | 9 | Replay, CSP, the `/health` info leak, hard-coded IDs, the failing test, and more |

**What's already solid:**
- **No real secrets in git history.** Only test fixtures such as `EAAA-SECRET-TOKEN` appear.
- **Prices come only from the menu catalog.** The AI has no price or discount field (`core/tools.py:33-79`, `agent/prompt.py:139-168`).
- **Quantities are capped in code** (`tools.py:138,220`).
- **Read-back is enforced by the cart's state machine** (`tools.py:357-423`, `cart.py:99-106`).
- **The AI can't choose who a call is transferred to.** Transfers only go to the restaurant's configured number.
- **SMS receipts only go to the verified caller ID.**
- **Caller name and phone are cleaned before they enter the prompt** (`prompt.py:40-88`).
- **Card numbers can't be captured.** No tool accepts them.
- **Restaurants are fully separated.** Every portal query is scoped to the tenant in the signed session. No IDOR was found.
- **No XSS found.** User data is escaped everywhere it's shown, including transcripts.
- **All SQL is parameterised.**
- **Passwords use PBKDF2 with 200k rounds**, and comparisons are constant-time.
- **Session cookies are signed** and HttpOnly, with SameSite and Secure set.
- **The Docker image runs as a non-root user.**
- **Failed POS orders are saved.** They become `pos_failed` orders plus a critical ticket (`api/main.py:1599-1619`).
- **Every app dependency is clean in `pip-audit`.**

---

## 2. Findings

### CRITICAL

**C1. Twilio webhook signature check fails open.** `voiceorder/api/main.py:800-808`
- **Cause:** if `TWILIO_AUTH_TOKEN` *or* `PUBLIC_BASE_URL` is missing, `_twilio_signature_ok()` logs a warning and returns `True`. `render.yaml` marks both as `sync: false`, which means you fill them in by hand. `PRODUCTION.md:49` even says the token was "skipped for now".
- **Risk:** anyone can POST fake calls to `/twilio/voice`, `/gather` and `/turn`. They can then place real Square orders, burn Claude and ElevenLabs credits, and fill the database.
- **Fix:** add `APP_ENV=production`. In production, refuse to start (or return 403) when either value is missing. Log signature failures as critical tickets.

**C2. Render free plan sleeps after 15 minutes idle.** `render.yaml:10`
- **Risk:** the first call after a quiet spell hits a cold start of roughly 30-60 seconds. Twilio times out, so the caller hears "application error" or nothing at all. In-memory call sessions (`_twilio_sessions`, `main.py:781`) are also lost on every restart or redeploy, which drops calls in progress.
- **Fix:** move to a paid always-on instance (Starter or above) before taking real calls. Set a Twilio *fallback URL* (TwiML Bin: "please call back / forward to the restaurant"). Deploy outside service hours.

### HIGH

**H1. The public `/chat` demo is ON by default.** `main.py:605-608`, `CHAT_DEMO_ENABLED` defaults to `"1"`
- **Status:** `render.yaml` sets it to 0, but any other deploy route (Docker, compose, a fresh Render service) exposes it.
- **Risk:** the demo places real POS orders and spends AI credits. Its only limit is 30 messages per 10 minutes per IP, held in memory. That's easy to get around by switching IPs.
- **Fix:** make it default to off. Require a login or a demo-only tenant with the Fake POS.

**H2. Public portal signup is ON by default.** `main.py:537`, `PORTAL_SIGNUP_ENABLED` defaults to `"1"`
- **Status:** `render.yaml` doesn't set it.
- **Risk:** anyone can create tenants. There's no email check, no CAPTCHA and no approval step, so you get spam tenants, database growth and a path to probing tenant-scoped features.
- **Fix:** default to `0`, and set `PORTAL_SIGNUP_ENABLED=0` in `render.yaml`. Add admin approval or email verification if self-signup is wanted.

**H3. Other secrets also fail open.**
- `require_secret` (`main.py:487-490`) lets everyone through when `VOICE_SECRET` is empty. That covers `/call/start`, `/tools/{name}`, `/call/end`, `/orders/recent` (recent orders with customer PII) and `/voice/speak` (free ElevenLabs).
- The `/owner` router (`api/owner.py:69-71`) allows unauthenticated access when `OWNER_SECRET` is empty and no portal session check is wired.
- **Status:** Render's `generateValue` covers this, but compose and Docker deploys don't.
- **Fix:** same production fail-fast as C1.

**H4. No backups for the production database.** `ops/backup_db.sh` only backs up SQLite.
- **Risk:** on Render the data lives in Postgres (Neon). Nothing in the repo backs that database up. Separately, if `DATABASE_URL` is missing the app only *warns* (`main.py:246-248`) and runs on a SQLite file that's wiped on every deploy.
- **Fix:** turn on Neon point-in-time restore or a daily `pg_dump` to object storage. Refuse to start in production without `DATABASE_URL`.

**H5. Toast and Clover can submit the same order twice.** `pos_adapters/toast.py:108,161-163`, `clover.py:14-16,84,112-114`
- **Cause:** the idempotency map lives in memory only, and neither API has a native idempotency key.
- **Risk:** a retry after a crash or restart creates a second kitchen ticket. Square is safe because it passes `idempotency_key` to Square.
- **Fix:** store `(tenant, cart_idempotency_key) → pos_order_id` in the database before and after submit. Check it first.

**H6. No limits on AI calls or call length.** `agent/llm.py:105-113`, `agent/loop.py:18`
- There's no request timeout or retry policy on the Anthropic call.
- There's no maximum number of turns per call, no maximum call length, no daily or per-tenant AI spend cap, and no limit on concurrent calls (`_twilio_sessions` is an unbounded dict).
- `MAX_TOOL_ITERATIONS=8` per turn is the only limit.
- **Risk:** a caller who never hangs up (or a looping bot) costs money for as long as they stay on. A hung API call stalls the turn: the poll loop gives up after about 30 polls, but the thread keeps running.
- **Fix:**
  - Set `timeout≈8s` and `max_retries=1` on the client.
  - Cap at about 40 turns or 15 minutes per call, then transfer or end politely.
  - Add a per-tenant daily token budget and a cap on concurrent sessions.

**H7. The encryption key for POS secrets can be lost.** `tenants/crypto.py:16-31`
- **Cause:** without `TENANT_MASTER_KEY`, a key is generated into `ops/.tenant_key` on local disk. Render's disk is wiped on deploy, which leaves every stored Square, Toast or Clover token undecryptable. There's no key rotation or versioning.
- **Status:** `render.yaml` generates the key, which is good, but nothing warns you if it changes.
- **Fix:**
  - Require `TENANT_MASTER_KEY` in production.
  - Store a key fingerprint in the database and refuse to start if it doesn't match.
  - Support `TENANT_MASTER_KEY_PREVIOUS` (MultiFernet) for rotation.
  - Keep an offline copy of the key.

### MEDIUM

**M1. SMS has no consent or opt-out handling.** `voice_adapters/twilio.py:146-177`, `main.py:904-925`
- **Status:** receipts are sent unconditionally. There's no STOP/HELP handling, no opt-out list and no "Reply STOP to opt out" text. US A2P 10DLC registration is required for business SMS on local numbers, and unregistered traffic gets filtered or blocked.
- **Fix:**
  - Register a 10DLC brand and campaign (transactional receipts).
  - Keep an opt-out table and check it before sending.
  - Handle inbound STOP (or rely on Twilio Advanced Opt-Out).
  - Say on the call that a text receipt is coming.

**M2. No AI disclosure.** Greeting at `voice_adapters/text.py:30-33` and `main.py:850`
- **Status:** callers hear "This call may be recorded" but are never told they're talking to an AI assistant. California's B.O.T. Act and FTC guidance favour disclosure, and similar state rules keep spreading.
- **Fix:** add "I'm the restaurant's automated assistant" to the greeting. Recording disclosure already exists, which is good for two-party-consent states such as CA, IL and WA, because transcripts and voicemail are stored.

**M3. Logins can't be revoked server-side.** `tenants/crypto.py:64-90`, `portal.py:1673-1677`
- **Status:** cookies are self-contained. Logout, admin password resets and role changes (`store.py:357-371,510-520`) don't invalidate them, so a stolen portal cookie stays valid for 7 days.
- **Fix:** add a `session_version` per user, put it in the cookie, and bump it on logout, reset or role change.

**M4. Login lockout is weak.** `portal.py:1546-1548,1605-1621`, `admin.py:38-39,487-497`
- **Status:** lockout counts per email only, in memory, with no per-IP limit. That means no protection against credential stuffing across many emails, and counters reset on restart. There's also no self-service password reset (admin-only), which is secure but a support burden.
- **Fix:** add a per-IP limit, and keep the counters in the database. Consider 2FA for `/admin`.

**M5. Free-text `note` has no length limit or cleaning.** `cart.py:37`, `tools.py`, going to Square (`square.py:244`), Toast (`toast.py:224-231`) and Clover (`clover.py:203`)
- **Risk:** callers can push long or abusive text to kitchen printers and POS screens.
- **Fix:** cap at about 140 characters, strip control characters, and optionally filter profanity.

**M6. `dispatch` crashes on bad AI arguments.** `tools.py:502-511`
- **Cause:** a bare `int(args.get(...))` raises when the model sends `"two"` or `null`.
- **Fix:** validate the schema and return a `ToolResult` error so the model can retry.

**M7. Personal data is kept forever, and caller speech goes into logs.**
- Full phone numbers, transcripts and voicemail URLs are stored with no retention or purge job (`tenants/store.py:116-160,390-427`).
- `main.py:1336-1345` logs raw caller speech. The phone-masking filter in `logsafe.py` doesn't cover names or addresses.
- **Fix:**
  - Add a retention setting, for example purging transcripts after 90 days.
  - Log only lengths or IDs at INFO level.
  - Add a per-customer delete action (CCPA requests).

**M8. Twilio voicemail recordings may be publicly fetchable.** `main.py:1177-1188`
- **Cause:** Twilio recording URLs are public by default unless "HTTP Basic Auth for media" is on.
- **Fix:** turn that on in the Twilio console and proxy playback through the portal.

**M9. `/health` doesn't check anything.** `main.py:1540-1551`
- **Status:** it always reports OK, even when the database is down. It also exposes the restaurant name, POS type and item count publicly.
- **Fix:** run `SELECT 1` against the database and check scheduler heartbeat age. Return minimal public output.

**M10. Dependencies aren't pinned.** `pyproject.toml:6-15` uses `>=` only, and `deploy/Dockerfile:2` uses `python:3.12-slim` with no digest
- **Status:** every build pulls the latest versions, so a breaking Anthropic SDK or FastAPI release can take production down on redeploy.
- **Fix:** commit a lock file (`pip-compile` or `uv lock`), pin the base image by digest, and run `pip-audit` in CI.

**M11. No rate limits on Twilio, tools or portal APIs, and no request size limit.**
- Only `/chat` and `/voice/speak` are limited, and only in memory.
- **Fix:** add an app-wide limiter (for example `slowapi`) and a body-size limit at the proxy.

**M12. Background agents run inside the web process.** `main.py:1732-1738`, `agents/scheduler.py:92-104`
- **Status:** heavy jobs (menu sync, QA, billing) compete with live call turns on a small single process. The database lease prevents duplicate runs, which is good.
- **Fix:** run the agents as a separate Render background worker, or schedule them off-peak.

### LOW

| # | Issue | Where | Fix |
|---|---|---|---|
| L1 | No replay protection on Twilio webhooks (the signature has no timestamp) | `main.py:800-819` | Reject a repeated `CallSid` + turn `n`, which is already mostly safe |
| L2 | CSP only sets `frame-ancestors` | `main.py:502-513` | Add `default-src 'self'`; move inline scripts to nonces |
| L3 | No CSRF token on portal and admin JSON POSTs (SameSite cookies help) | `portal.py`, `admin.py` | Double-submit token, or check the `Origin` header |
| L4 | `/voice/audio/{key}.mp3` is unauthenticated and has no rate limit (cached TTS only, path-safe) | `main.py:766-775` | Rate limit; serve from a CDN |
| L5 | Real phone number, Square location ID and restaurant name are hard-coded | `render.yaml:46-53`, `setup.sh`, `deploy/seed.py` | Move to `sync:false` environment values |
| L6 | Toast and Clover never check tax against the POS's own figures (only Square does) | `toast.py:150-184`, `clover.py:102-134` | Read back the POS total after create |
| L7 | The Square payment link is cached in memory only (`square.py:93,184-199`) | — | Read it from the saved order record |
| L8 | One failing test: `test_twilio_gather_runs_agent_turn` expects `<Say>One moment.</Say>` (the filler wording or audio changed) | `tests/unit/test_twilio.py:203` | Update the test or the filler |
| L9 | Dev-only tunnel scripts (`ops/ngrok_tunnel.sh`, `supervise.sh`, hard-coded subdomain) sit next to production files. They aren't in the Docker image. | `ops/` | Move to `dev/`; update `PRODUCTION.md` (it describes a stale localtunnel setup) |

---

## 3. Missing or incomplete code
- **Staff text on transfer:** stubbed, with "texting stubbed until Phase 5" at `core/tools.py:467-468`.
- **Square and Clover catalog sync:** `sync_catalog()` does nothing for these two (`square.py:112-117`, `clover.py:92-97`). Square menu sync runs through `square_catalog.py`/menusync, but Clover has none.
- **POS token refresh:** there's no OAuth refresh flow for Square or Clover, so an expired or revoked token only shows up as failed orders. Toast re-logs-in on a 401, which is fine.
- **POS webhooks:** there are none (Square order status, refunds, 86'd items). Signature checking will need to be designed in when they're added.
- **Marketing agent:** writes drafts only and never sends. That's fine, but consent capture must exist before sending is built.
- **Self-service password reset:** not built.
- **Card payments:** `PRODUCTION.md:43` says Square card processing isn't enabled yet, so payment links won't work until it is.
- **Tests:** there are no load or concurrency tests in CI (`scripts/loadtest_calls.py` exists but is manual), and none for the production fail-fast behaviour.
- **Branches:** the 9 other branches were not reviewed.

---

## 4. Go-live checklist
- [ ] Set `TWILIO_AUTH_TOKEN` and `PUBLIC_BASE_URL` (exact https Render URL). Confirm a fake POST to `/twilio/voice` returns 403. **(C1)**
- [ ] Move to an always-on paid plan and set a Twilio fallback URL. **(C2)**
- [ ] Set `PORTAL_SIGNUP_ENABLED=0` and confirm `CHAT_DEMO_ENABLED=0`. **(H1, H2)**
- [ ] Confirm `VOICE_SECRET`, `OWNER_SECRET` and `TENANT_MASTER_KEY` are set, and save the master key offline. **(H3, H7)**
- [ ] Turn on Neon PITR or daily `pg_dump`, then test a restore. **(H4)**
- [ ] Set `DATABASE_URL` and confirm the startup log has no SQLite warning. **(H4)**
- [ ] Set an AI timeout and per-call limits, and a spend alert in the Anthropic console. **(H6)**
- [ ] Register A2P 10DLC and add STOP handling. **(M1)**
- [ ] Add the AI disclosure to the greeting. **(M2)**
- [ ] Turn on Twilio recording media auth. **(M8)**
- [ ] Enable Square card processing (if using payment links).
- [ ] Add an uptime monitor on `/health` (UptimeRobot or Better Stack) plus Render alerts.
- [ ] Pin dependencies and redeploy from a known-good lock file. **(M10)**
- [ ] Place 5 real test calls: order, change, read-back, transfer, POS-down (`pos_failed` ticket appears).

## 5. Priority fix order
1. **Before any real call:** C1, H3 (production fail-fast for all secrets), H1 and H2 defaults, C2 hosting.
2. **Week 1:** H4 backups, H6 AI limits and timeouts, H7 key safety, M1 SMS compliance, M2 disclosure, M8 recordings.
3. **Week 2:** H5 Toast/Clover idempotency (before onboarding any Toast or Clover restaurant), M3 and M4 login hardening, M5 and M6 tool input checks, M9 health, M10 pinning.
4. **Then:** M7 retention, M11 rate limits, M12 worker split, Low items, the failing test.
