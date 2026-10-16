# Security Changes: Safer Defaults, Fake-Call Blocking and Admin Security Checks

**Branch:** `security-hardening` · **Date:** June 2026
**Based on:** the audit in [`security-audit-2026-06.md`](security-audit-2026-06.md), findings C1, H1, H2 and H3.

This branch closes the "open by accident" gaps. It does this without taking your live service down: nothing refuses to start. Instead you get:

1. **Safer defaults.** The public demo and public signup are now off unless you turn them on.
2. **A Security checks panel** on the Super Admin overview (`/admin/`). It shows every protective setting, whether it is set, what the risk is if it isn't, and how to fix it. It also shows how many fake or unsigned calls were rejected in the last 24 hours.
3. **An opt-in strict mode** (`SECURITY_STRICT=1`). Once the panel is all green, strict mode makes the app *refuse* phone webhooks and API calls if a protecting secret ever goes missing, instead of accepting them with a warning.
4. **A startup log.** Every missing setting is written to the Render logs at boot, with its severity and fix.

---

## 1. What changed and how it protects you

### 1.1 Public text demo is OFF by default (audit H1)

| | Before | After |
|---|---|---|
| `CHAT_DEMO_ENABLED` not set | Demo **on** | Demo **off** |
| `CHAT_DEMO_ENABLED=0` | off | off |
| `CHAT_DEMO_ENABLED=1` | on | on |
| Any other value (`yes`, `true`, typo) | **on** | off |

- **Why it matters:** `/chat` lets anyone on the internet talk to the ordering AI. That places **real orders in the restaurant's POS** and spends Anthropic and ElevenLabs credits. Previously, any deploy that forgot the setting (Docker, docker-compose, a second Render service) exposed it.
- **How it protects you:** the app is now *secure by omission*. Forgetting a setting can no longer open the demo. Only the exact value `1` turns it on.
- **Code:** `voiceorder/security.py` → `chat_demo_enabled()`, used by `_chat_enabled()` in `voiceorder/api/main.py`.

### 1.2 Public restaurant signup is OFF by default (audit H2)

| | Before | After |
|---|---|---|
| `PORTAL_SIGNUP_ENABLED` not set | Signup **on** | Signup **off** |
| `PORTAL_SIGNUP_ENABLED=1` | on | on |

- **Why it matters:** `/portal/signup` lets anyone create a restaurant account. There is no email check and no CAPTCHA, which means spam tenants, database growth, and strangers getting a foothold in tenant-only features.
- **How it protects you:** new restaurants are added by you from `/admin` (Restaurants → Add). Turn signup on only if you deliberately want open signups. `render.yaml` now sets `PORTAL_SIGNUP_ENABLED=0` explicitly.
- **Code:** `security.signup_enabled()`, wired into `PortalDeps(signup_enabled=...)` in `api/main.py`.

### 1.3 Security checks panel in the Super Admin dashboard (audit C1, H3)

On `/admin/` (Overview), a new **Security checks** card lists:

| Check | Severity if missing | What goes wrong without it |
|---|---|---|
| Twilio call signature check (`TWILIO_AUTH_TOKEN`) | Critical | Anyone can POST fake calls to `/twilio/*` and place real orders |
| Public https address (`PUBLIC_BASE_URL`, must start with `https://`) | Critical | Signatures are checked against this URL; when it's missing or `http://`, the check is silently skipped |
| API secret (`VOICE_SECRET`) | High | `/call/start`, `/tools/*`, `/orders/recent` (customer data) and `/voice/speak` are open |
| POS credential encryption key (`TENANT_MASTER_KEY`) | High | A key file on local disk is used instead. Render wipes it, and saved POS logins become unreadable |
| Permanent database (`DATABASE_URL`) | High | Data goes to a SQLite file that Render deletes on every deploy |
| Public text demo is off | High | See 1.1 |
| AI key (`ANTHROPIC_API_KEY`) | High | Calls can't be answered by the AI |
| Public restaurant signup is off | Medium | See 1.2 |
| Voice key (`ELEVENLABS_API_KEY`, only checked when TTS is on) | Medium | Callers get the plain Twilio voice |
| Owner script secret (`OWNER_SECRET`) | Low | Only scripted `/owner` access; signed-in admins are unaffected |
| Twilio account (`TWILIO_ACCOUNT_SID`) | Low | SMS receipts aren't sent |
| Strict mode (`SECURITY_STRICT`) | Low | Off means a missing secret is warned about, not enforced |

