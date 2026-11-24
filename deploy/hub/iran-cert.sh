#!/usr/bin/env bash
# The certificate for the mirror in Iran, behind that server's arvan-edge nginx (run as root).
#
# sarfeh-hub-cert.timer runs this every 30 minutes. Until the DNS record points at this server it does nothing; then
# it asks Let's Encrypt (certbot standalone on 127.0.0.1:8899, which arvan-edge passes the challenge to), installs the
# certificate and the HTTPS site, and reloads arvan-edge. From then on certbot's own timer renews it, with the same
# hook (--installed). So HTTPS turns on by itself shortly after the record is added.
set -euo pipefail

NAME=sarfeh-api-ir.pendarsolutions.ir
ADDRESS=109.122.247.136
SSL=/opt/arvan-edge/ssl
CONF=/opt/arvan-edge/conf.d
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = --installed ]; then
  # certbot's deploy hook: a new or renewed certificate is in $RENEWED_LINEAGE
  install -m 644 "$RENEWED_LINEAGE/fullchain.pem" "$SSL/sarfeh-api-ir.crt"
  install -m 600 "$RENEWED_LINEAGE/privkey.pem" "$SSL/sarfeh-api-ir.key"
  install -m 644 "$HERE/iran-https.conf" "$CONF/sarfeh-api-ir-https.conf"
  docker exec arvan-edge nginx -t && docker exec arvan-edge nginx -s reload
  echo "HTTPS is on for $NAME"
  exit 0
fi

if [ -d "/etc/letsencrypt/live/$NAME" ]; then exit 0; fi  # certbot.timer renews it from here on
found="$(getent ahostsv4 "$NAME" | awk '{ print $1; exit }' || true)"
if [ "$found" != "$ADDRESS" ]; then
  echo "$NAME doesn't point at $ADDRESS yet (${found:-no address}); trying again later"
  exit 0
fi
certbot certonly --standalone --http-01-address 127.0.0.1 --http-01-port 8899 -d "$NAME" --non-interactive \
  --agree-tos --register-unsafely-without-email --deploy-hook "$HERE/iran-cert.sh --installed"
