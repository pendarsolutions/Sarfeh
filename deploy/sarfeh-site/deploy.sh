#!/usr/bin/env bash
# Build the website for pendarsolutions.ir/sarfeh/ and put it on the server. prices.json is left alone: the server
# writes it (sarfeh-prices.timer). Run from the repository root:  deploy/sarfeh-site/deploy.sh [ssh-host]
set -euo pipefail
HOST="${1:-ubuntu@159.195.110.103}"
cd "$(dirname "$0")/../.."
(cd site && MSYS2_ENV_CONV_EXCL=SITE_BASE SITE_BASE=/sarfeh/ npm run build)
tar -C site/dist --exclude=./prices.json -czf - . | ssh "$HOST" 'mkdir -p ~/sarfeh/www && tar -C ~/sarfeh/www -xzf -'
ssh "$HOST" 'cd ~/sarfeh/src && git pull -q --ff-only && cp deploy/sarfeh-site/compose.yml deploy/sarfeh-site/site.conf ~/sarfeh/ \
  && cd ~/sarfeh && docker compose up -d --quiet-pull && docker exec sarfeh-site nginx -s reload'
echo "deployed: https://pendarsolutions.ir/sarfeh/prices/"
