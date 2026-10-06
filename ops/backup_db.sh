#!/usr/bin/env bash
# VoiceOrderAI SQLite backup: timestamped copy + prune to last 10.
set -u
APP_DIR="$HOME/workspace/voiceorder"
BACKUP_DIR="$APP_DIR/data/backups"
LOG="$APP_DIR/ops/supervise.log"
mkdir -p "$BACKUP_DIR"
TS="$(date '+%Y%m%d-%H%M')"
DEST="$BACKUP_DIR/voiceorder-$TS.db"
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$APP_DIR/data/voiceorder.db" ".backup '$DEST'"
else
  cp "$APP_DIR/data/voiceorder.db" "$DEST"
fi
# prune: keep newest 10
ls -t "$BACKUP_DIR"/voiceorder-*.db 2>/dev/null | tail -n +11 | xargs -r rm -f
echo "$(date '+%F %T') db backup -> $(basename "$DEST")" >> "$LOG"
