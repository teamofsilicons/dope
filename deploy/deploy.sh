#!/usr/bin/env bash
# Run on the Dopes host. State and credentials live outside the checkout.
set -euo pipefail
APP_DIR="${APP_DIR:-/srv/dope}"
SERVICE="${SERVICE:-dope}"
BRANCH="${1:-${BRANCH:-main}}"
EXPECTED="${2:-}"
VENV="${VENV:-$APP_DIR/.venv}"
cd "$APP_DIR"
exec 9>/run/lock/dope-deploy.lock
flock -x 9
git diff --quiet && git diff --cached --quiet || { echo "The production checkout has tracked edits." >&2; exit 1; }
git fetch --quiet --no-tags origin "refs/heads/$BRANCH"
revision="$(git rev-parse FETCH_HEAD)"
[[ -z "$EXPECTED" || "$EXPECTED" == "$revision" ]] || { echo "The remote branch changed before deployment." >&2; exit 1; }
git switch --quiet --detach "$revision"
if [[ ! -x "$VENV/bin/python" ]]; then python3 -m venv "$VENV"; fi
"$VENV/bin/python" -m pip --disable-pip-version-check install --quiet --only-binary=:all: -r requirements.txt
install -m 0644 deploy/dope.service "/etc/systemd/system/$SERVICE.service"
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
curl --retry 15 --retry-delay 1 --retry-connrefused --fail --silent --show-error \
  http://127.0.0.1:8000/openapi.json -o /dev/null
systemctl is-active --quiet "$SERVICE"
echo "Serving $revision"
