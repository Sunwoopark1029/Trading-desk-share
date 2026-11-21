#!/usr/bin/env bash
# Update the live copy from GitHub, safely:  sudo bash scripts/linux/update.sh
# Go back to the previous good version:     sudo bash scripts/linux/update.sh --rollback
#
# Steps: refuse if files were edited on the server -> fetch -> fast-forward -> install packages ->
# run the backend tests -> build the screen -> restart -> check it answers. If tests, build or the
# check fail, it returns to the version that was running before and restarts that.
# data/, logs/, secrets/ and .env are not in git and are never touched.
set -euo pipefail

APP_USER=trading
BRANCH=master
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HISTORY="$REPO/logs/deploy_history.txt"

say() { printf '\n== %s\n' "$*"; }
as_app() { sudo -u "$APP_USER" -H bash -lc "cd '$REPO' && $*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash scripts/linux/update.sh"; exit 1; }

healthy() {
  for _ in $(seq 1 20); do
    curl -fsS http://127.0.0.1:8000/api/auth/status >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

build_and_restart() {
  as_app "~/.local/bin/uv pip install --python backend/.venv/bin/python -r backend/requirements.txt"
  as_app "cd frontend && npm ci --no-audit --no-fund && npm run build"
  systemctl restart trading-desk.service
}

go_to() {   # check out a commit (tracked files only), rebuild, restart
  as_app "git reset --hard --quiet '$1'"
  build_and_restart
}

if [ -n "$(as_app 'git status --porcelain --untracked-files=no')" ]; then
  echo "Files in git were changed on the server. Updates come only from GitHub; nothing was changed."
  echo "See them with: sudo -u $APP_USER git -C $REPO status"
  exit 1
fi

current="$(as_app 'git rev-parse HEAD')"

if [ "${1:-}" = "--rollback" ]; then
  prev="$(grep -v " $current\$" "$HISTORY" 2>/dev/null | tail -1 | awk '{print $2}')"
  [ -n "$prev" ] || { echo "No earlier good version recorded in $HISTORY."; exit 1; }
  say "Going back to $prev"
  go_to "$prev"
  healthy && { echo "$(date -Iseconds) $prev" >> "$HISTORY"; echo "Back on $prev and running."; exit 0; }
  echo "The older version does not answer either. Check: journalctl -u trading-desk -n 80"; exit 1
fi

say "Fetching from GitHub"
as_app "git fetch --quiet origin '$BRANCH'"
target="$(as_app "git rev-parse 'origin/$BRANCH'")"
if [ "$target" = "$current" ]; then
  echo "Already up to date ($current)."; exit 0
fi
as_app "git merge --ff-only --quiet 'origin/$BRANCH'" || { echo "Can't fast-forward (history differs). Nothing changed."; exit 1; }

say "Backend packages + tests (the running copy is not touched yet)"
as_app "~/.local/bin/uv pip install --python backend/.venv/bin/python -r backend/requirements.txt"
if ! as_app "cd backend && .venv/bin/python -m pytest -q -x"; then
  say "Tests failed: staying on the running version"
  as_app "git reset --hard --quiet '$current'"
  as_app "~/.local/bin/uv pip install --python backend/.venv/bin/python -r backend/requirements.txt"
  exit 1
fi

say "Screen build + restart"
if ! (as_app "cd frontend && npm ci --no-audit --no-fund && npm run build" && systemctl restart trading-desk.service && healthy); then
  say "New version failed to start: going back to $current"
  go_to "$current"
  healthy && echo "Back on $current and running." || echo "Still not answering. Check: journalctl -u trading-desk -n 80"
  exit 1
fi

[ -s "$HISTORY" ] || echo "$(date -Iseconds) $current" >> "$HISTORY"
echo "$(date -Iseconds) $target" >> "$HISTORY"
chown "$APP_USER:$APP_USER" "$HISTORY"
echo "Updated to $target and running."
