"""The public record of price changes: what each refresh of the price table found.

scripts/refresh_prices.py calls this after it reads a provider. A provider's rows before and after the read are
compared, and what changed becomes one event: plans whose price or size moved, plans that appeared, plans that are
gone. Events are kept in a file beside the refresh's state (oldest first), published as `changes.json` next to
`prices.json` for the price page, and posted once each to Telegram and Bale when those are set up.

Posting is off until `notify.json` exists beside the state file:

    {"telegram": {"token": "123:ABC", "chat_id": "@channel"}, "bale": {"token": "...", "chat_id": "..."}}

Either service can be left out. A post is a few lines of Persian and a link to the price page; nothing else is sent.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Callable

import httpx

from pricing_engine.models import CatalogItem
from provider_adapters import natural_key

SCHEMA = 1
PAGE = "https://pendarsolutions.ir/sarfeh/prices/?view=changes"
#: what counts as a change of a row, beyond its price
SPECS = ("cpu_cores", "ram_gb", "storage_gb", "included_traffic_gb")
#: how many events the page gets, and how many lines of one event a post or the page spells out
PUBLISHED = 60
LINES = 8
#: how many changed rows of one event the page's table gets (a post spells out LINES of them)
PAGE_LINES = 40
#: an event older than this is never posted: a channel set up today doesn't start with a month of old news
POST_WITHIN = timedelta(hours=36)
APIS = {"telegram": "https://api.telegram.org", "bale": "https://tapi.bale.ai"}
#: whole plans first, then what a server is built from, so the lines a post has room for are the ones people look for
KINDS = ("compute_vm", "paas", "dbaas", "compute_component", "memory_component", "storage_component", "ipv4")
PLANS = frozenset(KINDS[:3])


def _s(value: Any) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)).normalize(), "f")


def _row(item: CatalogItem) -> dict[str, Any]:
    out = {"plan": item.sku, "name": item.gpu_model or item.name, "kind": item.service_type, "toman": _s(item.price_toman)}
    out.update({f: _s(getattr(item, f)) for f in SPECS if getattr(item, f) is not None})
    return out


def event(provider_id: str, before: list[CatalogItem], after: list[CatalogItem], at: datetime, *,
          since: str | None = None, source: str = "read") -> dict[str, Any] | None:
    """What changed for a provider between two sets of its rows, or None when nothing a reader cares about did."""
    old, new = {natural_key(i): i for i in before}, {natural_key(i): i for i in after}
    changed, added, removed = [], [], []
    for key, item in new.items():
        was = old.get(key)
        if was is None:
            added.append(_row(item))
            continue
        moved = {f: [_s(getattr(was, f)), _s(getattr(item, f))] for f in ("price_toman", *SPECS)
                 if getattr(was, f) != getattr(item, f)}
        if was.availability != item.availability:
            moved["availability"] = [was.availability, item.availability]
        if moved:
            row = _row(item)
            row["was"] = {("toman" if f == "price_toman" else f): v[0] for f, v in moved.items()}
            changed.append(row)
    removed = [_row(i) for key, i in old.items() if key not in new]
    if not (changed or added or removed):
        return None
    def by_price(r: dict[str, Any]) -> tuple[int, Decimal]:
        return (KINDS.index(r["kind"]) if r["kind"] in KINDS else len(KINDS), Decimal(r["toman"] or "0"))

    return {"id": f"{at.isoformat(timespec='seconds')}|{provider_id}", "at": at.isoformat(timespec="seconds"),
            "provider_id": provider_id, "since": since, "source": source, "price_date": _price_date(after),
            "changed": sorted(changed, key=by_price), "new": sorted(added, key=by_price),
            "removed": sorted(removed, key=by_price), "posted": {}}


def _price_date(rows: list[CatalogItem]) -> str | None:
    """The date the provider itself puts on its prices, when it states one."""
    dates = [i.source_price_date for i in rows if i.source_price_date]
    return max(dates) if dates else None


def added(provider_id: str, rows: list[CatalogItem], at: datetime) -> dict[str, Any]:
    """A provider that joined the price page: every plan of it, as new."""
    e = event(provider_id, [], rows, at, source="added")
    assert e is not None
    return e


# --------------------------------------------------------------------------- the file


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": SCHEMA, "events": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise SystemExit(f"{path}: unknown price-change schema {data.get('schema')}")
    return data


def save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def add(data: dict[str, Any], events: list[dict[str, Any]]) -> None:
    known = {e["id"] for e in data["events"]}
    data["events"] += [e for e in events if e["id"] not in known]
    data["events"].sort(key=lambda e: e["at"])


def _with_percent(row: dict[str, Any]) -> dict[str, Any]:
    old = row.get("was", {}).get("toman")
    if not old or not row.get("toman") or Decimal(old) == 0:
        return row
    move = ((Decimal(row["toman"]) - Decimal(old)) * 100 / Decimal(old)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return {**row, "percent": str(move)} if move else row


def _overall(changed: list[dict[str, Any]]) -> dict[str, Any]:
    """Which way an event's prices went, and by about how much: 'up' or 'down' when every priced row went that
    way ('mixed' otherwise), with the median move as a whole percent when more than one row moved."""
    moves = [(Decimal(r["toman"]) - Decimal(r["was"]["toman"])) * 100 / Decimal(r["was"]["toman"]) for r in changed
             if r.get("was", {}).get("toman") and r.get("toman") and Decimal(r["was"]["toman"]) != 0]
    moves = [m for m in moves if m != 0]
    if not moves:
        return {"direction": None, "typical_percent": None}
    direction = "up" if all(m > 0 for m in moves) else "down" if all(m < 0 for m in moves) else "mixed"
    ordered = sorted(abs(m) for m in moves)
    middle = ordered[len(ordered) // 2] if len(ordered) % 2 else (ordered[len(ordered) // 2 - 1] + ordered[len(ordered) // 2]) / 2
    typical = middle.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return {"direction": direction,
            "typical_percent": str(typical) if direction != "mixed" and len(moves) > 1 and typical else None}


def _history(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each event, what came before it: the provider's previous change, or for its first one, since when the
    prices it replaced had been on record (the read they were compared with); and how many days lie between."""
    last: dict[str, str] = {}
    out = {}
    for e in events:
        pid, day, key = e["provider_id"], e["at"][:10], e.get("id") or f"{e['at']}|{e['provider_id']}"
        previous, kind = (last[pid], "change") if pid in last else (e.get("since"), "first_record")
        if e["source"] == "added":
            previous, kind = None, None
        days = (date.fromisoformat(day) - date.fromisoformat(previous[:10])).days if previous else None
        out[key] = {"previous": previous[:10] if previous else None, "previous_kind": kind, "held_days": days}
        last[pid] = day
    return out


