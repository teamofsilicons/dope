#!/usr/bin/env bash
# Ship the local main branch to production from your laptop:
#   ./deploy/ship.sh
#
# Pushes main to origin, then runs the server-side deploy
# (deploy/deploy.sh) on the box over SSH and verifies the site.
#
# Override the defaults with env vars if your setup differs, e.g.:
#   DOPE_SSH_KEY=~/.ssh/dope.pem DOPE_SSH_HOST=1.2.3.4 ./deploy/ship.sh
set -euo pipefail

HOST="${DOPE_SSH_HOST:-54.67.150.208}"
SSH_USER="${DOPE_SSH_USER:-ubuntu}"
BRANCH="${BRANCH:-main}"
SITE="${DOPE_SITE:-https://dope.teamofsilicons.com}"

if [ -z "${DOPE_SSH_KEY:-}" ]; then
  for candidate in "$HOME/.ssh/dope.pem" "$HOME/Downloads/dope.pem"; do
    if [ -f "$candidate" ]; then
      DOPE_SSH_KEY="$candidate"
      break
    fi
  done
fi
if [ -z "${DOPE_SSH_KEY:-}" ] || [ ! -f "$DOPE_SSH_KEY" ]; then
  echo "!! No SSH key found. Put it at ~/.ssh/dope.pem or set DOPE_SSH_KEY." >&2
  exit 1
fi

cd "$(git rev-parse --show-toplevel)"

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$current_branch" != "$BRANCH" ]; then
  echo "!! You are on '$current_branch', not '$BRANCH'. Switch branches or set BRANCH." >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "!! Working tree is not clean. Commit or stash before shipping." >&2
  git status --short >&2
  exit 1
fi

echo "==> Pushing $BRANCH to origin"
git push origin "$BRANCH"

echo "==> Deploying on $SSH_USER@$HOST"
ssh -o ConnectTimeout=10 -i "$DOPE_SSH_KEY" "$SSH_USER@$HOST" 'sudo /srv/dope/deploy/deploy.sh'

echo "==> Verifying $SITE"
local_sha="$(git rev-parse "$BRANCH")"
remote_sha="$(ssh -o ConnectTimeout=10 -i "$DOPE_SSH_KEY" "$SSH_USER@$HOST" 'git -C /srv/dope rev-parse HEAD')"
if [ "$local_sha" != "$remote_sha" ]; then
  echo "!! Server is at $remote_sha but local $BRANCH is $local_sha" >&2
  exit 1
fi
curl -fsS -o /dev/null -w "  HTTP %{http_code}\n" "$SITE/"
echo "==> Shipped $local_sha"
