"""One server size priced at every provider: the data behind the public price page (site/public/prices.json).

A provider's offer for a size is the cheapest thing it sells that covers the size and can be priced exactly: a plan
with at least that much CPU, memory and disk, or exactly that size where a provider prices by component or by size.
Prices are as published, before VAT. A VAT the provider states is given beside the price, never inside it, so every
figure can be checked against the page it was read from.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .catalog import CatalogSnapshot
from .equivalence import _workload, compare_resource
from .models import BillingContext, CatalogItem, LineStatus, PriceBreakdown, PriceStatus, Resource
from .money import _serialize
from .pricing import price_resource

SCHEMA = 1
#: vCPUs -> the memory sizes (GB) shown with them
MEMORY_FOR = {1: (1, 2, 4), 2: (2, 4, 8), 4: (4, 8, 16), 8: (8, 16, 32), 16: (16, 32, 64)}
DISKS_GB = (25, 50, 100, 200, 400)
#: what every size is priced with: one public IPv4 and no traffic, since traffic terms differ too much to fold in
WORKLOAD = {"public_ipv4": 1, "traffic_gb": 0, "country": "IR"}
#: traffic terms that come from how a provider prices, not from a plan's row
TRAFFIC_BY_STRATEGY = {"component": "metered", "liara_plan": "metered", "daily_table": "not_published"}


def _source(priced: PriceBreakdown, row: CatalogItem | None) -> dict[str, str | None]:
    line = next((c for c in priced.components if c.source_url), None)
    return {
        "source_url": (row.source_url if row else None) or (line.source_url if line else None),
        "read_on": (line.retrieved_at if line else None) or (row.source_retrieved_at if row else None),
        "price_date": (line.price_date if line else None) or (row.source_price_date if row else None),
    }


def _conditions(priced: PriceBreakdown, row: CatalogItem | None, pol: dict[str, Any]) -> list[dict[str, Any]]:
    """What a reader should know about the figure beyond its size: each an id (with its numbers) that the page puts
    into words in its own language. Worked out from the priced lines and the policy, never from free text."""
    out: list[dict[str, Any]] = []
    ipv4 = [c for c in priced.components if c.code == "ipv4" or c.code.startswith("ipv4.")]
    if any(c.status == LineStatus.ASSUMED_INCLUDED for c in ipv4):
        out.append({"id": "ipv4_assumed"})  # the provider's page doesn't say an address comes with the plan
    else:
        paid = sum((c.subtotal_toman for c in ipv4 if c.status == LineStatus.PRICED and c.subtotal_toman), Decimal(0))
        if paid:
            out.append({"id": "ipv4_priced", "toman": _serialize(paid)})
    if row is not None and row.billing_period == "30_days":
        out.append({"id": "thirty_day_plan"})
    if pol.get("setup_fee_days"):
        out.append({"id": "setup_fee", "days": pol["setup_fee_days"]})
    return out


def _offer(pid: str, kind: str, priced: PriceBreakdown, size: tuple[int, int, int], pol: dict[str, Any],
           row: CatalogItem | None = None, plan: str | None = None) -> dict[str, Any]:
    strategy = pol["strategy"]
    cpu, ram, disk = (row.cpu_cores, row.ram_gb, row.storage_gb) if row else map(Decimal, size)
    policy = TRAFFIC_BY_STRATEGY.get(strategy) or (row.attributes.get("traffic_policy") if row else None) or "unknown"
    return {
        "provider_id": pid,
        "kind": kind,  # plan | components | by_size
        "plan": row.sku if row else plan,
        "plan_name": row.name if row else None,
        "monthly_toman": _serialize(priced.monthly_total_toman),
        "vat_toman": _serialize(priced.monthly_tax_toman) if priced.tax_lines else None,
        "cpu": _serialize(cpu),
        "ram_gb": _serialize(ram),
        "disk_gb": _serialize(disk),
        "cpu_shared": row.cpu_shared if row else None,
        "disk_type": row.storage_type.lower() if row and row.storage_type else None,
        "traffic": policy,
        "included_traffic_gb": _serialize(row.included_traffic_gb) if row and policy == "included_quota" else None,
        "datacenter": row.datacenter if row else None,
        **_source(priced, row),
        "conditions": _conditions(priced, row, pol),
        "notes": list(priced.assumptions),
    }


def offers_for(snapshot: CatalogSnapshot, cpu: int, ram: int,
               disk: int) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Each provider's cheapest exact offer covering the size, cheapest first; then the providers with none, and why."""
    ctx = BillingContext(include_tax=True)
    rules = snapshot.equivalence_rules
    src = Resource(id="size", service_type="compute_vm", cpu_cores=Decimal(cpu), ram_gb=Decimal(ram),
                   storage_gb=Decimal(disk), requires_public_ipv4=True, expected_monthly_download_gb=Decimal(0),
                   expected_monthly_upload_gb=Decimal(0))
    size = (cpu, ram, disk)
    offers: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for pid, pol in snapshot.policy["providers"].items():
        strategy = pol["strategy"]
        if not pol.get("exact_eligible"):
            missing.append({"provider_id": pid, "reason": "starting_price_only"})
            continue
        found: list[dict[str, Any]] = []
        if strategy == "component":
            storage = rules["storage_type_map"]["default"]
            for cls in rules["arvan_candidate_classes"]:
                cand = _workload(src, provider_id=pid, resource_class=cls, storage_type=storage)
                priced = price_resource(cand, snapshot, ctx)
                if priced.status == PriceStatus.EXACT:
                    found.append({**_offer(pid, "components", priced, size, pol, plan=cls), "disk_type": storage})
        elif strategy == "daily_table":
            priced = price_resource(_workload(src, provider_id=pid), snapshot, ctx)
            if priced.status == PriceStatus.EXACT:
                found.append(_offer(pid, "by_size", priced, size, pol))
        else:
            for row in snapshot.provider_items(pid):
                if row.service_type != "compute_vm" or not row.sku or row.availability != "available":
                    continue
                cand = _workload(src, provider_id=pid, sku=row.sku, storage_type=row.storage_type)
                c = compare_resource(src, cand, snapshot, ctx, candidate_row=row)
                if c.passed_hard_constraints and c.candidate.status == PriceStatus.EXACT:
                    found.append(_offer(pid, "plan", c.candidate, size, pol, row=row))
        if found:
            offers.append(min(found, key=lambda o: (Decimal(o["monthly_toman"]), Decimal(o["cpu"]),
                                                    Decimal(o["ram_gb"]), Decimal(o["disk_gb"]), o["plan"] or "")))
        else:
            missing.append({"provider_id": pid, "reason": "nothing_this_size"})
    offers.sort(key=lambda o: (Decimal(o["monthly_toman"]), o["provider_id"]))
    for o in offers:  # worked out here, so the page never does arithmetic on prices
        ratio = Decimal(o["monthly_toman"]) / Decimal(offers[0]["monthly_toman"])
        o["times_cheapest"] = str(ratio.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return offers, missing


def size_table(snapshot: CatalogSnapshot) -> dict[str, Any]:
    """Every size in the grid at every provider. The same catalog always gives the same table."""
    sizes = []
    for cpu, memories in MEMORY_FOR.items():
        for ram in memories:
            for disk in DISKS_GB:
                offers, missing = offers_for(snapshot, cpu, ram, disk)
                sizes.append({"cpu": cpu, "ram_gb": ram, "disk_gb": disk, "offers": offers, "missing": missing})
    return {
        "schema": SCHEMA,
        "catalog_version": snapshot.catalog_version,
        "catalog_date": snapshot.snapshot_at.date().isoformat(),
        "workload": WORKLOAD,
        "providers": {pid: {"name_fa": p.name_fa or p.name_en, "name_en": p.name_en, "website": p.website}
                      for pid, p in snapshot.providers.items() if pid in snapshot.policy["providers"]},
        "sizes": sizes,
    }
