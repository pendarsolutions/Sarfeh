"""HostIran: the cloud server plans of its Iran location, from the price API HostIran gave us.

    https://hostiran.net/cloud-service/cloud/cloud-info

The API lists every location and cluster HostIran sells. HostIran asked to be listed with the plans of one of them, its
"Iran location" table: the Beheshti datacenter's professional cluster with fast disks. That node is what this reads;
its prices are the table's exactly. Its other clusters (cheaper "iran" ones, NVMe ones in Mashhad and Ahwaz) are left
out until HostIran says which it wants compared.

Prices in the API are in Rial (the table HostIran sent shows the same numbers in Toman, a tenth). The API states no
traffic terms, IPv4, CPU class or disk type, so none is set: the engine then says it can't price a plan exactly,
rather than guess.

The endpoint sits behind ArvanCloud's browser check: a client that doesn't run its JavaScript gets the check page,
which fails the read visibly (SourceBlockedError) until HostIran exempts the path. A copy saved from a browser can be
applied meanwhile (scripts/refresh_prices.py --saved hostiran=FILE).
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from pricing_engine.models import CatalogItem

from .base import HttpAdapterMixin, ParseError, RawCatalog, SourceBlockedError, base_item

#: what ArvanCloud's browser check page contains instead of the JSON
CHALLENGE_MARKERS = ("Transferring to the website", "isTehranTimezone", "error-section--waiting")
PACKAGE = "beheshti"
NODE = "کلاستر حرفه ای - دیسک پرسرعت"
RIAL_PER_TOMAN = Decimal(10)
DATACENTER = "Beheshti"


def _family(name: str) -> str:
    """'Memory Optimized 3' -> 'memory_optimized'."""
    return re.sub(r"[\s-]+", "_", re.sub(r"\s*\d+$", "", name).strip()).lower()


class HostIranAdapter(HttpAdapterMixin):
    provider_id = "hostiran"
    source_url = "https://hostiran.net/cloud-service/cloud/cloud-info"
    scope = {("compute_vm", "general_purpose"), ("compute_vm", "memory_optimized"), ("compute_vm", "cpu_optimized")}
    key_skus = {"General Purpose 1", "General Purpose 2", "Memory Optimized 1", "CPU Optimized 1"}

    def normalize_catalog(self, raw: RawCatalog) -> list[CatalogItem]:
        if any(m in raw.content for m in CHALLENGE_MARKERS):
            raise SourceBlockedError("HostIran's price API returned ArvanCloud's browser check instead of JSON; "
                                     "until the path is exempted, apply a copy saved from a browser")
        try:
            data = json.loads(raw.content)
        except json.JSONDecodeError as exc:
            raise ParseError(f"HostIran's price API didn't return JSON ({exc})") from exc
        node = self._node(data)
        plans = node.get("packages") or []
        if not plans:
            raise ParseError(f"HostIran's {PACKAGE} node {NODE!r} lists no plans")
        price_date = max((p.get("updated_at") or "")[:10] for p in plans) or None
        items = []
        for p in plans:
            monthly = (p.get("calculator") or {}).get("monthly_cost")
            if not isinstance(monthly, int) or monthly <= 0 or not p.get("name"):
                raise ParseError(f"HostIran plan without a name or a monthly price: {p.get('id')}")
            items.append(base_item(
                self.provider_id, raw, service_type="compute_vm", pricing_model="fixed_sku",
                product_family=_family(p["name"]), sku=p["name"], name=p["name"],
                cpu_cores=Decimal(p["cores"]), cpu_unit="core", ram_gb=Decimal(p["memory"]),
                storage_gb=Decimal(p["disk"]), datacenter=DATACENTER, billing_period="month",
                price_toman=Decimal(monthly) / RIAL_PER_TOMAN, unit="plan_month", source_price_date=price_date,
                notes=f"{NODE} ({PACKAGE}). Not stated by the API: traffic terms, IPv4, CPU class, disk type.",
            ))
        return items

    @staticmethod
    def _node(data: dict[str, Any]) -> dict[str, Any]:
        for package in data.get("packages") or []:
            if package.get("name") == PACKAGE:
                for node in package.get("nodes") or []:
                    if node.get("name") == NODE:
                        return node
        raise ParseError(f"HostIran's price API has no {PACKAGE} node named {NODE!r}")
