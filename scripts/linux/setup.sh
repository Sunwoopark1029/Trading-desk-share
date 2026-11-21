#!/usr/bin/env bash
# One-time (and safe to repeat) setup of the cloud server. Run from the cloned repo:
#   sudo bash scripts/linux/setup.sh
# Installs: system packages, uv (via pipx), Python 3.15 (via uv), Node 26 (official tarball, checksum
# checked), the backend venv, the frontend build, the systemd service and the daily backup timer.
# It never reads or prints .env and never opens a public port.
set -euo pipefail

NODE_MAJOR=26
APP_USER=trading
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

say() { printf '\n== %s\n' "$*"; }
as_app() { sudo -u "$APP_USER" -H bash -lc "cd '$REPO' && $*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash scripts/linux/setup.sh"; exit 1; }
[ -f "$REPO/backend/requirements.txt" ] || { echo "Run this from inside the cloned Trading-desk repo."; exit 1; }

say "System packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q git curl ca-certificates xz-utils build-essential pipx sqlite3 tzdata
timedatectl set-timezone Asia/Seoul || true      # logs and the backup timer in KST (the app uses KST itself)

say "App user '$APP_USER'"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$APP_USER"
fi
chown -R "$APP_USER:$APP_USER" "$REPO"

say "Node.js $NODE_MAJOR (official build from nodejs.org, SHA-256 checked)"
if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'process.versions.node.split(".")[0]')" != "$NODE_MAJOR" ]; then
  arch="$(uname -m)"; case "$arch" in x86_64) narch=x64 ;; aarch64) narch=arm64 ;; *) echo "Unknown CPU $arch"; exit 1 ;; esac
  base="https://nodejs.org/dist/latest-v${NODE_MAJOR}.x"
  tmp="$(mktemp -d)"
  curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
  file="$(grep -o "node-v[0-9.]*-linux-${narch}.tar.xz" "$tmp/SHASUMS256.txt" | head -1)"
  curl -fsSL "$base/$file" -o "$tmp/$file"
  (cd "$tmp" && grep " $file\$" SHASUMS256.txt | sha256sum -c -)
  rm -rf /opt/node && mkdir -p /opt/node
  tar -xJf "$tmp/$file" -C /opt/node --strip-components=1
  ln -sf /opt/node/bin/node /usr/local/bin/node
  ln -sf /opt/node/bin/npm /usr/local/bin/npm
  ln -sf /opt/node/bin/npx /usr/local/bin/npx
  rm -rf "$tmp"
fi
node --version

say "uv and Python 3.15 (for user $APP_USER)"
as_app "pipx install uv >/dev/null 2>&1 || pipx upgrade uv >/dev/null 2>&1 || true"
as_app "~/.local/bin/uv python install 3.15"

say "Backend packages"
as_app "[ -x backend/.venv/bin/python ] || ~/.local/bin/uv venv --python 3.15 backend/.venv"
as_app "~/.local/bin/uv pip install --python backend/.venv/bin/python -r backend/requirements.txt"

say "Frontend build"
as_app "cd frontend && npm ci --no-audit --no-fund && npm run build"

say "Folders and permissions"
for d in data logs secrets backups; do
  install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$REPO/$d"
done
[ -f "$REPO/.env" ] && { chown "$APP_USER:$APP_USER" "$REPO/.env"; chmod 600 "$REPO/.env"; }

say "systemd: service + daily backup timer"
sed "s#@REPO@#$REPO#g" "$REPO/scripts/linux/trading-desk.service" > /etc/systemd/system/trading-desk.service
sed "s#@REPO@#$REPO#g" "$REPO/scripts/linux/trading-desk-backup.service" > /etc/systemd/system/trading-desk-backup.service
cp "$REPO/scripts/linux/trading-desk-backup.timer" /etc/systemd/system/trading-desk-backup.timer
systemctl daemon-reload
systemctl enable trading-desk.service trading-desk-backup.timer >/dev/null
systemctl start trading-desk-backup.timer

say "What is still missing"
missing=0
[ -f "$REPO/.env" ] || { echo "- .env: create it yourself (README 'Cloud server'), never paste keys into a chat"; missing=1; }
[ -f "$REPO/secrets/login.json" ] || { echo "- login passcode + PIN: sudo -u $APP_USER -H bash -c 'cd $REPO/backend && .venv/bin/python -m app.set_passcode'"; missing=1; }
[ -f "$REPO/data/config.local.yaml" ] || { echo "- data/config.local.yaml with remote.allow_remote: true and your Tailscale login (README)"; missing=1; }
if [ "$missing" -eq 0 ]; then
  systemctl restart trading-desk.service
  ok=0
  for _ in $(seq 1 20); do          # the app needs a few seconds to start
    if curl -fsS http://127.0.0.1:8000/api/auth/status >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  if [ "$ok" -eq 1 ]; then
    echo "The dashboard is running on 127.0.0.1:8000. Open it through Tailscale serve (README)."
  else
    echo "Started, but it does not answer after 40 s. Check: journalctl -u trading-desk -n 50 --no-pager"
  fi
else
  echo "Fix the items above, then run this script again (it is safe to repeat)."
fi
