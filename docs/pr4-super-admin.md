# PR 4 — Super Admin dashboard (`/admin`)

One place for platform staff to see and manage every restaurant. Restaurant
admins and kitchen staff can't open it; Super Admins can't use the restaurant
portal. Every change made here is in the audit log.

## Access

- **Login:** `/admin/login`, Super Admin accounts only (`platform_users`, created
  with `scripts/create_platform_user.py`). No web signup.
- **Session:** its own cookie (`vo_admin`, path `/admin`, SameSite=Strict, 12 h),
  signed with a different key than restaurant sessions, so neither can stand in
  for the other.
- **Failed logins:** audited; 5 failures in 15 minutes lock that email out.
- **Every page/API** checks a platform permission server-side.
- **Not built on purpose:** "log in as a restaurant" (impersonation). Admins
  change restaurant settings from the admin pages, in the open, audited.

## Pages

| Page | What it shows / does |
|---|---|
| Overview | Restaurants (active/suspended), calls and orders (24 h), live calls, kitchen requests waiting, calls handled without the AI, missed transfers, open alerts, background agent status with "last run" times, **emergency stop for all AI answering** |
| Restaurants | Search / filter by status; phone, POS provider + connected, calls and orders (7 days), features on, alerts; **add a restaurant with its first Admin login** |
| Restaurant detail | Status + **suspend / reactivate**; current AI state + **turn AI off now**; staff transfer number (warns if unset); POS provider and which credential fields are saved (values never shown); open alerts; feature switches; **staff logins: add Admin/Kitchen, change role, reset password**; recent calls; audit history |
| Live operations | Live calls, kitchen requests waiting, calls without the AI and missed transfers (24 h), across all restaurants |
| Feature rollout | Restaurants × features grid with on/off per restaurant, and a platform kill switch per feature |
| Usage | This month's calls, talk minutes, voice characters, SMS per restaurant (from the billing agent). Counts only — no invented cost figures |
| Audit log | All changes, filter by restaurant and action prefix (e.g. `admin.`, `flag.`, `webhook.`) |

## Behaviour changes outside `/admin`

- **Suspended restaurants:** calls to their number now hear "We're not taking
  calls right now" and hang up. Before, a suspended number fell through to the
  default restaurant.
- **Rejected Twilio webhooks** (bad signature) are audited as
  `webhook.signature_failed`, at most once a minute per endpoint.

## Changes

| Area | Change |
|---|---|
| New | `voiceorder/admin/admin.py` (pages + JSON actions), mounted in `api/main.py` |
| Crypto | `make/read_session_cookie(..., purpose=)`: per-purpose signing keys (portal sessions unchanged) |
| Store | platform-wide reads (calls/orders/alerts by restaurant, all calls, all pending approvals, usage by month), `set_tenant_status`, `reset_user_password`, `saved_flags`, audit filter by action prefix, number lookup incl. suspended |
| Portal UI | `ui.shell` accepts its own nav, home and logout links |
| No migrations | Uses existing tables |

## Operating it

```bash
# create a Super Admin (password prompted)
DATABASE_URL=postgresql://… python scripts/create_platform_user.py you@example.com super_admin
```
Then open `https://<your-app>/admin/login`.

## Rollback

No schema changes. Reverting the code removes `/admin`; restaurants are unaffected.

## Verification

- `pytest -q`: 341 passed, 1 failed (known env-dependent filler test), 7 skipped.
- Postgres: 7 passed; platform-wide counts checked against real rows.
- `test_super_admin.py` (9): all pages load without POS secrets; restaurant and
  admin sessions never interchangeable (incl. a correctly signed but wrong-purpose
  cookie); lockout + audit; create restaurant + admin; duplicate number refused;
  staff login / password reset / last-admin guard; flags, kill switch, emergency
  stop; suspend → closed message; webhook-failure audit rate limit.
- Production Docker image on a copy of production data, in Chrome: overview,
  restaurants, detail, operations, rollout, audit pages; added a kitchen login
  from the admin page (it then logged into the portal and landed on Kitchen);
  suspended → call heard the closed message; reactivated → AI answered;
  every step in the audit log.
