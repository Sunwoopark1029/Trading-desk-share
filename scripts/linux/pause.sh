#!/usr/bin/env bash
# Stop the live copy (e.g. while the PC uses the same Toss key):  sudo bash scripts/linux/pause.sh
# While paused nothing runs on the server: no data, no chat, and armed automatic rules do NOT fire.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
systemctl stop trading-desk.service
echo "Paused. Armed rules will not fire until you run: sudo bash scripts/linux/resume.sh"
