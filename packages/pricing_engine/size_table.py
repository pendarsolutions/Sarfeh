"""One server size priced at every provider: the data behind the public price page (site/public/prices.json).

A provider's offer for a size is the cheapest thing it sells that covers the size and can be priced exactly: a plan
with at least that much CPU, memory and disk, or exactly that size where a provider prices by component or by size.
Prices are as published, before VAT. A VAT the provider states is given beside the price, never inside it, so every
figure can be checked against the page it was read from.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .catalog import SEED_DIR, CatalogSnapshot
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
#: the hard constraint a plan fails when a term its price depends on is not published (data/seeds/equivalence_rules.json)
PRICEABLE = "HC-PRICEABLE"
#: traffic terms that come from how a provider prices, not from a plan's row
TRAFFIC_BY_STRATEGY = {"component": "metered", "liara_plan": "metered", "daily_table": "not_published"}


CPU_REPORTS = SEED_DIR.parent / "hardware" / "cpu-reports.json"
#: what a host shows when it hides the real processor from its guests
_MASKED = re.compile(r"qemu|kvm|virtual|hypervisor|common .*processor", re.IGNORECASE)
_BRAND = re.compile(r"intel|amd|xeon|epyc|ryzen|ampere|core\(tm\)", re.IGNORECASE)


def cpu_model_hidden(model: str | None) -> bool:
    """Whether a model name is the stand-in a host shows its guests ('QEMU Virtual CPU', 'Common KVM processor')
    and not a real processor's."""
    return bool(model) and bool(_MASKED.search(model)) and not _BRAND.search(model)


def load_cpu_reports(path: Path = CPU_REPORTS) -> list[dict[str, Any]]:
    """CPU models users reported for plans they rent (data/hardware/cpu-reports.json), or none without the file."""
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("reports") or [])


def cpu_models(reports: list[dict[str, Any]]) -> dict[tuple[str, str | None], dict[str, Any]]:
    """The newest report for each provider and plan, with how many reports there are for it. A model name such as
    'QEMU Virtual CPU' means the host hides the processor, which is worth saying as it is."""
    out: dict[tuple[str, str | None], dict[str, Any]] = {}
    for r in sorted(reports, key=lambda r: r["reported_on"]):
        key = (r["provider_id"], r.get("plan"))
        model = " ".join(str(r["cpu_model"]).split())
        out[key] = {"model": model, "hidden": cpu_model_hidden(model),
                    "mhz": str(r["cpu_mhz"]) if r.get("cpu_mhz") else None, "reported_on": r["reported_on"],
                    "reports": out.get(key, {}).get("reports", 0) + 1}
    return out


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
    if any(c.code == "traffic.unstated" for c in priced.components):
        out.append({"id": "traffic_not_stated"})  # priced with no traffic: the provider doesn't say what it costs
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
    ctx = BillingContext(include_tax=True, no_traffic_without_terms=True)
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
        # terms a plan big enough for the size needs to be priced exactly, and the provider doesn't state
        unstated: set[str] = set()
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
                elif all(k.passed for k in c.constraints if k.constraint_id != PRICEABLE):
                    unstated.update(u.missing_field for u in c.candidate.unavailable)
        if found:
            offers.append(min(found, key=lambda o: (Decimal(o["monthly_toman"]), Decimal(o["cpu"]),
                                                    Decimal(o["ram_gb"]), Decimal(o["disk_gb"]), o["plan"] or "")))
        elif unstated:
            # it sells a plan this size, but a term its price depends on (traffic, mostly) isn't published
            missing.append({"provider_id": pid, "reason": "terms_not_stated", "terms": sorted(unstated)})
        else:
            missing.append({"provider_id": pid, "reason": "nothing_this_size"})
    offers.sort(key=lambda o: (Decimal(o["monthly_toman"]), o["provider_id"]))
    for o in offers:  # worked out here, so the page never does arithmetic on prices
        ratio = Decimal(o["monthly_toman"]) / Decimal(offers[0]["monthly_toman"])
        o["times_cheapest"] = str(ratio.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    return offers, missing


def size_table(snapshot: CatalogSnapshot, cpu_reports: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Every size in the grid at every provider. The same catalog (and the same reports) always gives the same table.
    An offer whose plan has a CPU model reported by a user carries it as `cpu_model`."""
    models = cpu_models(load_cpu_reports() if cpu_reports is None else cpu_reports)
    sizes = []
    for cpu, memories in MEMORY_FOR.items():
        for ram in memories:
            for disk in DISKS_GB:
                offers, missing = offers_for(snapshot, cpu, ram, disk)
                for o in offers:
                    if (o["provider_id"], o["plan"]) in models:
                        o["cpu_model"] = models[(o["provider_id"], o["plan"])]
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
