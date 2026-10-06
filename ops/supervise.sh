#!/usr/bin/env bash
# VoiceOrderAI supervisor: keeps the API server and the public tunnel alive.
# Safe to run every 2 minutes from cron; uses PID files, never pkill patterns.
set -u

APP_DIR="$HOME/workspace/voiceorder"
OPS_DIR="$APP_DIR/ops"
TUNNEL_DIR="$HOME/workspace/tunnel"
LOG="$OPS_DIR/supervise.log"
APP_PID="$OPS_DIR/app.pid"
TUNNEL_PID="$OPS_DIR/tunnel.pid"
PUBLIC_URL="https://mighty-eel-45.loca.lt"
SUBDOMAIN="mighty-eel-45"

mkdir -p "$OPS_DIR"
touch "$LOG"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

alive() { # alive <pidfile>
  local pid=""
  [ -f "$1" ] && pid="$(cat "$1" 2>/dev/null)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

start_app() {
  cd "$APP_DIR" || return 1
  # shellcheck disable=SC2086
  env ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-haiku-4-5-20251001}" \
      PUBLIC_BASE_URL="$PUBLIC_URL" \
      nohup "$APP_DIR/.venv/bin/uvicorn" voiceorder.api.main:app --port 8000 \
      >>"$APP_DIR/server.log" 2>&1 &
  echo $! > "$APP_PID"
  log "app started pid=$(cat "$APP_PID")"
}

start_tunnel() {
  cd "$TUNNEL_DIR" || return 1
  nohup ./node_modules/.bin/lt --port 8000 --subdomain "$SUBDOMAIN" \
    >>"$TUNNEL_DIR/lt.log" 2>&1 &
  echo $! > "$TUNNEL_PID"
  log "tunnel started pid=$(cat "$TUNNEL_PID")"
}

# --- app ---
if ! curl -fsS -m 8 "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
  log "app unhealthy; restarting"
  if alive "$APP_PID"; then kill "$(cat "$APP_PID")" 2>/dev/null || true; sleep 3; fi
  start_app
  sleep 12
  if curl -fsS -m 8 "http://127.0.0.1:8000/health" >/dev/null 2>&1; then
    log "app recovered"
  else
    log "app FAILED to recover"
  fi
fi

# --- tunnel (with circuit breaker) ---
# This box's network blocks localtunnel's data ports (verified 2026-10-05:
# port 443 connects, port 7223 times out), so tunnel restarts can never
# recover. After TUNNEL_FAIL_THRESHOLD consecutive failures the circuit opens:
# restarts stop (no log spam, no pointless lt processes), the health probe
# keeps running, and a successful probe closes the circuit automatically.
# Manual reset: rm ~/workspace/voiceorder/ops/tunnel_circuit.state
# The tunnel client holds a local connection to :8000, so identify it by
# command pattern (bracket trick avoids matching this script itself).
TUNNEL_FAIL_THRESHOLD=10
TUNNEL_CIRCUIT="$OPS_DIR/tunnel_circuit.state"
tunnel_alive() { pgrep -f "[l]t --port 8000" >/dev/null 2>&1; }
tunnel_ok() { curl -fsS -m 15 "$PUBLIC_URL/health" >/dev/null 2>&1; }
tunnel_circuit_count() {
  local n=""
  [ -f "$TUNNEL_CIRCUIT" ] && n="$(cat "$TUNNEL_CIRCUIT" 2>/dev/null)"
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  echo "$n"
}
tunnel_circuit_set() { echo "$1" > "$TUNNEL_CIRCUIT"; }

# Seed the counter from past failures so the circuit opens immediately when
# the history already proves restarts are pointless.
if [ ! -f "$TUNNEL_CIRCUIT" ]; then
  hist="$(grep -c 'tunnel FAILED to recover' "$LOG" 2>/dev/null || echo 0)"
  case "$hist" in ''|*[!0-9]*) hist=0 ;; esac
  [ "$hist" -gt "$TUNNEL_FAIL_THRESHOLD" ] && hist="$TUNNEL_FAIL_THRESHOLD"
  tunnel_circuit_set "$hist"
fi

circuit_count="$(tunnel_circuit_count)"
if [ "$circuit_count" -ge "$TUNNEL_FAIL_THRESHOLD" ]; then
  # Circuit open: no restart attempts. Probe only, so it self-heals if the
  # endpoint comes back (e.g. stable hosting or a port-443 tunnel replaces it).
  if tunnel_ok; then
    tunnel_circuit_set 0
    log "tunnel circuit closed; public endpoint healthy again"
  fi
  # Otherwise stay silent: quieting the loop is the point.
else
  if ! tunnel_ok; then
    log "tunnel unhealthy; restarting (failure $((circuit_count + 1))/$TUNNEL_FAIL_THRESHOLD)"
    if tunnel_alive; then pkill -f "[l]t --port 8000" 2>/dev/null || true; sleep 3; fi
    start_tunnel
    sleep 20
    if tunnel_ok; then
      tunnel_circuit_set 0
      log "tunnel recovered"
    else
      tunnel_circuit_set $((circuit_count + 1))
      log "tunnel FAILED to recover"
      if [ $((circuit_count + 1)) -ge "$TUNNEL_FAIL_THRESHOLD" ]; then
        log "tunnel circuit OPENED after $TUNNEL_FAIL_THRESHOLD consecutive failures; restarts paused"
      fi
    fi
  elif [ "$circuit_count" -gt 0 ]; then
    tunnel_circuit_set 0
  fi
fi

# --- disk hygiene: keep logs from growing forever ---
for f in "$LOG" "$TUNNEL_DIR/lt.log"; do
  if [ -f "$f" ] && [ "$(stat -c%s "$f" 2>/dev/null || echo 0)" -gt 52428800 ]; then
    tail -c 10485760 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    log "truncated oversized log $f"
  fi
done
