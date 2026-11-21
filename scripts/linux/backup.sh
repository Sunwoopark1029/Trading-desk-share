#!/usr/bin/env bash
# Daily copy of data/ and logs/ (run by trading-desk-backup.timer, or by hand:
#   sudo -u trading bash scripts/linux/backup.sh
# Keeps 14 days in backups/ (not in git). .env and secrets/ are NOT included: the Lightsail snapshot
# covers the whole server, and keys should not travel inside backup files.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KEEP_DAYS=14
OUT="$REPO/backups"
mkdir -p "$OUT"
chmod 700 "$OUT"
name="desk-$(date +%Y%m%d-%H%M).tar.gz"
tar -czf "$OUT/$name" -C "$REPO" data logs
chmod 600 "$OUT/$name"
find "$OUT" -name 'desk-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "Backup: $OUT/$name"
