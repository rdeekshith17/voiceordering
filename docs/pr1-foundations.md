# PR 1 — Foundations (migrations, roles, flags, audit, agents)

Everything here is invisible to restaurant owners: the portal looks and behaves
exactly as before for existing accounts. It is the base the next features
(AI ON/OFF schedules, kitchen approvals, Super Admin) build on.

## What changed

| Area | Change |
|---|---|
| Migrations | `voiceorder/migrations.py`. Numbered steps, each applied once per database and recorded in `schema_migrations`. Runs at startup (Postgres takes an advisory lock). |
| Roles | `tenant_users.role` = `owner` / `manager` / `kitchen`. Existing users became `owner`. Permissions live in `voiceorder/tenants/rbac.py`. |
| Portal enforcement | Every portal route names the permission it needs. A page the role can't open redirects to the role's home page; an API call returns 403. The sidebar hides pages the role can't open. |
| Platform users | `platform_users` (super_admin / support), separate from restaurant logins. Created only with `scripts/create_platform_user.py`. No UI yet (PR 4). |
| Feature flags | `tenant_feature_flags`, all **off** by default: `voice_schedule_enabled`, `hitl_enabled`, `multilingual_enabled`. |
| Audit log | `audit_logs`, append-only, secrets redacted. Written on signup, settings changes, POS credential saves, POS credentials copied from env at startup, flag changes, platform-user creation. |
| Agents | `voiceorder/agents/scheduler.py` runs all 7 agents in the web app when `AGENTS_ENABLED=1`. A `job_runs` lease keeps each to one run per interval, even across restarts or several instances. |

### Role permissions

| Permission | owner | manager | kitchen |
|---|:-:|:-:|:-:|
| Dashboard, Statistics, Marketing, Usage | ✓ | ✓ | |
| Orders | ✓ | ✓ | ✓ |
| Customers, Live calls | ✓ | ✓ | |
| System settings | ✓ | ✓ | |
| POS setup (credentials) | ✓ | | |
| Support | ✓ | ✓ | |
| Manage staff (UI in a later PR) | ✓ | | |
| Decide kitchen approvals (PR 3) | ✓ | ✓ | ✓ |

### Agent schedule

| Agent | Every | Writes |
|---|---|---|
| support_check | 15 min | platform / POS-health tickets |
| fraud_watch | 1 h | fraud tickets |
| menu_sync | 6 h | menu cache + `menu_sync_*` settings |
| billing_rollup | 1 day | `usage_daily` (yesterday, UTC) |
| onboarding_check | 1 day | stalled-signup tickets |
| qa_review | 1 day | call-quality tickets |
| marketing_drafts | 7 days | promo drafts (never sent) |

All are deterministic (no LLM by default), idempotent or deduplicated, and only
make read-only calls to the POS. Status: `job_runs` table (`store.job_status()`).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AGENTS_ENABLED` | off (`1` in `render.yaml`) | Run the agent scheduler in the app |
| `DISABLED_FEATURES` | empty | Emergency kill switch without the DB, e.g. `hitl_enabled` or `all` |

## Operating it

- **Create a platform admin:**
  `DATABASE_URL=… python scripts/create_platform_user.py you@example.com super_admin`
  (password prompted, 12+ characters).
- **Turn a flag on for one restaurant** (until the Super Admin UI exists):
  `store.set_flag(tenant_id, "voice_schedule_enabled", True, actor="you")`.
- **Kill a feature everywhere:** `store.set_flag("*", flag, False, actor=…)` or set
  `DISABLED_FEATURES`.
- **Free plan note:** `support_check` requests the app's own public `/health` about
  every 15 minutes. That may delay Render's free-plan sleep, but it isn't a reliable
  keep-alive (Render sleeps after 15 idle minutes and the check can land late), so
  don't count on it for answering calls instantly.

## Rollback

All steps are additive (new tables, one new column with a default), so deploying
the previous release is safe: older code ignores them. Don't delete the tables;
the audit log is meant to be kept. To stop the agents without a deploy, set
`AGENTS_ENABLED=0` in Render.

## Verification

- `pytest -q`: 293 passed, 1 failed (known env-dependent filler test), 5 skipped.
- `TEST_DATABASE_URL=… pytest tests/unit/test_postgres_storage.py`: 5 passed on
  local Postgres, including two app instances racing for one job lease.
- Upgrade of the exact pre-PR 1 production schema on local Postgres: existing
  user became `owner`, tenant untouched, second start applied nothing.
- All 7 agents run once against a copy of the local database with real Square
  credentials: all `ok` in about 1 s.
