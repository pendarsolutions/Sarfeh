# Sarfeh price service API

The HTTP API behind [Sarfeh's price service](price-service.md): the signed catalog of Iranian cloud prices that every
Sarfeh installation updates from. Sarfeh uses it by itself; this page is for checking it by hand, for monitoring, and
for anyone who wants the same prices in their own tools.

## Mirrors

| Mirror | Base URL |
| --- | --- |
| In Iran | `https://sarfeh-api-ir.pendarsolutions.ir` |
| Outside Iran | `https://sarfeh-api.pendarsolutions.ir` |

Both serve the same catalog, and a token from one works on the other. Sarfeh tries the mirror in Iran first and
remembers whichever answered last. The service's code is `apps/relay/finops_relay/prices.py`.

## Quick check

```bash
B=https://sarfeh-api-ir.pendarsolutions.ir      # or https://sarfeh-api.pendarsolutions.ir

curl -s $B/health                                # no token needed

TOKEN=$(curl -s -X POST $B/v1/prices/register -H 'Content-Type: application/json' -d '{"version":"0.6.0"}' \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

curl -s $B/v1/prices/latest -H "Authorization: Bearer $TOKEN"     # what the newest catalog is

curl -s --compressed -D headers.txt -o catalog.json $B/v1/prices/catalog.json -H "Authorization: Bearer $TOKEN"
grep -i -e etag -e x-catalog headers.txt                          # the signature comes in X-Catalog-Signature
```

## Authentication

Every `/v1/prices` call except `register` (and the publisher's `PUT`) needs a token:

```http
Authorization: Bearer ins_dd509afa3ab9d1e3.04m-A6vsr4vhj04H4Q2ks_bun-CJNOjE
```

Take one token per installation and keep it: it doesn't expire. Sarfeh takes its token when it's installed and keeps
it in its database. A token is the installation's id and an HMAC of it, so either mirror can check it without asking
the other. Optionally send `X-Sarfeh-Version: 0.6.0`; the service keeps the newest version it saw per token.

The service keeps, per token: when it was first and last seen, the version, and how many checks and downloads. No
addresses, and nothing about the servers, costs or people of an installation.

## Endpoints

### `POST /v1/prices/register`

Gives a new token. The body is optional.

```json
{"version": "0.6.0"}
```

`200`:

```json
{"install_id": "ins_dd509afa3ab9d1e3", "token": "ins_dd509afa3ab9d1e3.04m-A6vsr4vhj04H4Q2ks_bun-CJNOjE"}
```

### `GET /v1/prices/latest`

What the newest catalog is, without downloading it. This is what an installation asks every hour.

`200`:

```json
{
  "generated_at": "2026-11-24T13:31:15.267158+00:00",
  "digest": "54134a4e8c6cd6ad9ce88823561726d4629f46281524c04ad08d99a177b55343",
  "sha256": "0930708d2736a3681a031c4faf5d0af7d6eab39c90d950978e937a3a8ccf0452",
  "bytes": 510817,
  "items": 464,
  "providers": {
    "arvancloud": {"digest": "…", "items": 50, "read_on": "2026-11-24T10:58:42+00:00"},
    "abrha": {"digest": "…", "items": 1}
  }
}
```

| Field | Meaning |
| --- | --- |
| `generated_at` | When the catalog was signed. A newer one is published after each read of the providers, twice a day (03:15 and 15:15 Tehran time) |
| `digest` | SHA-256 of all rows **without their read dates**. It changes only when a price, plan or spec changes, so a different digest means new prices, and the same digest means the same prices were read again |
| `sha256` | SHA-256 of the file's exact bytes: the `ETag` of the download |
| `providers.*.digest` | The same per provider, to see which ones changed |
| `providers.*.read_on` | When that provider's prices were last read from its site. A provider without it was never read live (its rows come from the seed catalog) |

### `GET /v1/prices/catalog.json`

The catalog itself, as signed. Ask with `Accept-Encoding: gzip` (`curl --compressed`): about 50 KB instead of 500.

Headers of a `200`:

| Header | |
| --- | --- |
| `X-Catalog-Signature` | Ed25519 signature of the body's exact bytes, base64 |
| `X-Catalog-Generated-At` | The catalog's `generated_at` |
| `ETag` | `"<sha256>"`. Send it back as `If-None-Match` and an unchanged catalog answers `304` with no body |

The body:

```json
{
  "schema": 1,
  "generated_at": "2026-11-24T13:31:15.267158+00:00",
  "digest": "…",
  "providers": {"arvancloud": {"digest": "…", "items": 50, "read_on": "…"}},
  "items": [
    {
      "id": "14bb974b4d855d63", "provider_id": "arvancloud", "service_type": "compute_component",
      "pricing_model": "component_rate", "sku": "IR-CPU-BASIC", "name": "Iran Basic CPU per vCore",
      "region_country": "IR", "cpu_cores": "1", "ram_gb": null, "price_toman": "252000", "billing_period": "month",
      "unit": "vcore_month", "availability": "available", "source_url": "https://www.arvancloud.ir/fa/pricing/all",
      "source_retrieved_at": "2026-11-24", "…": "…"
    }
  ]
}
```

Each item is one price row in the shape of `CatalogItem` (packages/pricing_engine/models.py). Amounts are decimal
strings in Toman, never floats. A row's `source_url` and `source_retrieved_at` say where and when it was read.

### `PUT /v1/prices` (the price reader)

How Pendar's price reader publishes. The body is the catalog's exact bytes, with its signature in
`X-Catalog-Signature`. There is no token: the signature is the credential. A mirror keeps a catalog only if it was
signed with Pendar's key and is newer than the one it has.

| Answer | |
| --- | --- |
| `200 {"status": "stored", …}` | Kept; `/latest` now describes it |
| `200 {"status": "unchanged", …}` | It's the one already there |
| `400` | The signature doesn't match the key, or the schema is unknown |
| `409` | Older than the catalog there: a signed old catalog can't roll prices back |
| `413` | Larger than 20 MB |

### `GET /health`

No token. `{"status": "ok", "bot": false, "bot_username": null, "prices": "<generated_at of the catalog held>"}`,
for monitoring: `prices` should never be more than about 13 hours old.

## Checking the signature

Never use a catalog whose signature you haven't checked. Pendar's public key (also in
`apps/api/finops_api/config.py` as `SARFEH_CATALOG_PUBLIC_KEY`):

```text
FgZ6B1RBtESN+VYQYBxzc4N7dlQF8D96+ExCsIe4ZAw=
```

```python
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

KEY = "FgZ6B1RBtESN+VYQYBxzc4N7dlQF8D96+ExCsIe4ZAw="
signature = "..."  # the X-Catalog-Signature header
body = open("catalog.json", "rb").read()  # exactly as downloaded (curl --compressed undoes the gzip)
Ed25519PublicKey.from_public_bytes(base64.b64decode(KEY)).verify(base64.b64decode(signature), body)  # raises if not
```

Also refuse a catalog whose `generated_at` is older than the one you have: Sarfeh does both
(`provider_adapters.read_feed` and `finops_api.catalog_feed.apply_feed`).

## Errors and limits

An error is JSON: `{"detail": "what went wrong"}`.

| Status | When |
| --- | --- |
| `401` | No token, or one the service doesn't recognise. Take a new one with `register` |
| `404` | No catalog published on this mirror yet: try the other |
| `429` | Over a limit. The `detail` says which; wait, or use the other mirror |
| `503` | The mirror isn't set up to give tokens or take catalogs |

Limits are counted on each mirror separately:

| What | Limit |
| --- | --- |
| New tokens | 10 an hour per address |
| `GET /v1/prices/latest` | 60 an hour per token |
| `GET /v1/prices/catalog.json` | 24 a day per token (a `304` doesn't count) |
| `PUT /v1/prices` | 30 an hour per address |
| Anything | 5 requests a second per address, with bursts of 20 (the nginx in front) |

Once an hour for `latest`, and a download only when `digest` or `generated_at` changed, stays far inside them.
