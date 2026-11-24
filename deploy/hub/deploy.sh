#!/usr/bin/env bash
# Put this checkout's committed price service on one of the mirrors, then restart it. The code goes over SSH, not with
# git pull, because GitHub is slow or closed from inside Iran.
#
#   deploy/hub/deploy.sh ubuntu@109.122.247.136     # in Iran
#   deploy/hub/deploy.sh ubuntu@62.83.32.47         # outside Iran
#
# The first time, write ~/sarfeh-hub/.env on the mirror (deploy/hub/README.md); until then it only copies the files.
set -euo pipefail

HOST="${1:?usage: deploy/hub/deploy.sh USER@HOST}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
REV="$(git rev-parse --short HEAD)"
[ -z "$(git status --porcelain -- apps/relay deploy/hub)" ] ||
  echo "warning: apps/relay or deploy/hub has uncommitted changes; only what's committed ($REV) is sent" >&2

git archive --format=tar HEAD apps/relay deploy/hub | ssh "$HOST" "set -e
mkdir -p ~/sarfeh-hub && cd ~/sarfeh-hub
rm -rf src.new && mkdir src.new && tar -x -C src.new && echo $REV > src.new/REVISION
rm -rf src && mv src.new src
cp src/deploy/hub/compose.yml src/deploy/hub/compose.edge.yml src/deploy/hub/Caddyfile .
if [ -f .env ]; then
  sudo docker compose up -d --quiet-pull 2>&1 | grep -v -e Running -e Started -e Waiting || true
  sudo docker compose restart 2>&1 | grep -v -e Restarting -e Started || true
  sleep 5
  sudo docker exec sarfeh-hub python -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8090/health').read().decode())\"
else
  echo 'files copied; write .env (deploy/hub/README.md), then run this again'
fi"
echo "deployed $REV to $HOST"