def published(data: dict[str, Any], providers: dict[str, dict[str, str]]) -> str:
    """The page's copy: the newest events first, long lists cut with the full counts kept. Every price change comes
    with its percent, and every event with its direction and typical move, so the page never does arithmetic on a
    price."""
    history = _history(data["events"])
    out = []
    for e in reversed(data["events"][-PUBLISHED:]):
        out.append({"at": e["at"], "provider_id": e["provider_id"], "since": e.get("since"), "source": e["source"],
                    "price_date": e.get("price_date"),
                    **history[e.get("id") or f"{e['at']}|{e['provider_id']}"],
                    "counts": {k: len(e[k]) for k in ("changed", "new", "removed")}, **_overall(e["changed"]),
                    "changed": [_with_percent(r) for r in e["changed"][:PAGE_LINES]], "new": e["new"][:3],
                    "removed": e["removed"][:3]})
    return json.dumps({"schema": SCHEMA, "providers": providers, "events": out}, ensure_ascii=False,
                      separators=(",", ":")) + "\n"


# --------------------------------------------------------------------------- posts

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(number: Any) -> str:
    """A whole number in Persian digits with thousands separators."""
    return f"{int(Decimal(str(number))):,}".replace(",", "٬").translate(_FA)


def _percent(old: str, new: str) -> str | None:
    if not old or Decimal(old) == 0:
        return None
    move = (Decimal(new) - Decimal(old)) * 100 / Decimal(old)
    whole = abs(move).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if whole == 0:
        return None
    return f"{fa(whole)}٪ {'گران‌تر' if move > 0 else 'ارزان‌تر'}"


