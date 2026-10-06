# VoiceOrderAI — Production Runbook

**Restaurant:** Hyderabad House · **Phone:** +1 562 268 0097 · **POS:** Square (live)
**Public URL:** https://mighty-eel-45.loca.lt · **Health:** `GET /health`

## What's running

| Piece | Where | Supervised by |
|---|---|---|
| FastAPI app (`:8000`) | `~/workspace/voiceorder`, PID in `ops/app.pid` | `ops/supervise.sh` every 5 min (cron `voiceorder-supervise`) |
| LocalTunnel → public HTTPS | `~/workspace/tunnel` | **down — network-blocked** (see note below); supervisor circuit breaker has paused restarts |
| Order DB backups | `data/backups/` (keep 10) | cron `voiceorder-db-backup` every 6 h |
| Logs | `server.log`, `tunnel/lt.log`, `ops/supervise.log` | auto-truncated at 50 MB |

If the app dies, the supervisor restarts it within ~5 minutes and notes
it in `ops/supervise.log`. It only messages you if something **fails to recover**.

> **Tunnel status 2026-10-05:** this box's network blocks localtunnel's data
> ports (verified: port 443 connects, port 7223 times out), so tunnel restarts
> can never recover. `ops/supervise.sh` has a circuit breaker: after 10
> consecutive tunnel failures it stops restarting the tunnel (no log spam) and
> probes silently, auto-resuming if a public endpoint comes back. State lives in
> `ops/tunnel_circuit.state` (delete it to reset). No live inbound calls from
> this box until stable hosting or a port-443 tunnel (cloudflared/ngrok) is set up.

## What's production-ready today

- Real Square menu (74 items) synced at startup; orders created as **OPEN** (visible in dashboard).
- Latency-hardened call flow: gather answers in ~0.5 s, background turns, pre-warmed greeting.
- SQLite order persistence, idempotent submit, pay-at-pickup fallback, per-turn logging.
- TTS/owner endpoints behind secrets + rate limits; Twilio signature validation
  **auto-enforces** the moment `TWILIO_AUTH_TOKEN` is added to `.env`.
- Secrets: `.env` is mode 0600; server logs verified free of key material.
- **Multi-tenant (v0.4.0):** the platform now serves many restaurants. Each tenant
  gets an isolated portal at `GET /portal` — login/signup, POS setup (Square /
  Toast / Clover with encrypted credentials + "Test connection"), settings, and a
  **live call view** showing only that tenant's conversations as they happen.
  Incoming calls route by the dialed Twilio number (`To` → tenant). Hyderabad
  House is tenant #1 with its Square credentials migrated to encrypted storage.

## Your action items (only you can do these)

1. **Square: enable card processing.** Dashboard → Settings → Payments → complete
   onboarding/verification. Until then, orders are pay-at-pickup (no payment links).
   Also cancel the leftover test orders (steak burrito ~$11.37, chicken burritos).
2. **Twilio: upgrade when ready for real customers.** Trial limits calls to verified
   numbers and gates settings behind a $20 top-up. Then: complete **A2P 10DLC**
   registration or SMS receipts won't deliver.
3. **Twilio Auth Token** (skipped for now): paste into `.env` as `TWILIO_AUTH_TOKEN`
   and restart the app — signature validation turns on automatically.
4. **Stable hosting (recommended before real customers).** This server runs in a
   sandbox with a temporary tunnel URL. For real traffic, deploy to a VPS
   (Hetzner/DigitalOcean), Render, or Fly.io with your own domain, then repoint
   the Twilio voice/status webhooks at the new URL. The app is a standard
   FastAPI service: `uvicorn voiceorder.api.main:app --port 8000` + `.env`.

## If something breaks

- Check `GET /health` (local `:8000` and public URL) — `uptime_seconds` tells you
  when it last restarted.
- Read the tail of `server.log` for the failing call (`twilio call ... turn:` lines
  show what the caller said, which tools ran, and the reply).
- Test orders in Square: cancel them in the Square dashboard; they are harmless.
- Kill switches: `TTS_ENABLED=0` disables voice synthesis (falls back to Twilio
  `<Say>`); `SQUARE_MENU_SYNC=0` skips menu sync at startup (uses cache).
