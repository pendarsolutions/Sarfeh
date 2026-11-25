"""Put the newest signed price catalog from Sarfeh's price service into data/feed, for a release to carry.

    python scripts/fetch_catalog.py                 # from the price service (scripts/bump-version.sh runs this)
    python scripts/fetch_catalog.py --from DIR      # from a catalog.json and catalog.json.sig already downloaded

A new installation applies it at its first start (finops_api.price_service.apply_bundled), so it begins with the prices
of the day it was released rather than the seed's. The signature is checked against the key built into Sarfeh before
anything is written: a release never carries a catalog an installation would refuse.

The token this needs is kept in var/release-token (outside git) and taken the first time.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "apps" / "api"))

from finops_api.config import SARFEH_CATALOG_PUBLIC_KEY, SARFEH_PRICE_SERVICE, SARFEH_VERSION  # noqa: E402
from provider_adapters import FeedError, feed_info, read_feed  # noqa: E402

OUT = ROOT / "data" / "feed"
TOKEN = ROOT / "var" / "release-token"


def download() -> tuple[bytes, str]:
    import httpx

    headers = {"User-Agent": f"Sarfeh/{SARFEH_VERSION} release", "X-Sarfeh-Version": SARFEH_VERSION}
    errors = []
    for url in [u.strip().rstrip("/") for u in SARFEH_PRICE_SERVICE.split(",")]:
        try:
            if not TOKEN.exists():
                r = httpx.post(f"{url}/v1/prices/register", json={"version": SARFEH_VERSION}, headers=headers, timeout=30)
                r.raise_for_status()
                TOKEN.parent.mkdir(exist_ok=True)
                TOKEN.write_text(r.json()["token"], encoding="ascii")
            r = httpx.get(f"{url}/v1/prices/catalog.json", timeout=60,
                          headers={**headers, "Authorization": f"Bearer {TOKEN.read_text(encoding='ascii').strip()}"})
            if r.status_code == 401:
                TOKEN.unlink()
            r.raise_for_status()
            return r.content, r.headers["X-Catalog-Signature"]
        except (httpx.HTTPError, KeyError) as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    raise SystemExit("couldn't download the catalog:\n  " + "\n  ".join(errors))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", type=Path, help="a folder with catalog.json and catalog.json.sig")
    args = ap.parse_args()
    if args.src:
        content = (args.src / "catalog.json").read_bytes()
        signature = (args.src / "catalog.json.sig").read_text(encoding="ascii").strip()
    else:
        content, signature = download()
    try:
        read_feed(content, signature, SARFEH_CATALOG_PUBLIC_KEY)
    except FeedError as exc:
        raise SystemExit(f"refused: {exc}") from exc
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "catalog.json.tmp"
    tmp.write_bytes(content)
    (OUT / "catalog.json.sig").write_text(signature + "\n", encoding="ascii")
    shutil.move(tmp, OUT / "catalog.json")
    info = feed_info(content)
    from pricing_engine import load_seed_snapshot

    lacking = sorted(set(load_seed_snapshot().providers) - set(info["providers"]))
    if lacking:  # a new installation then has only the seed's rows for them until it updates
        print(f"warning: the catalog has no rows for {', '.join(lacking)}; has the price reader published since "
              "they were added?", file=sys.stderr)
    read = sorted(p["read_on"][:10] for p in info["providers"].values() if p.get("read_on"))
    print(json.dumps({"generated_at": info["generated_at"], "items": info["items"],
                      "providers_read": f"{read[0]} to {read[-1]}" if read else None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
