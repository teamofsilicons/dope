#!/usr/bin/env bash
# Deploy the latest main onto the server.
# Run this on the box (defaults assume the systemd + nginx setup in this dir):
#   sudo /srv/dope/deploy/deploy.sh
#
# Override the defaults with env vars if your paths differ, e.g.:
#   APP_DIR=/srv/dope SERVICE=dope BRANCH=main ./deploy/deploy.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/dope}"
SERVICE="${SERVICE:-dope}"
BRANCH="${BRANCH:-main}"
VENV="${VENV:-$APP_DIR/.venv}"

echo "==> Deploying $SERVICE from $APP_DIR ($BRANCH)"
cd "$APP_DIR"

echo "==> Fetching latest $BRANCH"
git fetch --prune origin
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "==> Syncing dependencies"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt

echo "==> Restarting $SERVICE"
# DB migrations (new tables/columns, category seeding) run automatically on startup.
systemctl restart "$SERVICE"
sleep 1
systemctl --no-pager --lines=0 status "$SERVICE" || true

echo "==> Smoke check"
if curl -fsS -o /dev/null -w "  HTTP %{http_code}\n" http://127.0.0.1:8000/ ; then
  echo "==> Deploy complete"
else
  echo "!! App did not respond on 127.0.0.1:8000 — check: journalctl -u $SERVICE -n 50" >&2
  exit 1
fi
