#!/usr/bin/env bash
# Start the live copy again:  sudo bash scripts/linux/resume.sh
# Stop the PC copy first if it uses the same Toss key (a new Toss token cancels the other one).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
systemctl start trading-desk.service
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/api/auth/status >/dev/null 2>&1; then
    echo "Running again."; exit 0
  fi
  sleep 2
done
echo "Started, but it does not answer yet. Check: journalctl -u trading-desk -n 50"
exit 1
