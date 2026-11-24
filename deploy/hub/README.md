# Sarfeh's price service: running the mirrors

Installations update their prices from Sarfeh's price service (docs/price-service.md explains it from their side).
Pendar runs it on two mirrors with the same code and the same token secret, so a token from one works on the other:

| Mirror | Address | Server | In front |
|---|---|---|---|
| In Iran | `https://sarfeh-api-ir.pendarsolutions.ir` | 109.122.247.136 | Caddy in this folder's compose, with its own certificate. DNS only (grey cloud), so it resolves to this server and not to a CDN that may be out of reach from inside Iran |
| Outside Iran | `https://sarfeh-api.pendarsolutions.ir` | 62.83.32.47 | Cloudflare (proxied, Flexible), then the server's edge nginx `mn2-nginx` (`edge.conf`) |

Installations try the mirror in Iran first.

```text
 price reader (159.195.110.103, scripts/refresh_prices.py, twice a day)
     │ signs the whole catalog, PUT /v1/prices to each mirror
     ▼
 mirror in Iran ─┐
 mirror outside ─┴─► installations: POST /v1/prices/register (once), GET /v1/prices/latest (hourly),
                     GET /v1/prices/catalog.json (when an admin updates; the signature comes in a header)
```

A mirror keeps a catalog only if its signature checks out against `FINOPS_HUB_PUBLIC_KEY` and it is newer than the
one it has. Installations check the signature again against the key built into Sarfeh, so a mirror (or anyone who
takes one over) can't change a price; at most it can stop serving.

## Setting up a mirror

```bash
mkdir -p ~/sarfeh-hub && cd ~/sarfeh-hub
git clone https://github.com/pendarsolutions/Sarfeh.git src
cp src/deploy/hub/compose.yml src/deploy/hub/compose.edge.yml src/deploy/hub/Caddyfile .
# .env (chmod 600): the catalog's public key, the token secret (copy it from the other mirror; never print it), and
#   in Iran:     COMPOSE_PROFILES=tls  HUB_DOMAIN=sarfeh-api-ir.pendarsolutions.ir
#   outside:     COMPOSE_FILE=compose.yml:compose.edge.yml
docker compose up -d
docker exec sarfeh-hub python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8090/health').read())"
```

Outside Iran, `edge.conf` goes to `/opt/infrastructure/nginx/services/sarfeh-api.conf.services`, then
`docker exec mn2-nginx nginx -t && docker exec mn2-nginx nginx -s reload`. In Iran, ports 80 and 443 are Caddy's; if
the certificate was asked for before the DNS record existed, `docker compose restart caddy` asks again at once.

## The price reader

On 159.195.110.103, beside the state file in `~/sarfeh/state/`:

- `catalog-signing-key` (chmod 600) signs every catalog. Its public half is `catalog-public-key`, and it's built into
  Sarfeh as `SARFEH_CATALOG_PUBLIC_KEY` (apps/api/finops_api/config.py). Losing the private key means a new pair and a
  release that carries the new public key; installations on older versions then stop updating.
- `publish.json`: `{"key_file": "catalog-signing-key", "hubs": ["https://sarfeh-api-ir.pendarsolutions.ir", "https://sarfeh-api.pendarsolutions.ir"]}`

Each `sarfeh-prices` run sends the catalog to both mirrors and logs a line per mirror (`journalctl -u sarfeh-prices`).
A mirror that was down gets the next run's catalog. To send it now without reading the providers:

```bash
~/sarfeh/venv/bin/python ~/sarfeh/src/scripts/refresh_prices.py --state ~/sarfeh/state/prices-state.json \
  --out ~/sarfeh/www/prices.json --offline
```

## Day to day

- Update the code: `cd ~/sarfeh-hub/src && git pull && cd .. && docker compose restart hub`
- Installations seen in the last 30 days, per mirror:
  `docker exec sarfeh-hub python -c "import sqlite3,time; print(sqlite3.connect('/app/var/prices/clients.db').execute('select count(*) from clients where last_seen > ?', (time.time()-30*86400,)).fetchone()[0])"`
  (an installation that switched mirrors is counted on both)
- Refuse a token from now on: add its install id to `FINOPS_HUB_REVOKED=ins_...,ins_...` in `.env` on both mirrors,
  then `docker compose up -d`.
- The limits, per mirror: 10 new tokens an hour per address, 60 checks an hour and 24 downloads a day per
  installation, 30 publishes an hour per address. Outside Iran, nginx also allows 5 requests a second per address.
