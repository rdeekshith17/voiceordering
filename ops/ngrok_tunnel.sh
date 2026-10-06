#!/usr/bin/env bash
# ngrok tunnel manager for VoiceOrderAI.
#
# NOTE (verified 2026-10-05): ngrok does NOT work from this sandbox. The agent
# honors HTTPS_PROXY, but the TLS handshake to connect.ngrok-agent.com:443
# gets EOF through the egress proxy (blocked/dropped), so no session can ever
# establish. Kept for use on unrestricted networks only.
#
# Original rationale: this sandbox only allows outbound connections to port
# 443, which localtunnel cannot use (verified dead 2026-10-05, 1,128 failed
# restarts). cloudflared quick tunnels ignore the proxy env vars and fail at
# the TLS handshake. pinggy's SSH endpoint throttles this box's shared egress
# IP. ngrok was the last candidate; it is dead here too.
#
# Usage:
#   NGROK_AUTHTOKEN=<token> ./ngrok_tunnel.sh start   # token never echoed/logged
#   ./ngrok_tunnel.sh status|url|stop
#
# On success, the public https URL is written to ops/tunnel.url and printed.
# NOTE: the app embeds PUBLIC_BASE_URL in its TwiML, so after (re)starting the
# tunnel, restart the app with PUBLIC_BASE_URL set to the new URL (the
# supervisor does this from its PUBLIC_URL variable), then repoint Twilio's
# voice webhook (see ops/twilio_repoint.py).
set -u

APP_DIR="$HOME/workspace/voiceorder"
OPS_DIR="$APP_DIR/ops"
BIN="$OPS_DIR/bin/ngrok"
PIDFILE="$OPS_DIR/ngrok.pid"
URLFILE="$OPS_DIR/tunnel.url"
LOG="$OPS_DIR/ngrok.log"
API="http://127.0.0.1:4040/api/tunnels"

cmd="${1:-status}"

ngrok_alive() {
  local pid=""
  [ -f "$PIDFILE" ] && pid="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

public_url() {
  curl -fsS -m 5 "$API" 2>/dev/null \
    | python3 -c "import json,sys; ts=json.load(sys.stdin).get('tunnels',[]); print(next((t['public_url'] for t in ts if t.get('public_url','').startswith('https://')),''))" 2>/dev/null
}

case "$cmd" in
  start)
    if [ -z "${NGROK_AUTHTOKEN:-}" ]; then
      echo "error: NGROK_AUTHTOKEN env var is required (paste your ngrok authtoken; it is never logged)" >&2
      exit 2
    fi
    if [ ! -x "$BIN" ]; then
      echo "error: ngrok binary missing at $BIN" >&2
      exit 2
    fi
    if ngrok_alive; then
      echo "ngrok already running (pid $(cat "$PIDFILE")): $(public_url)"
      exit 0
    fi
    # Kill any stale ngrok on 4040 first.
    pkill -f "[n]grok http 8000" 2>/dev/null || true
    sleep 1
    # NGROK_AUTHTOKEN env var is honored by the ngrok agent; nothing is
    # written to disk, and the token value never appears in logs.
    env NGROK_AUTHTOKEN="$NGROK_AUTHTOKEN" \
      nohup "$BIN" http 8000 --log stdout >>"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "ngrok starting (pid $(cat "$PIDFILE")), waiting for public URL..."
    url=""
    for _ in $(seq 1 30); do
      sleep 2
      url="$(public_url)"
      [ -n "$url" ] && break
      ngrok_alive || { echo "error: ngrok died; see $LOG" >&2; exit 1; }
    done
    if [ -z "$url" ]; then
      echo "error: no public URL after 60s; see $LOG" >&2
      exit 1
    fi
    echo "$url" > "$URLFILE"
    echo "tunnel live: $url"
    echo "next: restart the app with PUBLIC_BASE_URL=$url, then repoint Twilio (ops/twilio_repoint.py --url $url)"
    ;;
  stop)
    if ngrok_alive; then
      kill "$(cat "$PIDFILE")" 2>/dev/null || true
      echo "ngrok stopped"
    else
      echo "ngrok not running"
    fi
    rm -f "$PIDFILE"
    ;;
  url)
    public_url
    ;;
  status|*)
    if ngrok_alive; then
      echo "running (pid $(cat "$PIDFILE")) url=$(public_url)"
    else
      echo "not running"
    fi
    ;;
esac
