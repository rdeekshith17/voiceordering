# VoiceOrderAI — run it on your own computer (testing)

Your machine isn't behind the sandbox's network restrictions, so a normal
tunnel (ngrok) works there. This gets you the real thing: live phone calls,
the owner dashboard, and the tenant portal.

## Steps

### 1. Unzip and set up
```bash
unzip voiceorder-local.zip -d voiceorder-local
cd voiceorder-local
./setup.sh
```
`setup.sh` creates a Python virtualenv, installs dependencies, then asks for
your keys (paste them; they stay hidden and are written to `.env` with mode
0600 — nothing is printed or logged):
- Anthropic API key (+ model, default filled in)
- ElevenLabs API key (free plan is fine)
- Twilio Account SID (default filled in) + Auth Token (from your Twilio console)
- Twilio phone number (default filled in: +1 562 268 0097)
- PUBLIC_BASE_URL — leave empty for now; you'll set it after ngrok (step 3)

It prints your **owner-dashboard password** at the end — save it.

Needs: Linux, Python 3.10+.

### 2. Start the app
```bash
./run.sh
```
Open http://127.0.0.1:8000/health — you should see `{"status":"ok", ...}`.

### 3. Expose it with ngrok
Install ngrok (https://ngrok.com/download, or `sudo snap install ngrok`), then:
```bash
ngrok config add-authtoken <YOUR_NGROK_TOKEN>
ngrok http 8000
```
Copy the `https://xxxx.ngrok-free.app` URL it shows.

### 4. Point the app at the tunnel
Put that URL in `.env` as `PUBLIC_BASE_URL` (no trailing slash), restart
`./run.sh`.

### 5. Point Twilio at the tunnel
Send the ngrok URL to Hulk. He'll repoint your Twilio number's voice webhook
to `https://xxxx.ngrok-free.app/twilio/voice` (needs your Twilio Auth Token,
which you already entered — he'll ask for it transiently at that moment).

### 6. Test
Call **+1 562 268 0097** from your verified mobile (+1 283 229 8041).
Dashboard: `https://xxxx.ngrok-free.app/owner` (password from step 1).
Portal: `https://xxxx.ngrok-free.app/portal/login`.

## Notes
- The bundle includes your Hyderabad House tenant, its encrypted Square
  credentials, the live menu cache, and the voice cache — no re-entry needed.
- Orders you place while testing hit your **real Square account** as
  pay-at-pickup (card processing isn't onboarded yet). Cancel test orders in
  the Square dashboard afterwards.
- Twilio is still a trial account: calls only to/from verified numbers, and a
  trial disclaimer plays first. Upgrade it last, when you go live for real.
