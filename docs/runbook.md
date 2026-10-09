# VoiceOrderAI — Operator runbook

For whoever keeps the platform running. Production: Render web service
`voiceorderai` (Docker, `deploy/Dockerfile`), database Neon Postgres
(`DATABASE_URL`), phone numbers on Twilio, POS per restaurant (Square live).

## Where things are

| What | Where |
|---|---|
| Platform admin | `https://voiceorderai.onrender.com/admin` (Super Admin logins only) |
| Restaurant portal | `https://voiceorderai.onrender.com/portal` (restaurant Admin / Kitchen) |
| Health | `GET /health` (public), `GET /admin/api/health` (admin) |
| Logs | Render dashboard → service → Logs. Phone numbers are masked to the last 4 digits |
| Change history | `/admin/audit` (every settings, POS, flag, staff and admin change) |
| Background agents | `/admin` overview → Background agents (last run, ok/error) |

## Environment variables (Render → Environment)

| Variable | Value | Notes |
|---|---|---|
| `DATABASE_URL` | Neon connection string | **Required.** Without it, data lives on Render's disk and is wiped on every deploy |
| `TENANT_MASTER_KEY` | generated once | **Never change.** Encrypts POS credentials and signs logins; a new key makes saved credentials unreadable and logs everyone out |
| `TZ` | `America/Chicago` | Fallback clock for restaurants without a time zone |
| `AGENTS_ENABLED` | `1` | Runs the 7 background agents in the app |
| `CHAT_DEMO_ENABLED` | `0` | The public text demo would place real POS orders; keep off in production |
| `PORTAL_SIGNUP_ENABLED` | `1` or `0` | Public self-signup. Set `0` if restaurants are only added from `/admin` |
| `DISABLED_FEATURES` | empty | Emergency kill switch without the database: `voice_schedule_enabled`, `hitl_enabled`, or `all` |
| `TWILIO_AUTH_TOKEN` | from Twilio | Turns on webhook signature checks. Must be set in production |
| `PUBLIC_BASE_URL` | `https://voiceorderai.onrender.com` | Used to build webhook URLs and check signatures |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | | The AI |
| `ELEVENLABS_API_KEY` | | The voice; without it calls fall back to Twilio's built-in voice |
| `VOICE_SECRET`, `OWNER_SECRET` | generated | Protect the tool API and the legacy `/owner` API |

## Deploy

1. Merge to `main` on GitHub. Render builds and deploys automatically.
2. Watch the deploy log for: `migration applied: …` (only when new),
   `storage backend: postgres (…neon.tech…)`, `agent scheduler started`, and no tracebacks.
3. Smoke test: open `/health`; log in to `/admin`; place one test call (see below).

Migrations run automatically at startup, are additive only, and are recorded in
`schema_migrations`, so a restart never re-applies them.

## Roll back

- **Code:** Render → Deploys → pick the previous deploy → "Rollback". Older code
  ignores newer tables/columns, so this is safe.
- **A single feature, instantly:** `/admin/rollout` → "Kill everywhere", or add it to
  `DISABLED_FEATURES` (takes effect on the next deploy/restart).
- Never drop tables or edit `schema_migrations` by hand.

## Emergencies

| Situation | Do this |
|---|---|
| The AI is saying something wrong / misbehaving everywhere | `/admin` → **Emergency stop: all AI answering**. Calls follow each restaurant's fallback (staff / voicemail / message). Resume from the same button |
| One restaurant only | `/admin/restaurants/<restaurant>` → **Turn AI off now** (or the restaurant does it on its AI phone page) |
| A restaurant must stop taking calls entirely | **Suspend** it: callers hear "We're not taking calls right now" |
| Kitchen approvals causing trouble | `/admin/rollout` → Kitchen approvals → **Kill everywhere** |
| Square orders failing | Each failed order is kept on the restaurant's **Orders** page with status **pos failed** and a **critical alert** ("Order didn't reach the POS: call … back", with the phone and items); the caller is told it isn't confirmed yet and staff will call back. Staff enter it in the POS by hand and call the customer. Then check the POS card / **Test connection** |
| Neon down | Calls still answer; saves fail and are logged; callers on hold for the kitchen keep holding, then the AI apologizes and continues. Check status.neon.tech |
| ElevenLabs down | Calls continue with Twilio's built-in voice automatically |
| Anthropic down | Callers hear "Sorry, I hit a snag" and can retry; turn the AI off (emergency stop) to send calls to staff instead |
| Suspicious login activity | `/admin/audit` → filter `admin.` or `portal.`; lockouts appear as `admin.login_locked` / `portal.login_locked` |
| Webhook forgery attempts | `/admin/audit` → filter `webhook.` (`webhook.signature_failed`) |

## Routine tasks

- **Add a Super Admin:** `DATABASE_URL=<neon> python scripts/create_platform_user.py you@example.com super_admin`
- **Add a restaurant:** `/admin/restaurants` → Add a restaurant (creates its first Admin login). Then follow `docs/onboarding-checklist.md`.
- **Add kitchen staff / reset a password:** `/admin/restaurants/<restaurant>` → Staff logins.
- **Rotate a key:**
  - Anthropic / ElevenLabs / Twilio / Square: create the new key at the provider,
    update Render (or the restaurant's POS setup page for Square), then revoke the old one.
  - Neon password: Neon → Roles → reset → paste the new `DATABASE_URL` into Render.
  - **Never rotate `TENANT_MASTER_KEY`** without a re-encryption plan (saved POS
    credentials would become unreadable).
- **Load test** (before a big launch), against a throwaway database only:
  `DATABASE_URL=<test db> python scripts/loadtest_calls.py --calls 40`

## Smoke test call (after any deploy)

1. Call the restaurant number. Expect the greeting (returning callers hear their
   saved name and number).
2. Order one item, confirm, and check it appears in the portal **Orders** and in Square.
3. If AI phone controls are on: **Turn AI off now** → call → should forward to the
   staff number (or voicemail) → **Resume AI**.
4. If kitchen approvals are on: ask for something off-menu → it appears on the
   **Kitchen** page → approve → the AI relays it.
