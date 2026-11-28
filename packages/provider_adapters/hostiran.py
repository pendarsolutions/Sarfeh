"""HostIran: the cloud server plans of its Iranian clusters, from the price API HostIran gave us.

    https://hostiran.net/cloud-service/cloud/cloud-info

The API lists every location HostIran sells as a package of nodes (a cluster with one kind of disk) of plans. This
reads the Iranian clusters sold at their listed price (NODES), each plan as a row of its own:

* `iran` (the API doesn't say which datacenter): the enterprise cluster with fast and with economy disks, the economy
  cluster with fast disks, the enterprise plus cluster with NVMe disks;
* `beheshti`: the professional cluster with fast disks (the one HostIran first asked to be listed with) and with NVMe
  disks; `mashhad` and `ahwaz`: the professional cluster with NVMe disks.

Left out: `payam` and `nsx-payam`, whose packages carry a 40% discount that the API doesn't say the prices include or
not; the `*cds-h*` packages, named after hosts and with no plans; foreign locations; nodes with no plans. Nodes are
matched by package and node name, never by numeric id. One of NODES missing, without plans or with a discount fails
the read (ParseError, naming it): a cluster never drops out unseen, and the last good rows stay until NODES is updated.

Each row carries its cluster as attributes: `cluster` (HostIran's name, without the disk), `disk_class`, and
`generation` where the API states it. The location's tiers (`hostiran.<package>`) name a cluster in their title
("کلاستر سازمانی + FULL SSD") and its compute type in `meta` ("g9"); only `iran` has tiers, for the enterprise (g9)
and economy (g8) clusters, so the professional and enterprise plus clusters get no generation. The disk names map to
storage classes as the same tiers price them: a cluster's fast-disk plans cost what its "FULL SSD" (all_flash) tier
does at the tier's size, its economy-disk plans what its "HDD+SSD" (hybrid) tier does (worked out in
tests/providers/test_hostiran_api.py). NVMe is in the node's name.

A row's storage type and SKU are part of its identity (natural_key), and the Beheshti fast-disk rows were in the
catalog before the API said what its disks are: they keep `storage_type` None (their disk class is in `disk_class`)
and their plain plan names as SKUs, so they stay the same rows on the change record. Every other plan's SKU adds its
cluster ("General Purpose 4 (Enterprise)"): the engine finds a plan by its SKU and the page prints it as the name.

Prices in the API are in Rial (the table HostIran sent shows the same numbers in Toman, a tenth). The API states no
traffic terms, IPv4 or CPU class, so none is set: the engine then says it can't price a plan exactly, rather than
guess.

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
RIAL_PER_TOMAN = Decimal(10)

#: (package, node, label, datacenter): the clusters read, by HostIran's own names. The label goes into the SKU of each
#: plan: the cluster's English name or, for the professional cluster (sold in three datacenters), the datacenter's;
#: then the disk, unless it is the fast one.
NODES = (
    ("beheshti", "کلاستر حرفه ای - دیسک پرسرعت", None, "Beheshti"),  # plain plan names: see above
    ("beheshti", "کلاستر حرفه ای - دیسک NVME", "Beheshti, NVMe", "Beheshti"),
    ("iran", "کلاستر سازمانی - دیسک پرسرعت", "Enterprise", None),
    ("iran", "کلاستر سازمانی - دیسک اقتصادی", "Enterprise, economy disk", None),
    ("iran", "کلاستر اقتصادی - دیسک پرسرعت", "Economy", None),
    ("iran", "کلاستر سازمانی پلاس - دیسک NVME", "Enterprise Plus, NVMe", None),
    ("mashhad", "کلاستر حرفه ای - دیسک NVME", "Mashhad, NVMe", "Mashhad"),
    ("ahwaz", "کلاستر حرفه ای - دیسک NVME", "Ahwaz, NVMe", "Ahwaz"),
)
#: a node name's disk part -> (storage_type, disk_class)
DISKS = {"دیسک پرسرعت": ("SSD", "ssd"), "دیسک اقتصادی": ("hybrid", "hybrid"), "دیسک NVME": ("NVMe", "nvme")}
_GENERATION = re.compile(r"g\d{1,2}", re.IGNORECASE)


def _family(name: str) -> str:
    """'Memory Optimized 3' -> 'memory_optimized', 'Cost-Optimized 3' -> 'cost_optimized'."""
    return re.sub(r"[\s-]+", "_", re.sub(r"\s*\d+$", "", name).strip()).lower()


def generations(data: dict[str, Any], package: str) -> dict[str, str]:
    """Cluster name -> hardware generation ('G9'), as a location's tiers state it: each names a cluster in its title
    ("کلاستر سازمانی + FULL SSD") and its compute type in meta ("g9"). A cluster whose tiers disagree, or a compute
    type of another form, gets none: it is a label beside the price, so it is left out rather than guessed."""
    locations = data.get("hostiran")
    tiers = locations.get(package) if isinstance(locations, dict) else None
    found: dict[str, set[str]] = {}
    for tier in tiers.values() if isinstance(tiers, dict) else ():
        meta = tier.get("meta") if isinstance(tier, dict) else None
        if not isinstance(meta, dict):
            continue
        title, kind = tier.get("title"), meta.get("compute_type")
        if isinstance(title, str) and " + " in title and isinstance(kind, str):
            found.setdefault(title.split(" + ")[0].strip(), set()).add(kind.strip().upper())
    return {cluster: kind for cluster, kinds in found.items() if len(kinds) == 1
            for kind in kinds if _GENERATION.fullmatch(kind)}


class HostIranAdapter(HttpAdapterMixin):
    provider_id = "hostiran"
    source_url = "https://hostiran.net/cloud-service/cloud/cloud-info"
    scope = {("compute_vm", "general_purpose"), ("compute_vm", "memory_optimized"), ("compute_vm", "cpu_optimized"),
             ("compute_vm", "cost_optimized")}
    key_skus = {"General Purpose 1", "General Purpose 2", "Memory Optimized 1", "CPU Optimized 1"}

    def normalize_catalog(self, raw: RawCatalog) -> list[CatalogItem]:
        if any(m in raw.content for m in CHALLENGE_MARKERS):
            raise SourceBlockedError("HostIran's price API returned ArvanCloud's browser check instead of JSON; "
                                     "until the path is exempted, apply a copy saved from a browser")
        try:
            data = json.loads(raw.content)
        except json.JSONDecodeError as exc:
            raise ParseError(f"HostIran's price API didn't return JSON ({exc})") from exc
        if not isinstance(data, dict):
            raise ParseError("HostIran's price API returned JSON of another shape")
        items: list[CatalogItem] = []
        skus: set[str] = set()
        for (package, node_name, label, datacenter), node in zip(NODES, self._nodes(data), strict=True):
            cluster, _, disk = node_name.partition(" - ")
            storage_type, disk_class = DISKS[disk]
            generation = generations(data, package).get(cluster)
            attributes = {"cluster": cluster, **({"generation": generation} if generation else {}),
                          "disk_class": disk_class}
            plans = node["packages"]
            if not isinstance(plans, list) or not all(isinstance(p, dict) and isinstance(p.get("calculator"), dict)
                                                      for p in plans):
                raise ParseError(f"HostIran's {package} node {node_name!r} lists plans of another shape")
            price_date = max(str(p.get("updated_at") or "")[:10] for p in plans) or None
            for p in plans:
                monthly = p["calculator"].get("monthly_cost")
                if not isinstance(monthly, int) or monthly <= 0 or not p.get("name"):
                    raise ParseError(f"HostIran plan without a name or a monthly price: {p.get('id')}")
                if not all(isinstance(p.get(k), int) and p[k] > 0 for k in ("cores", "memory", "disk")):
                    raise ParseError(f"HostIran plan {p['name']!r} ({package}, {node_name}) without its size")
                sku = p["name"] if label is None else f"{p['name']} ({label})"
                if sku in skus:  # the engine finds a plan by provider and SKU
                    raise ParseError(f"HostIran's price API lists {sku!r} twice")
                skus.add(sku)
                items.append(base_item(
                    self.provider_id, raw, service_type="compute_vm", pricing_model="fixed_sku",
                    product_family=_family(p["name"]), sku=sku, name=p["name"],
                    cpu_cores=Decimal(p["cores"]), cpu_unit="core", ram_gb=Decimal(p["memory"]),
                    storage_gb=Decimal(p["disk"]), storage_type=None if label is None else storage_type,
                    datacenter=datacenter, billing_period="month",
                    price_toman=Decimal(monthly) / RIAL_PER_TOMAN, unit="plan_month", source_price_date=price_date,
                    notes=f"{node_name} ({package}). Not stated by the API: traffic terms, IPv4, CPU class.",
                    attributes=dict(attributes),
                ))
        return items

    @staticmethod
    def _nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
        """The node of each of NODES, in order. Each must be there, with plans and no discount: one gone fails the
        read, naming it, rather than leaving a cluster out of the catalog unseen."""
        listed = data.get("packages")
        packages = {p.get("name"): p for p in listed if isinstance(p, dict)} if isinstance(listed, list) else {}
        found, missing = [], []
        for package_name, node_name, _, _ in NODES:
            package = packages.get(package_name) or {}
            nodes = package.get("nodes") if isinstance(package.get("nodes"), list) else []
            node = next((n for n in nodes if isinstance(n, dict) and n.get("name") == node_name), None)
            if node is None or not node.get("packages"):
                missing.append(f"{package_name} {node_name!r}")
                continue
            if package.get("discount") != 0:
                # prices listed beside a discount: before it or after it, the API doesn't say
                raise ParseError(f"HostIran's {package_name} package has a discount ({package.get('discount')}) "
                                 "beside its prices, which may or may not include it")
            found.append(node)
        if missing:
            raise ParseError(f"HostIran's price API has no plans for {len(missing)} of the clusters read: "
                             + "; ".join(missing))
        return found
