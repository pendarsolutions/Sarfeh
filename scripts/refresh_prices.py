"""Refresh the public price table from the providers' own pages, without the app or its database.

    python scripts/refresh_prices.py --state var/prices-state.json --out site/public/prices.json
    python scripts/refresh_prices.py ... --approve iranserver     # apply a move the guardrails held for review
    python scripts/refresh_prices.py ... --only liara --only chabokan
    python scripts/refresh_prices.py ... --import data/price-files/arvancloud-2026-11-24.json

Each provider's page is read with the same adapter and the same guardrails as an installation's price sync
(packages/provider_adapters/sync.py). What happens to a provider:

* read and understood: its rows are replaced by what the page says now, with today as their read date;
* the page couldn't be read or understood: its last good rows stay, with the date they were read;
* prices moved more than the guardrail allows: nothing is applied until it is approved with --approve.

The last good rows are kept in the state file, so a failed read never brings back older prices. Providers never
read live (ArvanCloud, whose site blocks automated reading; Abrha) keep their catalog rows, until a reviewed price
file is applied with --import: someone copies the prices from the official page into a file
(docs/provider-adapters.md), and it goes through the same guardrails as a read. The report printed at the end lists
every change, so it can be checked before it is published.

What a read changes is also kept as an event (scripts/price_feed.py): published as changes.json beside the table,
and posted to Telegram and Bale when notify.json beside the state file names a bot and a channel.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))
sys.path.insert(0, str(ROOT / "scripts"))

import price_feed  # noqa: E402

from pricing_engine import load_seed_snapshot  # noqa: E402
from pricing_engine.catalog import CatalogSnapshot  # noqa: E402
from pricing_engine.models import CatalogItem  # noqa: E402
from pricing_engine.size_table import size_table  # noqa: E402
from provider_adapters import ADAPTERS, ManualPriceAdapter, ParseError, natural_key, plan_sync  # noqa: E402

STATE_SCHEMA = 1
#: a price read longer ago than this is marked on the page, so nobody mistakes it for today's
OLD_AFTER_DAYS = 14


def _dump(item: CatalogItem) -> dict[str, Any]:
    """A row as stored: its own fields only (computed ones such as price_rial are worked out again on load)."""
    return item.model_dump(mode="json", include=set(CatalogItem.model_fields))


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": STATE_SCHEMA, "providers": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != STATE_SCHEMA:
        raise SystemExit(f"{path}: unknown state schema {state.get('schema')}")
    return state


def _fetch(adapter: Any, timeout_s: float) -> tuple[Any, Exception | None]:
    async def run() -> Any:
        return await asyncio.wait_for(adapter.fetch_catalog(), timeout_s)

    try:
        return asyncio.run(run()), None
    except Exception as exc:  # noqa: BLE001 - any failure leaves the last good rows in place
        return None, exc


def refresh(state: dict[str, Any], *, only: set[str], approve: set[str], timeout_s: float) -> list[dict[str, Any]]:
    """Read each provider and update `state` in place. Returns one report entry per provider read."""
    seed = load_seed_snapshot()
    reports = []
    for pid, cls in ADAPTERS.items():
        if only and pid not in only:
            continue
        adapter = cls()
        kept = state["providers"].get(pid)
        previous = ([CatalogItem.model_validate(i) for i in kept["items"]] if kept
                    else [i for i in seed.items if i.provider_id == pid])
        raw, error = _fetch(adapter, timeout_s)
        plan = plan_sync(adapter, raw, previous, parse_error=error)
        entry = {"provider_id": pid, "status": plan.status, "error": plan.error, "warnings": plan.warnings,
                 "changes": plan.summary()["changes"], "applied": False}
        if plan.applicable or (plan.status == "quarantined" and pid in approve):
            fresh = adapter.normalize_catalog(raw)
            ids = {natural_key(p): p.id for p in previous}
            for item in fresh:
                item.id = ids.get(natural_key(item), item.id)  # the same row keeps its identity
            outside = [p for p in previous if (p.service_type, p.product_family) not in adapter.scope]
            since = kept["read_on"][:10] if kept else seed.snapshot_at.date().isoformat()
            entry["event"] = price_feed.event(pid, previous, outside + fresh, raw.fetched_at, since=since)
            state["providers"][pid] = {
                "read_on": raw.fetched_at.isoformat(timespec="seconds"),
                "source_url": raw.source_url,
                "items": [_dump(i) for i in outside + fresh],
            }
            entry["applied"] = True
        reports.append(entry)
    state["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return reports


def import_file(state: dict[str, Any], path: Path, *, approve: set[str]) -> dict[str, Any]:
    """Apply a reviewed price file to `state`: the rows it names get its prices and today as their read date, the
    provider's other rows stay as they were. Returns a report entry like refresh()'s."""
    content = path.read_text(encoding="utf-8")
    try:
        pid = json.loads(content).get("provider_id")
    except (json.JSONDecodeError, AttributeError) as exc:
        raise SystemExit(f"{path}: not a price file ({exc})") from exc
    kept = state["providers"].get(pid)
    seed = load_seed_snapshot()
    previous = ([CatalogItem.model_validate(i) for i in kept["items"]] if kept
                else [i for i in seed.items if i.provider_id == pid])
    if not previous:
        raise SystemExit(f"{path}: the catalog has no provider {pid!r}")
    adapter = ManualPriceAdapter(pid, previous)
    raw, error = None, None
    try:
        raw = adapter.load(content)
    except ParseError as exc:
        error = exc
    plan = plan_sync(adapter, raw, previous, parse_error=error)
    entry = {"provider_id": pid, "status": plan.status, "error": plan.error, "warnings": plan.warnings,
             "changes": plan.summary()["changes"], "applied": False}
    if raw is not None and (plan.applicable or (plan.status == "quarantined" and pid in approve)):
        fresh = {natural_key(i): i for i in adapter.normalize_catalog(raw)}
        merged = [fresh.get(natural_key(p), p) for p in previous]
        since = kept["read_on"][:10] if kept else seed.snapshot_at.date().isoformat()
        entry["event"] = price_feed.event(pid, previous, merged, raw.fetched_at, since=since, source="file")
        state["providers"][pid] = {
            "read_on": raw.fetched_at.isoformat(timespec="seconds"),
            "source_url": raw.source_url,
            "from_file": path.name,
            "items": [_dump(i) for i in merged],
        }
        entry["applied"] = True
    state["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return entry


def snapshot_from(state: dict[str, Any], at: datetime) -> CatalogSnapshot:
    """The catalog with every provider read live replaced by its last good rows."""
    seed = load_seed_snapshot(snapshot_at=at)
    live = {pid: [CatalogItem.model_validate(i) for i in p["items"]] for pid, p in state["providers"].items()}
    items = [i for i in seed.items if i.provider_id not in live] + [i for rows in live.values() for i in rows]
    return CatalogSnapshot(providers=seed.providers, items=items, billing_rules=seed.billing_rules, policy=seed.policy,
                           equivalence_rules=seed.equivalence_rules, recommendation_rules=seed.recommendation_rules,
                           snapshot_at=at)


def render(state: dict[str, Any], at: datetime) -> str:
    table = size_table(snapshot_from(state, at))
    for size in table["sizes"]:
        for offer in size["offers"]:
            read = offer.get("read_on")
            if read and (at.date() - date.fromisoformat(read[:10])).days > OLD_AFTER_DAYS:
                offer["old_after_days"] = OLD_AFTER_DAYS
    # when each provider was last read live: the page shows this beside the per-row dates
    table["live_read"] = {pid: p["read_on"] for pid, p in sorted(state["providers"].items())}
    return json.dumps(table, ensure_ascii=False, separators=(",", ":")) + "\n"


def _print_report(reports: list[dict[str, Any]]) -> None:
    for r in reports:
        mark = "applied" if r["applied"] else "kept last good rows"
        print(f"{r['provider_id']:<11} {r['status']:<11} {mark}")
        if r["error"]:
            print(f"    error: {r['error'][:200]}")
        for w in r["warnings"]:
            print(f"    warning: {w}")
        for c in r["changes"]:
            print(f"    {c['kind']:<8} {c['sku']}: {c['old_price_toman']} -> {c['new_price_toman']}  {','.join(c['fields'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", type=Path, default=ROOT / "var" / "prices-state.json")
    ap.add_argument("--out", type=Path, default=ROOT / "site" / "public" / "prices.json")
    ap.add_argument("--only", action="append", default=[], help="read only this provider (repeatable)")
    ap.add_argument("--approve", action="append", default=[], help="apply a held move for this provider")
    ap.add_argument("--offline", action="store_true", help="read nothing; rebuild the table from the state file")
    ap.add_argument("--import", dest="price_file", type=Path, metavar="FILE",
                    help="apply a reviewed price file and read nothing else (for a provider that can't be read)")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    state = _load_state(args.state)
    feed_path = args.state.with_name("price-changes.json")
    feed = price_feed.load(feed_path)
    seed = load_seed_snapshot()
    if not args.offline:
        if args.price_file:
            reports = [import_file(state, args.price_file, approve=set(args.approve))]
        else:
            reports = refresh(state, only=set(args.only), approve=set(args.approve), timeout_s=args.timeout)
        _print_report(reports)
        args.state.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.state.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(args.state)
        price_feed.add(feed, [r["event"] for r in reports if r.get("event")])
        notify = args.state.with_name("notify.json")
        if notify.exists():
            names = {pid: p.name_fa or p.name_en for pid, p in seed.providers.items()}
            posted = price_feed.post_pending(feed, json.loads(notify.read_text(encoding="utf-8")), names)
            if posted:
                print(f"posted {posted} price change(s)")
        price_feed.save(feed_path, feed)
    text = render(state, datetime.now(UTC))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(args.out)  # a reader never sees half a file
    providers = {pid: {"name_fa": p.name_fa or p.name_en, "name_en": p.name_en} for pid, p in seed.providers.items()}
    changes = args.out.with_name("changes.json")
    tmp = changes.with_suffix(".tmp")
    tmp.write_text(price_feed.published(feed, providers), encoding="utf-8", newline="\n")
    tmp.replace(changes)
    print(f"wrote {args.out} ({len(text.encode()) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
