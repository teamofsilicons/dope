#!/usr/bin/env bash
# Ship a clean branch to the Dopes production Lightsail instance.
set -euo pipefail

BRANCH="${BRANCH:-main}"
SITE="${DOPE_SITE:-https://dopes.teamofsilicons.com}"
cd "$(git rev-parse --show-toplevel)"

remote() {
  if [[ -n "${DOPE_SSH_HOST:-}" ]]; then
    local remote_command
    printf -v remote_command '%q ' "$@"
    ssh -o BatchMode=yes -o ConnectTimeout=12 -i "${DOPE_SSH_KEY:?Set the key for the override host}" \
      "${DOPE_SSH_USER:-ubuntu}@$DOPE_SSH_HOST" "$remote_command"
  else
    python3 deploy/lightsail_ssh.py "$@"
  fi
}

[[ "$(git branch --show-current)" == "$BRANCH" ]] || { echo "Check out $BRANCH before shipping." >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Commit your changes before shipping." >&2; exit 1; }
revision="$(git rev-parse HEAD)"
git push origin "$BRANCH"
remote sudo /srv/dope/deploy/deploy.sh "$BRANCH" "$revision"
curl --fail --silent --show-error -o /dev/null -w 'HTTPS %{http_code}\n' "$SITE/"
echo "Shipped $revision"
