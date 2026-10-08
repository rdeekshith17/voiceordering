# VoiceOrderAI — Deployment Runbook

Move the app off the temporary tunnel to stable hosting. Everything technical is
prepped here; you (or whoever owns the accounts) supply the host, domain, and secrets.

## What you're deploying

- **App:** FastAPI, `uvicorn voiceorder.api.main:app`, listens on `$PORT` (default 8000).
- **State:** orders, carts, tenants, portal accounts, POS credentials, settings,
  call transcripts, tickets and usage. Either:
  - **Postgres** — set `DATABASE_URL=postgresql://…` and everything lives there.
    Use this on any host whose disk is wiped on deploy (Render free plan).
  - **SQLite** (default) — `data/voiceorder.db` (override with `VOICEORDER_DB`),
    which must live on a **persistent volume**.
  Menu cache `data/square_catalog.json` and TTS cache `.tts_cache/` are caches
  and can be lost safely.
- **Stateless parts:** nothing else. Keep it to **one replica** on SQLite —
  SQLite can't be shared across replicas.

## Option 0 — Render free plan + free Postgres (no monthly cost)

Render's free plan wipes the container's disk on every deploy, restart and
idle spin-down, so data must live in an outside database.

1. Create a free Postgres at **neon.tech** (or supabase.com). Pick the region
   closest to your Render region. Copy the connection string; it looks like
   `postgresql://user:password@ep-xxxx.us-east-2.aws.neon.tech/neondb?sslmode=require`.
2. Render dashboard → your service → **Environment** → add
   `DATABASE_URL` = that connection string → **Save** (this redeploys).
3. Check the deploy log for `storage backend: postgres (postgres://…@<host>)`.
   If you see `DATABASE_URL is not set` instead, the variable didn't save.
4. Sign up again in the portal once (earlier data was already wiped), connect
   your POS, and set your time zone. From now on it survives deploys.
5. Make sure `TZ` is set to the restaurant's zone (e.g. `America/Chicago`);
   otherwise pickup times told to callers are in UTC.
6. Keep `TENANT_MASTER_KEY` unchanged forever: saved POS credentials are
   encrypted with it and can't be read with a different key.

The free plan still sleeps after 15 minutes idle, so the first call after a
quiet spell can time out while the service wakes up (see Option A).

## Option A — Render (easiest, ~$7/mo)

1. Push `~/workspace/voiceorder` to a private GitHub repo (make sure `.env` and
   `data/` are git-ignored — they contain secrets).
2. Render dashboard → **New → Web Service** → connect the repo.
   - **Runtime:** Docker (it will find `deploy/Dockerfile`? No — Render builds
     from the repo root. Easiest: set **Dockerfile Path** to `deploy/Dockerfile`
     and **Docker Context** to `.` (repo root), since the Dockerfile does
     `COPY voiceorder ./voiceorder`.)
   - **Plan:** Starter ($7/mo) is enough for this workload.
3. Add a **Disk** (persistent volume): name `voiceorder-data`, mount path
   `/app/data`, size 1 GB. (TTS cache can stay ephemeral.)
4. **Environment → Add from `.env`**: paste your filled-in `.env.production`
   (copy of `deploy/.env.production.example`). Set `PUBLIC_BASE_URL` to your
   Render URL, e.g. `https://voiceorder-ai.onrender.com`.
5. Deploy. Note: Render free tier spins down on idle — **don't use free** for a
   phone service; an inbound call during a cold start will time out.

## Option B — Fly.io (~$5–10/mo)

1. Install `flyctl` and `fly launch` inside `~/workspace/voiceorder`
   (it will detect the Dockerfile; point it at `deploy/Dockerfile`).
2. `fly volumes create voiceorder_data --size 1 --region <nearest>` and add to
   `fly.toml`:
   ```toml
   [[mounts]]
     source = "voiceorder_data"
     destination = "/app/data"
   ```