**How the card works:**
- It also shows **"Fake or unsigned calls rejected (24 h)"**. This counts the `webhook.signature_failed` events that are already written to the audit log whenever a request fails the Twilio signature check. A non-zero number means someone is probing your phone webhooks, or your `PUBLIC_BASE_URL` doesn't match what Twilio calls.
- Problems are sorted worst-first, and each one shows the exact fix.
- Values are **never shown**, only whether they're set. The panel reads environment presence only, so it can't leak a secret.
- The same data is available as JSON at `GET /admin/api/security`. It needs a Super Admin session with the `diagnostics.view` permission; unauthenticated requests get 401/403. You can point an uptime monitor at it, for example to alert when `"ok": false`.
- **Code:** `voiceorder/security.py` (`run_checks()`, `problems()`), and `voiceorder/admin/admin.py` (`_security_card()`, `_rejected_webhooks()`, route `/admin/api/security`).

### 1.4 Strict mode: actually block fake calls (audit C1, H3). Opt-in.

When `SECURITY_STRICT=1`:

| Situation | Without strict (today's behaviour) | With strict |
|---|---|---|
| `TWILIO_AUTH_TOKEN` or `PUBLIC_BASE_URL` missing, and a webhook arrives at `/twilio/*` | Accepted, warning logged ("dev only") | **Rejected with 403**, error logged, counted in the "rejected" number |
| `VOICE_SECRET` missing, and a call arrives at `/call/start`, `/tools/*`, `/call/end`, `/orders/recent` or `/voice/speak` | Accepted | **Rejected with 503** "VOICE_SECRET is not set" |
| Wrong or missing Twilio signature (secrets set) | Rejected 403 | Rejected 403 (unchanged) |

- **Why it's opt-in:** you asked to keep the service running and monitor from the admin dashboard for now. Strict mode is the switch for "later". Once the Security checks card is green, set `SECURITY_STRICT=1` on Render. From then on, a secret disappearing (deleted by mistake, renamed, new service) shuts the door instead of silently opening it.
- **Code:** `_twilio_signature_ok()` and `require_secret()` in `voiceorder/api/main.py`.

### 1.5 Startup warnings

On every boot, each failing check is logged, for example:

```
security check [critical]: Twilio call signature check - Set TWILIO_AUTH_TOKEN (Twilio console > Account > API keys & tokens).
```

You can see these in Render → your service → **Logs**.

---

## 2. What you need to do on Render

1. Open **Dashboard → voiceorderai → Environment** and confirm these are set:
   - `TWILIO_AUTH_TOKEN`
   - `PUBLIC_BASE_URL`, set to *exactly* `https://<your-service>.onrender.com` with no trailing slash
   - `VOICE_SECRET`, `TENANT_MASTER_KEY` and `DATABASE_URL`
2. **If you currently rely on public signup,** add `PORTAL_SIGNUP_ENABLED=1`. Otherwise do nothing: it is now off.
3. Deploy this branch, then sign in at `/admin/` and check the **Security checks** card.
4. When everything critical and high is green, set `SECURITY_STRICT=1` and redeploy.
5. Place one test call to confirm calls still work, since a wrong `PUBLIC_BASE_URL` would now be rejected. If the call fails, the "rejected" counter goes up and the fix is to correct `PUBLIC_BASE_URL`.

## 3. Behaviour changes to be aware of

- **Signup and demo are off unless set to `1`.** Previously anything other than `0` meant on.
- No change to call flow, ordering, prices, POS, portal logins or tenant data.
- No database migration is needed.

## 4. Tests

- **New file:** `tests/unit/test_security_posture.py` (6 tests). It covers:
  - the off-by-default values, including typos;
  - `/chat` returning 404 by default;
  - strict mode rejecting unsigned `/twilio/voice`, `/gather` and `/status`;
  - strict mode rejecting API calls when `VOICE_SECRET` is missing;
  - checks sorting and the `http://` detection;
  - the admin card rendering, the JSON API, the rejected-webhook counter, and the API being closed to anonymous visitors.
- **Test config:** `tests/conftest.py` turns the demo and signup on explicitly for the rest of the suite, which exercises them.
- **Existing test fixed:** `test_twilio_gather_runs_agent_turn` no longer hard-codes one filler phrase. The filler wording varies, which made this test fail before this branch.
- **Result:** `365 passed, 7 skipped`.

## 5. Still open from the audit (not in this branch)

These are listed in priority order in `security-audit-2026-06.md`:
- C2: Render free plan sleeping
- H4: Postgres backups
- H5: Toast/Clover duplicate orders
- H6: AI timeouts and per-call limits
- H7: encryption key fingerprint
- M1: SMS STOP/10DLC
- M2: AI disclosure in the greeting
- and the remaining Medium and Low items.
