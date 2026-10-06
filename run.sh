#!/usr/bin/env bash
# Start VoiceOrderAI locally (foreground). Ctrl+C to stop.
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "No .env found — run ./setup.sh first."; exit 1; }
exec .venv/bin/uvicorn voiceorder.api.main:app --port 8000