3. `fly secrets import < deploy/.env.production` (same filled-in file).
4. `fly deploy`. You get `https://<app>.fly.dev`; add a custom domain later in
   `fly certs`.
5. Keep `min_machines_running = 1` — never scale to zero for a phone service.

## Option C — Cheap VPS (Hetzner CX22 ~€4/mo, or DigitalOcean $6 droplet)

1. Provision Ubuntu 24.04, point your domain's A record at it.
2. Install Docker: `curl -fsSL https://get.docker.com | sh`
3. Copy the project: `scp -r ~/workspace/voiceorder root@<host>:/opt/voiceorder`
   (exclude `.env`, `data/`, `.venv`, `__pycache__`).
4. On the host: `cd /opt/voiceorder/deploy && cp .env.production.example .env.production`
   and fill it in (set `PUBLIC_BASE_URL=https://<your-domain>`).
5. `docker compose up -d --build`. Data persists in the `voiceorder-data`
   Docker volume even across `up --build`.
6. Put Caddy in front for automatic HTTPS (simplest):
   ```
   <your-domain> {
       reverse_proxy 127.0.0.1:8000
   }
   ```
   `apt install caddy`, drop that in `/etc/caddy/Caddyfile`, `systemctl reload caddy`.

## Repoint Twilio webhooks (do right after deploy)

In the [Twilio Console](https://console.twilio.com) → **Phone Numbers → Manage →
Active numbers → +1 562 268 0097**:

| Field | New value |
|---|---|
| Voice: **A call comes in** | `https://<your-domain>/twilio/voice` (HTTP POST) |
| Voice: **Status callback** | `https://<your-domain>/twilio/status` (HTTP POST) |

The app's `/twilio/gather` and `/twilio/turn` URLs are referenced internally in
the TwiML the app generates from `PUBLIC_BASE_URL` — they follow automatically.
After saving, the old tunnel URL can be retired; nothing else points at it.

## Verify the deploy

1. `curl https://<your-domain>/health` → `{"status":"ok", ...}` with
   `menu_items` > 0 and `pos_profile` correct.
2. Open `https://<your-domain>/portal/login` in a browser → login page renders.
3. Log in as the owner → **Calls** view shows history; **Settings** shows the
   restaurant config.
4. Place a real test call to the Twilio number (from a verified number while
   still on trial) and order an item; check the order appears in the portal
   **Calls** view and in the Square dashboard as an OPEN order.
5. If you added `TWILIO_AUTH_TOKEN`, confirm signature validation is active:
   a forged POST to `/twilio/voice` without a valid `X-Twilio-Signature` must be
   rejected (the app logs a warning and returns an error).

## Backups

- The SQLite DB is the system of record (tenants, orders, **encrypted POS
  credentials**, portal users). The named volume (`voiceorder-data`) survives
  redeploys, but a volume is not a backup.
- Keep the existing cron backup (`ops/backup_db.sh`) running **on the host**:
  it copies `data/voiceorder.db` to timestamped files; copy `data/backups/`
  off the host regularly (e.g. `scp`, or a cron that pushes to S3/rclone).
- **Migrating the current data:** before first deploy, copy the live DB into the
  volume so Hyderabad House's tenant, encrypted Square creds, and order history
  carry over:
  - VPS: `docker compose up -d`, then
    `docker cp data/voiceorder.db <container>:/app/data/voiceorder.db` and restart.
  - Render/Fly: `scp` the db to the host/volume via their SSH/console, or
    re-run the portal signup + POS setup (15 min) and skip the copy.

## Rollback

Keep the old tunnel deployment running until the new URL is verified end to
end (steps 1–4 above). If the new host fails, repoint the Twilio webhooks back
to the old URL — a 60-second change in the Twilio console.

## What NOT to do

- Don't run two replicas against one SQLite file (corruption).
- Don't use Render/Heroku-style free tiers (sleep on idle kills inbound calls).
- Don't commit `.env.production` to git. Ever.
