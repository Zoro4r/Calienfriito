#!/bin/sh
# Auto-deploy the ccs-prod branch on the VM (run every minute by ccs-autodeploy.timer).
# Fetches origin/ccs-prod; when there are new commits, fast-forwards /opt/ccs and
# redeploys only what changed. A lock skips a run while a deploy is still going
# (a backend image build takes ~8 min). Logs: journalctl -u ccs-autodeploy
set -eu
REPO=/opt/ccs
BRANCH=ccs-prod

exec 9>/run/ccs-autodeploy.lock
flock -n 9 || exit 0

cd "$REPO"
git="git -c safe.directory=$REPO"
$git fetch -q origin "$BRANCH"
old=$($git rev-parse HEAD)
new=$($git rev-parse "origin/$BRANCH")
[ "$old" = "$new" ] && exit 0

changed=$($git diff --name-only "$old" "$new")
if ! $git merge -q --ff-only "origin/$BRANCH"; then
  echo "ccs-autodeploy: cannot fast-forward $old -> $new (local changes?); nothing deployed" >&2
  exit 1
fi
echo "ccs-autodeploy: $old -> $new"
echo "$changed" | sed 's/^/  changed: /'

has() { echo "$changed" | grep -qE "$1"; }

if has '^(docker-compose[^/]*\.ya?ml|infra/docker-compose[^/]*\.ya?ml)$'; then
  docker compose up -d --remove-orphans
fi
if has '^backend/(requirements[^/]*\.txt|Dockerfile)$'; then
  docker compose build backend && docker compose up -d --no-deps backend
elif has '^backend/'; then
  docker compose restart backend
fi
if has '^frontend/'; then
  docker compose build frontend && docker compose up -d --no-deps frontend
elif has '^infra/gcp/nginx'; then
  docker compose restart frontend
fi
if has '^infra/gcp/mosquitto'; then
  docker compose restart mosquitto
fi
echo "ccs-autodeploy: done"
