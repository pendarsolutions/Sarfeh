"""Sarfeh's price service, seen from an installation.

Pendar reads the providers' prices twice a day and publishes them, signed, on two mirrors (one in Iran, one outside).
An installation takes a token from the service the first time (at install), looks every hour at what the newest
catalog is, and when it has new prices, says so: in the dashboard, and by email and Bale where those are set up. An
admin then updates, from the Catalog page or with `sudo sarfeh prices update` (or it happens straight away, with
FINOPS_PRICE_AUTO_UPDATE). A catalog that only says the same prices were read again is applied quietly, so the dates
stay true without anyone being asked.

No price is taken on trust: every catalog is checked against the key built into Sarfeh (SARFEH_CATALOG_PUBLIC_KEY),
whichever mirror it came from. What the service gets from an installation is its token and Sarfeh's version, nothing
about its servers, costs or people.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from provider_adapters import FeedError, feed_info

from .alerts import notify
from .catalog_feed import apply_feed
from .config import SARFEH_VERSION, get_settings
from .orm import PriceServiceState, ProviderRow

log = logging.getLogger("finops.prices")
#: a mirror that doesn't answer quickly is passed over for the next one
TIMEOUT = httpx.Timeout(10, read=60)
#: the signed catalog that comes with a release (scripts/fetch_catalog.py puts it there)
BUNDLED = "catalog.json"


class PriceServiceError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _when(stamp: str | None) -> datetime | None:
    return datetime.fromisoformat(stamp) if stamp else None


def state_row(db: Session) -> PriceServiceState:
    row = db.get(PriceServiceState, 1)
    if row is None:
        row = PriceServiceState(id=1)
        db.add(row)
        db.flush()
    return row


def _mirrors(row: PriceServiceState) -> list[str]:
    urls = get_settings().price_service_list
    if row.last_url in urls:  # the one that answered last time first
        urls = [row.last_url] + [u for u in urls if u != row.last_url]
    return urls


def _headers(token: str | None = None) -> dict[str, str]:
    h = {"User-Agent": f"Sarfeh/{SARFEH_VERSION}", "X-Sarfeh-Version": SARFEH_VERSION}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _register(db: Session, row: PriceServiceState, url: str) -> None:
    r = httpx.post(f"{url}/v1/prices/register", json={"version": SARFEH_VERSION}, headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    row.token, row.registered_at = r.json()["token"], _now()
    db.commit()


def _request(db: Session, row: PriceServiceState, path: str) -> httpx.Response:
    """GET from the first mirror that answers. Takes a token first if there's none, and once more if a mirror no
    longer knows the one there is."""
    errors = []
    for url in _mirrors(row):
        host = urlsplit(url).hostname or url
        for attempt in (1, 2):
            try:
                if not row.token:
                    _register(db, row, url)
                r = httpx.get(f"{url}{path}", headers=_headers(row.token), timeout=TIMEOUT)
            except httpx.HTTPStatusError as exc:
                errors.append(f"{host}: HTTP {exc.response.status_code}")
                break
            except httpx.HTTPError as exc:
                errors.append(f"{host}: {type(exc).__name__}")
                break
            if r.status_code == 401 and attempt == 1:
                row.token = None
                continue
            if r.status_code >= 400:
                detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else ""
                errors.append(f"{host}: HTTP {r.status_code}" + (f" ({detail})" if detail else ""))
                break
            row.last_url = url
            return r
    raise PriceServiceError("; ".join(errors) or "no price service is set (FINOPS_PRICE_SERVICE_URLS)")


def changed_providers(latest: dict[str, Any] | None, applied: dict[str, Any] | None) -> list[str]:
    """Providers whose rows differ between the newest catalog and the applied one."""
    if not latest:
        return []
    have = (applied or {}).get("providers") or {}
    return sorted(pid for pid, p in (latest.get("providers") or {}).items()
                  if p.get("digest") != (have.get(pid) or {}).get("digest"))


def update_available(row: PriceServiceState) -> bool:
    return bool(row.latest) and row.latest.get("digest") != (row.applied or {}).get("digest")


def _names(db: Session) -> dict[str, str]:
    return {p.id: p.name_en for p in db.scalars(select(ProviderRow))}


def _record(row: PriceServiceState, content: bytes, by: str) -> dict[str, Any]:
    info = feed_info(content)
    row.applied = {**info, "applied_at": _now().isoformat(timespec="seconds"), "by": by}
    if not row.latest or (_when(row.latest.get("generated_at")) or _now()) <= _when(info["generated_at"]):
        row.latest = {k: info[k] for k in ("generated_at", "digest", "providers")}
    return info


def update(db: Session, by: str = "admin") -> dict[str, Any]:
    """Download the newest catalog, check its signature and apply it. Raises PriceServiceError or FeedError."""
    row = state_row(db)
    before = row.applied
    r = _request(db, row, "/v1/prices/catalog.json")
    signature = r.headers.get("X-Catalog-Signature", "")
    results = apply_feed(db, r.content, signature, get_settings().catalog_public_key or "")
    info = _record(row, r.content, by)
    row.check_error = None
    db.commit()
    return {"generated_at": info["generated_at"], "changed": changed_providers(info, before), "providers": results}


def check(db: Session) -> dict[str, Any]:
    """Ask the service what its newest catalog is. New prices: say so once (or apply them, with auto-update). The
    same prices read again: apply quietly, so the read dates here stay true."""
    s = get_settings()
    row = state_row(db)
    try:
        latest = _request(db, row, "/v1/prices/latest").json()
    except PriceServiceError as exc:
        row.checked_at, row.check_error = _now(), str(exc)
        db.commit()
        log.warning("price service: %s", exc)
        return status(db)
    row.latest, row.checked_at, row.check_error = latest, _now(), None
    db.commit()
    applied = row.applied or {}
    try:
        if latest.get("digest") != applied.get("digest"):
            if s.price_auto_update:
                update(db, by="automatic")
            elif row.notified_digest != latest.get("digest"):
                _announce(db, latest, applied)
                row.notified_digest = latest.get("digest")
                db.commit()
        elif (_when(latest.get("generated_at")) or _now()) > (_when(applied.get("generated_at")) or _now()):
            update(db, by="confirmation")
    except (PriceServiceError, FeedError) as exc:
        row.check_error = f"update: {exc}"
        db.commit()
    return status(db)


def _announce(db: Session, latest: dict[str, Any], applied: dict[str, Any]) -> None:
    names = _names(db)
    changed = [names.get(pid, pid) for pid in changed_providers(latest, applied)]
    day = (latest.get("generated_at") or "")[:10]
    notify("New prices from Sarfeh's price service",
           f"Pendar published new prices on {day}" + (f", for {', '.join(changed)}" if changed else "") + ".\n"
           "To use them: Catalog page in the dashboard, Update prices; or on the server: sudo sarfeh prices update")


def apply_bundled(db: Session, folder: Path | None = None) -> str:
    """Apply the catalog that came with this release, unless a newer one is applied already. Runs at every start."""
    s = get_settings()
    if not s.uses_price_service:
        return "not used: prices don't come from Sarfeh's price service here"
    folder = folder or s.seed_dir.parent / "feed"
    path = folder / BUNDLED
    if not path.exists():
        return "no catalog came with this release"
    content, signature = path.read_bytes(), (folder / f"{BUNDLED}.sig").read_text(encoding="ascii").strip()
    row = state_row(db)
    have = _when((row.applied or {}).get("generated_at"))
    if have is not None and have >= _when(feed_info(content)["generated_at"]):
        db.commit()
        return "a newer catalog is applied already"
    results = apply_feed(db, content, signature, s.catalog_public_key or "")
    info = _record(row, content, "release")
    db.commit()
    return f"applied the catalog of {info['generated_at']}: " + ", ".join(f"{k} {v}" for k, v in results.items())


def _read_on(providers: dict[str, Any] | None) -> str | None:
    dates = [p["read_on"] for p in (providers or {}).values() if p.get("read_on")]
    return max(dates) if dates else None


def status(db: Session) -> dict[str, Any]:
    s = get_settings()
    source = "catalog_url" if s.catalog_url else s.price_source
    row = db.get(PriceServiceState, 1)
    if row is None or not s.uses_price_service:
        return {"source": source, "auto_update": s.price_auto_update, "update_available": False}
    latest, applied = row.latest, row.applied
    return {
        "source": source,
        "auto_update": s.price_auto_update,
        "registered": bool(row.token),
        "mirror": urlsplit(row.last_url).hostname if row.last_url else None,
        "checked_at": row.checked_at,
        "error": row.check_error,
        "applied": applied and {"generated_at": applied.get("generated_at"), "applied_at": applied.get("applied_at"),
                                "by": applied.get("by"), "read_on": _read_on(applied.get("providers"))},
        "latest": latest and {"generated_at": latest.get("generated_at"), "read_on": _read_on(latest.get("providers"))},
        "update_available": update_available(row),
        "changed": changed_providers(latest, applied) if update_available(row) else [],
    }