def _line(row: dict[str, Any]) -> str:
    was, parts = row.get("was", {}), []
    if "toman" in was and was["toman"] is not None and row["toman"] is not None:
        pct = _percent(was["toman"], row["toman"])
        parts.append(f"از {fa(was['toman'])} به {fa(row['toman'])} تومان" + (f" ({pct})" if pct else ""))
    for field, label, unit in (("cpu_cores", "پردازنده", "هسته"), ("ram_gb", "رم", "گیگ"), ("storage_gb", "دیسک", "گیگ"),
                               ("included_traffic_gb", "ترافیک", "گیگ")):
        if field in was and was[field] is not None and row.get(field) is not None:
            parts.append(f"{label} از {fa(was[field])} به {fa(row[field])} {unit}")
    if "availability" in was and not parts:
        parts.append("ظرفیتش تمام شد" if was["availability"] == "available" else "دوباره موجود شد")
    return f"{row['plan']}: " + "، ".join(parts or ["شرایطش عوض شد"])


def message(e: dict[str, Any], name: str) -> str:
    """One event as a short Persian post."""
    priced = [r for r in e["changed"] if r.get("was", {}).get("toman") is not None and r["toman"] is not None]
    ups = sum(Decimal(r["toman"]) > Decimal(r["was"]["toman"]) for r in priced)
    n = len(e["changed"])
    # a provider that prices by component (per core, per gigabyte) has rates, not plans
    what = "پلن" if all(r["kind"] in PLANS for r in e["changed"]) else "تعرفه"
    if priced and ups == len(priced) == n:
        head = f"{name} قیمت {fa(n)} {what} را بالا برد"
    elif priced and ups == 0 and len(priced) == n:
        head = f"{name} قیمت {fa(n)} {what} را پایین آورد"
    elif n:
        head = f"{name}: {fa(n)} {what} عوض شد"
    elif e["source"] == "added":
        head = f"{name} به صفحه قیمت صرفه اضافه شد، با {fa(len(e['new']))} پلن"
    elif e["new"] and not e["removed"]:
        head = f"{name} {fa(len(e['new']))} پلن جدید اضافه کرد"
    elif e["removed"] and not e["new"]:
        head = f"{name} {fa(len(e['removed']))} پلن را برداشت"
    else:
        head = f"{name}: فهرست پلن‌ها عوض شد"
    lines = [head] + [_line(r) for r in e["changed"][:LINES]]
    if n > LINES:
        lines.append(f"و {fa(n - LINES)} {what} دیگر")
    extra = []  # the counts the first line didn't already give
    if e["new"] and (n or e["removed"]):
        extra.append(f"{fa(len(e['new']))} پلن جدید")
    if e["removed"] and (n or e["new"]):
        extra.append(f"{fa(len(e['removed']))} پلن حذف شد")
    if extra:
        lines.append(" · ".join(extra))
    if e["source"] == "file":
        lines.append("(قیمت‌ها دستی از صفحه رسمی خوانده شده)")
    return "\n".join(lines + ["", PAGE])


def post_pending(data: dict[str, Any], config: dict[str, Any], names: dict[str, str], now: datetime | None = None,
                 send: Callable[..., Any] = httpx.post) -> int:
    """Post each recent event once to every service that is set up. A failed post stays pending for the next run."""
    now = now or datetime.now(UTC)
    sent = 0
    for e in data["events"]:
        for service, api in APIS.items():
            target = config.get(service) or {}
            if e["posted"].get(service) or not (target.get("token") and target.get("chat_id")):
                continue
            if now - datetime.fromisoformat(e["at"]) > POST_WITHIN:
                e["posted"][service] = "skipped"  # older than the channel's first day: not news any more
                continue
            try:
                resp = send(f"{api}/bot{target['token']}/sendMessage", timeout=20, json={
                    "chat_id": target["chat_id"], "text": message(e, names.get(e["provider_id"], e["provider_id"])),
                    "disable_web_page_preview": True})
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - the refresh must not fail because a messenger is down
                print(f"    {service}: not posted ({type(exc).__name__}); it will be tried again")
                continue
            e["posted"][service] = now.isoformat(timespec="seconds")
            sent += 1
    return sent
